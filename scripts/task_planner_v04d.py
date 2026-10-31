#!/usr/bin/env python3
"""Approval-gated, isolated PREPARE/COMMIT prototype for planner v0.4d."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any


VERSION = "0.4d"
BASE_VERSION = "0.4c"
MAX_APPROVAL_TTL = 900
MAX_APPROVAL_RECORD_BYTES = 1_100_000
APPROVAL_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_PENDING = "pending"
STATE_COMMITTING = "committing"
STATE_COMMITTED = "committed"
STATE_FAILED = "failed/requires_reconciliation"
CLAIM_PREFIX = "claim-"
RECEIPT_PREFIX = "receipt-"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = _load(SCRIPT_DIR / "task_planner_v04c.py", "task_planner_v04c")
PlanValidationError = BASE.PlanValidationError
ApprovalError = RuntimeError


def _base_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("version") != VERSION:
        raise PlanValidationError(f"plan.version must be {VERSION}")
    converted = dict(plan)
    converted["version"] = BASE_VERSION
    return converted


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _ensure_isolated_paths(isolated_root: Path, approval_dir: Path) -> tuple[Path, Path]:
    root = isolated_root.resolve()
    approvals = approval_dir.resolve()
    production_root = Path(r"E:\AI-Agent-Workspace")
    test_root = Path(r"E:\AI-Agent-Workspace\test-environment").resolve()
    if root == production_root or (_within(production_root, root) and not _within(test_root, root)):
        raise ApprovalError("v0.4d requires an isolated test-environment root")
    if not _within(root, approvals):
        raise ApprovalError("approval directory must be inside the isolated root")
    if approvals == root:
        raise ApprovalError("approval directory must be a child of the isolated root")
    current = root
    for component in approvals.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise ApprovalError("approval directory symlink is not permitted")
    return root, approvals


def _safe_target(root: Path, relative: str) -> Path:
    BASE.BASE.validate_workspace_path(relative)
    current = root
    for component in relative.split("/"):
        current = current / component
        if current.is_symlink():
            raise ApprovalError("target path contains a symlink")
    target = current.resolve()
    if not _within(root, target):
        raise ApprovalError("target path escapes isolated root")
    return target


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(encoded) > MAX_APPROVAL_RECORD_BYTES:
        raise ApprovalError("approval record is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".approval-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _approval_path(approval_dir: Path, approval_id: str) -> Path:
    if not isinstance(approval_id, str) or not APPROVAL_ID.fullmatch(approval_id):
        raise ApprovalError("approval ID is invalid")
    return approval_dir / f"approval-{approval_id}.json"


def _claim_path(approval_dir: Path, approval_id: str) -> Path:
    _approval_path(approval_dir, approval_id)
    return approval_dir / f"{CLAIM_PREFIX}{approval_id}.json"


def _receipt_path(approval_dir: Path, approval_id: str) -> Path:
    _approval_path(approval_dir, approval_id)
    return approval_dir / f"{RECEIPT_PREFIX}{approval_id}.json"


def _read_json_file(path: Path, label: str, maximum: int = MAX_APPROVAL_RECORD_BYTES) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ApprovalError(f"{label} is unavailable") from exc
    if len(raw) > maximum:
        raise ApprovalError(f"{label} is too large")
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalError(f"{label} is invalid") from exc
    if not isinstance(record, dict):
        raise ApprovalError(f"{label} is invalid")
    return record


def _read_approval(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ApprovalError("approval record symlink is not permitted")
    record = _read_json_file(path, "approval record")
    required = {"version", "approval_id", "status", "expires_at", "plan_sha256", "content_sha256", "target", "content"}
    if not required.issubset(record):
        raise ApprovalError("approval record is incomplete")
    if record["version"] != VERSION or record["status"] not in {STATE_PENDING, STATE_COMMITTING, STATE_COMMITTED, STATE_FAILED}:
        raise ApprovalError("approval state is invalid")
    if not isinstance(record["approval_id"], str) or not APPROVAL_ID.fullmatch(record["approval_id"]):
        raise ApprovalError("approval record ID is invalid")
    if not isinstance(record["content"], str) or len(record["content"].encode("utf-8")) > BASE.BASE.MAX_REPORT_LENGTH:
        raise ApprovalError("approval artifact is invalid")
    if _content_hash(record["content"]) != record["content_sha256"]:
        raise ApprovalError("approval artifact hash mismatch")
    return record


def _write_claim(path: Path, approval_id: str, nonce: str, now: float) -> bool:
    metadata = {"version": VERSION, "approval_id": approval_id, "nonce": nonce, "pid": os.getpid(), "created_at": now}
    encoded = json.dumps(metadata, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return True


def _read_claim(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ApprovalError("claim symlink is not permitted")
    claim = _read_json_file(path, "approval claim", 4096)
    if claim.get("version") != VERSION or not isinstance(claim.get("pid"), int) or not isinstance(claim.get("nonce"), str):
        raise ApprovalError("approval claim is invalid")
    return claim


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _remove_claim(path: Path) -> None:
    if path.is_symlink():
        raise ApprovalError("claim symlink is not permitted")
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _read_receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise ApprovalError("receipt symlink is not permitted")
    receipt = _read_json_file(path, "receipt", 16384)
    required = {"version", "approval_id", "commit_nonce", "target", "plan_sha256", "content_sha256", "sha256", "bytes"}
    if not required.issubset(receipt) or type(receipt["bytes"]) is not int or receipt["bytes"] < 0:
        raise ApprovalError("receipt is incomplete")
    return receipt


def _mark_failed(path: Path, record: dict[str, Any], reason: str, now: float) -> None:
    record["status"] = STATE_FAILED
    record["failure_reason"] = reason[:500]
    record["failed_at"] = now
    _atomic_json(path, record)


def _reconcile_committing(
    path: Path,
    record: dict[str, Any],
    root: Path,
    approvals: Path,
    now: float,
) -> dict[str, Any]:
    """Finalize only when a durable receipt and exact target prove success."""
    target = _safe_target(root, record["target"])
    receipt = _read_receipt(_receipt_path(approvals, record["approval_id"]))
    target_hash: str | None = None
    if target.exists():
        if not target.is_file() or target.is_symlink():
            reason = "target is not a regular file during reconciliation"
        else:
            target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            reason = "target exists without a durable receipt"
    else:
        reason = "commit interrupted before target creation"

    receipt_matches = (
        receipt is not None
        and receipt.get("version") == VERSION
        and receipt.get("approval_id") == record["approval_id"]
        and receipt.get("commit_nonce") == record.get("commit_nonce")
        and receipt.get("target") == record["target"]
        and receipt.get("plan_sha256") == record["plan_sha256"]
        and receipt.get("content_sha256") == record["content_sha256"]
        and receipt.get("sha256") == record["content_sha256"]
        and target_hash == record["content_sha256"]
    )
    if receipt_matches:
        record["status"] = STATE_COMMITTED
        record["committed_at"] = now
        _atomic_json(path, record)
        return {"status": STATE_COMMITTED, "approval_id": record["approval_id"], "target": record["target"], "sha256": target_hash}

    if receipt is not None and not receipt_matches:
        reason = "receipt or target does not match the prepared artifact"
    _mark_failed(path, record, reason, now)
    raise ApprovalError(reason)


def reconcile_approval(
    approval_id: str,
    *,
    isolated_root: Path,
    approval_dir: Path,
    now: float | None = None,
) -> dict[str, Any]:
    """Recover only from durable evidence; never retry file creation."""
    root, approvals = _ensure_isolated_paths(isolated_root, approval_dir)
    path = _approval_path(approvals, approval_id)
    record = _read_approval(path)
    if record["approval_id"] != approval_id:
        raise ApprovalError("approval record ID does not match requested ID")
    current = time.time() if now is None else now
    claim_path = _claim_path(approvals, approval_id)
    if record["status"] == STATE_COMMITTED:
        if claim_path.exists():
            claim = _read_claim(claim_path)
            if not _pid_alive(claim["pid"]):
                _remove_claim(claim_path)
        return {"status": record["status"], "approval_id": approval_id}
    if record["status"] == STATE_FAILED:
        receipt = _read_receipt(_receipt_path(approvals, approval_id))
        if receipt is None:
            return {"status": STATE_FAILED, "approval_id": approval_id}
        if claim_path.exists():
            claim = _read_claim(claim_path)
            if _pid_alive(claim["pid"]):
                raise ApprovalError("approval claim is active; reconciliation is not safe")
            _remove_claim(claim_path)
        nonce = secrets.token_urlsafe(24)
        if not _write_claim(claim_path, approval_id, nonce, current):
            raise ApprovalError("approval claim was acquired concurrently")
        try:
            return _reconcile_committing(path, record, root, approvals, current)
        finally:
            if claim_path.exists():
                _remove_claim(claim_path)
    if record["status"] == STATE_PENDING:
        if not claim_path.exists():
            return {"status": STATE_PENDING, "approval_id": approval_id}
        claim = _read_claim(claim_path)
        if _pid_alive(claim["pid"]):
            raise ApprovalError("approval claim is active; reconciliation is not safe")
        _remove_claim(claim_path)
        _mark_failed(path, record, "claim existed before committing state was durable", current)
        return {"status": STATE_FAILED, "approval_id": approval_id}

    if claim_path.exists():
        claim = _read_claim(claim_path)
        if _pid_alive(claim["pid"]):
            raise ApprovalError("approval claim is active; reconciliation is not safe")
        _remove_claim(claim_path)
    nonce = secrets.token_urlsafe(24)
    if not _write_claim(claim_path, approval_id, nonce, current):
        raise ApprovalError("approval claim was acquired concurrently")
    try:
        return _reconcile_committing(path, record, root, approvals, current)
    finally:
        if claim_path.exists():
            _remove_claim(claim_path)


def prepare(
    plan: Any,
    *,
    adapter: Any,
    isolated_root: Path,
    approval_dir: Path,
    ttl_seconds: int = 300,
    now: float | None = None,
) -> dict[str, Any]:
    if not 1 <= ttl_seconds <= MAX_APPROVAL_TTL:
        raise ApprovalError("approval TTL is invalid")
    root, approvals = _ensure_isolated_paths(isolated_root, approval_dir)
    converted = _base_plan(plan)
    result = BASE.run_mode("test", converted, adapter=adapter, workspace_root=root)
    if not result.get("success"):
        raise ApprovalError("PREPARE failed; no approval was created")
    reports = [record for record in result.get("records", []) if record.get("status") == "would_write"]
    if len(reports) != 1:
        raise ApprovalError("PREPARE requires exactly one report artifact")
    report = reports[0]
    target = _safe_target(root, report["path"])
    if target.exists():
        raise ApprovalError("target already exists")
    if not target.parent.is_dir() or target.parent.is_symlink() or not os.access(target.parent, os.W_OK):
        raise ApprovalError("target directory is not writable")
    content = report.get("content")
    content_hash = _content_hash(content)
    if content_hash != report.get("sha256"):
        raise ApprovalError("prepared content hash mismatch")
    approval_id = secrets.token_urlsafe(32)
    created = time.time() if now is None else now
    record = {
        "version": VERSION,
        "approval_id": approval_id,
        "status": "pending",
        "created_at": created,
        "expires_at": created + ttl_seconds,
        "plan_sha256": _canonical_hash(plan),
        "content_sha256": content_hash,
        "target": report["path"],
        "content": content,
    }
    _atomic_json(_approval_path(approvals, approval_id), record)
    return {
        "status": "prepared",
        "approval_id": approval_id,
        "expires_at": record["expires_at"],
        "plan_sha256": record["plan_sha256"],
        "content_sha256": content_hash,
        "target": report["path"],
        "records": result["records"],
    }


def _atomic_create(target: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".prepared-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link creation is atomic and refuses to replace an existing file.
        os.link(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _receipt_bytes(record: dict[str, Any], target: str, content_hash: str, plan_hash: str, size: int) -> bytes:
    receipt = {
        "version": VERSION,
        "approval_id": record["approval_id"],
        "commit_nonce": record["commit_nonce"],
        "target": target,
        "plan_sha256": plan_hash,
        "content_sha256": content_hash,
        "sha256": content_hash,
        "bytes": size,
    }
    return json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def commit(
    approval_id: str,
    plan: Any,
    *,
    isolated_root: Path,
    approval_dir: Path,
    approved_target: str,
    approved_content_sha256: str,
    approved_plan_sha256: str,
    approve: bool = False,
    human_confirmation: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    if not approve or human_confirmation is None:
        raise ApprovalError("explicit human confirmation is required; --approve alone is insufficient")
    root, approvals = _ensure_isolated_paths(isolated_root, approval_dir)
    BASE.validate_plan(_base_plan(plan), root)
    path = _approval_path(approvals, approval_id)
    record = _read_approval(path)
    if record["approval_id"] != approval_id:
        raise ApprovalError("approval record ID does not match requested ID")
    current = time.time() if now is None else now
    expected_confirmation = f"APPROVE {approval_id} {approved_target} {approved_content_sha256}"
    if not secrets.compare_digest(human_confirmation, expected_confirmation):
        raise ApprovalError("human confirmation does not match the exact prepared artifact")
    if record["status"] == STATE_COMMITTED:
        raise ApprovalError("approval has already been committed")
    if record["status"] == STATE_FAILED:
        raise ApprovalError("approval requires reconciliation")
    if record["status"] == STATE_COMMITTING:
        if _claim_path(approvals, approval_id).exists():
            raise ApprovalError("approval is already committing; reconciliation required")
        raise ApprovalError("approval is committing; reconcile before retry")
    if current >= float(record["expires_at"]):
        _mark_failed(path, record, "approval expired before commit claim", current)
        raise ApprovalError("approval has expired")
    plan_hash = _canonical_hash(plan)
    if plan_hash != record["plan_sha256"] or plan_hash != approved_plan_sha256:
        raise ApprovalError("plan hash does not match approval")
    if approved_content_sha256 != record["content_sha256"]:
        raise ApprovalError("content hash does not match approval")
    if approved_target != record["target"]:
        raise ApprovalError("target does not match approval")
    target = _safe_target(root, record["target"])
    if target.exists() or not target.parent.is_dir() or target.parent.is_symlink() or not os.access(target.parent, os.W_OK):
        raise ApprovalError("target is unavailable or already exists")
    content = record["content"].encode("utf-8")
    if hashlib.sha256(content).hexdigest() != record["content_sha256"]:
        raise ApprovalError("approval content hash mismatch")
    claim_path = _claim_path(approvals, approval_id)
    nonce = secrets.token_urlsafe(24)
    if not _write_claim(claim_path, approval_id, nonce, current):
        raise ApprovalError("approval is already claimed; reconciliation required")
    record["status"] = STATE_COMMITTING
    record["commit_nonce"] = nonce
    record["commit_started_at"] = current
    try:
        _atomic_json(path, record)
        # Recheck all target properties after the durable claim and before creation.
        target = _safe_target(root, record["target"])
        if target.exists() or not target.parent.is_dir() or target.parent.is_symlink() or not os.access(target.parent, os.W_OK):
            raise ApprovalError("target is unavailable or already exists")
        receipt_path = _receipt_path(approvals, approval_id)
        if receipt_path.exists():
            raise ApprovalError("unexpected receipt already exists")
        _atomic_create(target, content)
        written_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if written_hash != record["content_sha256"]:
            raise ApprovalError("committed file hash mismatch")
        # The receipt is a durable commit marker. It is created without replace.
        _atomic_create(receipt_path, _receipt_bytes(record, record["target"], written_hash, record["plan_sha256"], len(content)))
        persisted_receipt = _read_receipt(receipt_path)
        if persisted_receipt is None or persisted_receipt.get("sha256") != written_hash:
            raise ApprovalError("receipt verification failed")
        record["status"] = "committed"
        record["committed_at"] = current
        _atomic_json(path, record)
    except Exception as exc:
        if record.get("status") == STATE_COMMITTING:
            try:
                _mark_failed(path, record, str(exc), current)
            except Exception:
                # Leave committing state and claim durable for explicit recovery.
                raise
        raise
    finally:
        if record.get("status") in {STATE_COMMITTED, STATE_FAILED} and claim_path.exists():
            _remove_claim(claim_path)
    return {
        "status": "committed",
        "approval_id": approval_id,
        "target": record["target"],
        "sha256": written_hash,
        "bytes": len(content),
        "plan_sha256": record["plan_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "dry_run", "prepare", "commit", "reconcile"))
    parser.add_argument("plan", type=Path)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--approval-dir", type=Path, default=None)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--approval-id")
    parser.add_argument("--target")
    parser.add_argument("--content-sha256")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--human-confirmation")
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if args.mode == "validate":
            result = {"mode": args.mode, **BASE.validate_plan(_base_plan(plan), args.workspace)}
            result["version"] = VERSION
        elif args.mode == "dry_run":
            result = BASE.run_mode("dry_run", {**plan, "version": BASE_VERSION}, workspace_root=args.workspace)
            result["mode"] = args.mode
            result["version"] = VERSION
        elif args.mode == "prepare":
            if args.token_file is None or args.approval_dir is None:
                raise ApprovalError("prepare requires an isolated credential file and approval directory")
            adapter = BASE.ADAPTER.create_isolated_adapter(args.token_file)
            result = prepare(plan, adapter=adapter, isolated_root=args.workspace, approval_dir=args.approval_dir)
        elif args.mode == "reconcile":
            if args.approval_dir is None or args.approval_id is None:
                raise ApprovalError("reconcile requires approval ID and approval directory")
            result = reconcile_approval(args.approval_id, isolated_root=args.workspace, approval_dir=args.approval_dir)
        else:
            required = (args.approval_dir, args.approval_id, args.target, args.content_sha256, args.plan_sha256, args.human_confirmation)
            if any(value is None for value in required):
                raise ApprovalError("commit requires approval ID, target, and both hashes")
            result = commit(
                args.approval_id,
                plan,
                isolated_root=args.workspace,
                approval_dir=args.approval_dir,
                approved_target=args.target,
                approved_content_sha256=args.content_sha256,
                approved_plan_sha256=args.plan_sha256,
                approve=args.approve,
                human_confirmation=args.human_confirmation,
            )
    except (OSError, json.JSONDecodeError, PlanValidationError, ApprovalError, ValueError) as exc:
        print(json.dumps({"mode": args.mode, "version": VERSION, "status": "failed", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.mode == "validate":
        return 0
    if args.mode == "dry_run":
        return 0 if result.get("success") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
