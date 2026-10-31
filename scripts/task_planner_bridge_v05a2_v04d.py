#!/usr/bin/env python3
"""Test-only bridge: v0.5a.2 planning -> v0.4d PREPARE.

This module is intentionally not an executor or approval API. It performs
planning, isolated retrieval/calculation through an injected/test adapter,
and v0.4d PREPARE only. COMMIT and reconciliation are never called.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import ntpath
import os
import posixpath
import secrets
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PLAN_PATH = SCRIPT_DIR / "task_planner_v05a2.py"
V04D_PATH = SCRIPT_DIR / "task_planner_v04d.py"
V04C_PATH = SCRIPT_DIR / "task_planner_v04c.py"
V05A_PATH = SCRIPT_DIR / "task_planner_v05a.py"
V05A_VERSION = "0.5a"
V04D_VERSION = "0.4d"
V04C_VERSION = "0.4c"
COMPILER_PREVIEW_TARGET = "output/task-plan-v05a2-preview.md"
TARGET_PREFIX = "output/task-plan-bridge-"
WINDOWS_TEST_TOKEN = r"E:\AI-Agent-Workspace\test-environment\test-token.txt"
LINUX_TEST_TOKEN = "/run/secrets/agent_knowledge_token"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PLANNER = _load(PLAN_PATH, "task_planner_v05a2")
V04D = _load(V04D_PATH, "task_planner_v04d")
V04C = _load(V04C_PATH, "task_planner_v04c")
V05A = _load(V05A_PATH, "task_planner_v05a")


class BridgeError(RuntimeError):
    pass


def compatibility_findings() -> dict[str, Any]:
    return {
        "planner_version": V05A_VERSION,
        "prepare_input_version": V04D_VERSION,
        "prepare_base_version": V04C_VERSION,
        "conversion": "deep-copy v0.5a plan, change compatibility envelope to v0.4d, then invoke v0.4d._base_plan and v0.4c.validate_plan",
        "preserved": ["operation names", "typed variables", "dependencies", "evidence extraction", "output/temp path rules"],
        "rejected": ["v0.5a plans with unsupported operations", "unexpected path targets", "missing report/verify pair", "v0.4d schema mismatch"],
    }


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _unique_target(isolated_root: Path) -> str:
    output = (isolated_root / "output").resolve()
    if not output.is_dir() or output.is_symlink():
        raise BridgeError("isolated output directory is unavailable")
    for _ in range(4):
        candidate = f"{TARGET_PREFIX}{secrets.token_hex(8)}.md"
        path = isolated_root / candidate.replace("/", "\\")
        if not path.exists() and _is_within(isolated_root.resolve(), path.parent.resolve()):
            return candidate
    raise BridgeError("could not allocate a unique isolated target")


def _validate_v05a_plan(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict) or plan.get("version") != V05A_VERSION:
        raise BridgeError("bridge requires a v0.5a compiled plan")
    validation = V05A.validate_plan(plan)
    if not validation.get("valid"):
        raise BridgeError("compiled plan failed the v0.5a validator")


def _adapt_plan(plan: dict[str, Any], isolated_root: Path) -> dict[str, Any]:
    _validate_v05a_plan(plan)
    adapted = copy.deepcopy(plan)
    report_steps = [step for step in adapted["steps"] if step.get("operation") == "write_report"]
    verify_steps = [step for step in adapted["steps"] if step.get("operation") == "verify_file"]
    if len(report_steps) != 1 or len(verify_steps) != 1:
        raise BridgeError("bridge requires exactly one write_report and one verify_file step")
    if report_steps[0]["args"].get("path") != COMPILER_PREVIEW_TARGET:
        raise BridgeError("compiled report target does not match the v0.5a.2 compiler target")
    if verify_steps[0]["args"].get("path") != COMPILER_PREVIEW_TARGET or verify_steps[0]["args"].get("reference") != report_steps[0]["id"]:
        raise BridgeError("compiled verification reference is incompatible with v0.4d")
    target = _unique_target(isolated_root)
    report_steps[0]["args"]["path"] = target
    verify_steps[0]["args"]["path"] = target

    adapted["version"] = V04D_VERSION
    try:
        base_plan = V04D._base_plan(adapted)
        validation = V04D.BASE.validate_plan(base_plan, isolated_root)
    except (ValueError, TypeError, V04D.PlanValidationError) as exc:
        raise BridgeError("planner/executor schema compatibility validation failed") from exc
    if not validation.get("valid"):
        raise BridgeError("planner/executor schema compatibility validation failed")
    return adapted



def _test_adapter(token_file: Path) -> Any:
    supplied_path = _validate_test_token_path(token_file)
    return V04C.ADAPTER.create_isolated_adapter(supplied_path)


def _expected_test_token(platform_name: str | None = None) -> str:
    return WINDOWS_TEST_TOKEN if (platform_name or os.name) == "nt" else LINUX_TEST_TOKEN


def _canonical_token_string(value: Path | str, platform_name: str | None = None) -> str:
    platform = platform_name or os.name
    raw = os.fspath(value)
    if platform == "nt":
        return ntpath.normcase(ntpath.abspath(raw))
    return posixpath.normpath(raw)


def _validate_test_token_path(token_file: Path | str, platform_name: str | None = None, require_existing: bool = True) -> Path | str:
    if not isinstance(token_file, (Path, str)):
        raise BridgeError("isolated test credential path is invalid")
    platform = platform_name or os.name
    expected = _expected_test_token(platform)
    if _canonical_token_string(token_file, platform) != _canonical_token_string(expected, platform):
        raise BridgeError("only the isolated test credential file is permitted")
    if not require_existing:
        return token_file
    token_path = Path(token_file)
    if token_path.is_symlink():
        raise BridgeError("isolated test credential symlink is not permitted")
    try:
        supplied_path = token_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BridgeError("isolated test credential is unavailable") from exc
    if not supplied_path.is_file():
        raise BridgeError("isolated test credential is not a regular file")
    return supplied_path



def prepare_from_plan(
    plan: dict[str, Any],
    *,
    isolated_root: Path,
    approval_dir: Path,
    adapter: Any,
) -> dict[str, Any]:
    if not callable(getattr(adapter, "search", None)):
        raise BridgeError("bridge requires a knowledge adapter with search()")
    adapted = _adapt_plan(plan, isolated_root.resolve())
    prepared = V04D.prepare(adapted, adapter=adapter, isolated_root=isolated_root, approval_dir=approval_dir)
    report = next((record for record in prepared.get("records", []) if record.get("status") == "would_write"), None)
    return {
        "valid": True,
        "execution_performed": False,
        "commit_performed": False,
        "compatibility": compatibility_findings(),
        "compiled_plan": adapted,
        "safe_preview": report,
        "prepare": prepared,
    }


def prepare_from_request(
    request: str,
    *,
    model_client: Any,
    isolated_root: Path,
    approval_dir: Path,
    adapter: Any | None = None,
    token_file: Path | None = None,
) -> dict[str, Any]:
    planning = PLANNER.generate_compile_validate(request, model_client)
    if not planning.get("valid"):
        return {
            "valid": False,
            "execution_performed": False,
            "commit_performed": False,
            "planning": planning,
            "compatibility": compatibility_findings(),
            "compiled_plan": None,
            "safe_preview": None,
            "prepare": None,
        }
    if adapter is None:
        if token_file is None:
            raise BridgeError("isolated token_file or injected adapter is required")
        adapter = _test_adapter(token_file)
    result = prepare_from_plan(planning["compiled_plan"], isolated_root=isolated_root, approval_dir=approval_dir, adapter=adapter)
    result["planning"] = planning
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, nargs="?", help="precompiled v0.5a plan JSON")
    parser.add_argument("--request", help="natural-language request for the fixed local LM Studio client")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--approval-dir", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--model", default="qwen2.5-7b-nerd-uncensored-v1.0")
    args = parser.parse_args(argv)
    try:
        adapter = _test_adapter(args.token_file)
        if bool(args.request) == bool(args.plan):
            raise BridgeError("provide exactly one of plan or --request")
        if args.request:
            result = prepare_from_request(
                args.request,
                model_client=PLANNER.IntentLMStudioClient(args.model),
                isolated_root=args.workspace,
                approval_dir=args.approval_dir,
                adapter=adapter,
            )
        else:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            result = prepare_from_plan(plan, isolated_root=args.workspace, approval_dir=args.approval_dir, adapter=adapter)
    except (OSError, json.JSONDecodeError, BridgeError, ValueError) as exc:
        print(json.dumps({"valid": False, "execution_performed": False, "commit_performed": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
