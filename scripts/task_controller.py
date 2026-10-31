#!/usr/bin/env python3
"""Deterministic, allowlisted local task runner (standard library only).

Usage:
    py task_controller.py run job_offer_plan.json
    py task_controller.py status job_offer_plan.json

Reads a JSON plan with supported steps:
  calculate: arithmetic on numeric named facts
  write_template: render a trusted plan template to output/temp
  verify_file: compare file contents and SHA256 to the earlier write

This is a local controller MVP, not a general-purpose LLM agent or remote tool runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from string import Formatter

SUPPORTED = {"calculate", "write_template", "verify_file"}
KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json_atomic(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def safe_target(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("A non-empty relative path is required")
    if relative.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", relative):
        raise ValueError("Absolute paths are not permitted")
    parts = relative.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0] not in {"output", "temp"}:
        raise ValueError("File targets must be under output/ or temp/")
    if any(p in {"", ".", ".."} for p in parts):
        raise ValueError("Invalid path component")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are not allowed")
    target = current.resolve()
    if root not in target.parents:
        raise ValueError("Path escapes the workspace")
    return target


def fmt_decimal(x: Decimal) -> str:
    return f"{x:.2f}"


def calculate(step: dict, values: dict):
    name = step["save_as"]
    if not KEY.fullmatch(name):
        raise ValueError("Invalid calculation result name")
    op = step["operation"]
    operands = step["operands"]
    if not isinstance(operands, list) or len(operands) != 2:
        raise ValueError("Calculations require exactly two operands")
    nums = []
    for operand in operands:
        val = values[operand] if isinstance(operand, str) and operand in values else operand
        try:
            number = Decimal(str(val))
        except (InvalidOperation, ValueError):
            raise ValueError(f"Invalid numeric operand: {operand}")
        if not number.is_finite():
            raise ValueError("Nonfinite operands are prohibited")
        nums.append(number)
    a, b = nums
    if op == "multiply":
        result = a * b
    elif op == "subtract":
        result = a - b
    elif op == "add":
        result = a + b
    elif op == "divide":
        if b == 0:
            raise ValueError("Division by zero")
        result = a / b
    else:
        raise ValueError(f"Unsupported calculation operation: {op}")
    if not result.is_finite():
        raise ValueError("Nonfinite result")
    values[name] = fmt_decimal(result)
    return f"{name}={values[name]}"


def render_template(template: str, values: dict):
    if not isinstance(template, str) or not template.strip():
        raise ValueError("Cannot write an empty template")
    for _, field_name, format_spec, conversion in Formatter().parse(template):
        if field_name is not None:
            if not KEY.fullmatch(field_name) or format_spec or conversion:
                raise ValueError("Only simple {variable} fields are supported")
            if field_name not in values:
                raise ValueError(f"Missing value for template field {field_name}")
    return template.format_map(values)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def execute(plan: dict, root: Path, state: dict, state_file: Path):
    values = dict(plan["facts"])
    if not isinstance(values, dict):
        raise ValueError("Plan facts must be an object")
    receipts = {}
    # Always rerun from the beginning; no blind skip based on stale status.
    # Creates/overwrites only plan-approved paths and verifies final bytes.
    state["status"] = "running"
    state["steps"] = []
    state["started_at"] = utc_now()
    write_json_atomic(state_file, state)
    for number, step in enumerate(plan["steps"], start=1):
        entry = {"number": number, "type": step.get("type"), "status": "running", "started_at": utc_now()}
        state["steps"].append(entry)
        write_json_atomic(state_file, state)
        try:
            kind = step["type"]
            if kind == "calculate":
                result = calculate(step, values)
            elif kind == "write_template":
                target = safe_target(root, step["path"])
                content = render_template(step["template"], values)
                data = content.encode("utf-8")
                if not data.strip() or len(data) > 1_000_000:
                    raise ValueError("Empty or oversized file is not permitted")
                if not target.parent.is_dir():
                    raise ValueError(f"Missing parent directory: {target.parent}")
                if target.exists() and not target.is_file():
                    raise ValueError("Target is not a regular file")
                if target.exists() and not step.get("overwrite", False):
                    raise ValueError("Target exists; overwrite was not approved")
                # Atomic replace, avoids half-written files on interruption.
                fd, tmp = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, target)
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                digest = sha256(data)
                receipts[step["path"]] = digest
                result = f"Wrote {step['path']} ({len(data)} bytes, sha256={digest})"
            elif kind == "verify_file":
                target = safe_target(root, step["path"])
                expected = receipts.get(step["path"])
                if expected is None:
                    raise ValueError("No successful write receipt for this path")
                if not target.is_file():
                    raise ValueError("Written file is missing")
                actual = sha256(target.read_bytes())
                if actual != expected:
                    raise ValueError("Content hash differs from write receipt")
                result = f"Verified {step['path']} (sha256={actual})"
            else:
                raise ValueError(f"Unsupported step: {kind}")
            entry.update(status="completed", finished_at=utc_now(), result=result)
            print(f"[{number}/{len(plan['steps'])}] OK: {result}")
        except Exception as exc:
            entry.update(status="failed", finished_at=utc_now(), error=str(exc))
            state.update(status="failed", finished_at=utc_now())
            write_json_atomic(state_file, state)
            print(f"[{number}/{len(plan['steps'])}] FAILED: {exc}", file=sys.stderr)
            return 1
        write_json_atomic(state_file, state)
    state.update(status="completed", finished_at=utc_now(), calculated_values=values)
    write_json_atomic(state_file, state)
    print(f"SUCCESS: completed {len(plan['steps'])} steps; verification receipt recorded.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "status"])
    parser.add_argument("plan", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("AGENT_WORKSPACE", r"E:\AI-Agent-Workspace")))
    args = parser.parse_args()
    root = args.workspace.resolve()
    plan_path = args.plan.resolve()
    if not plan_path.is_file():
        parser.error(f"Plan not found: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) or not isinstance(plan.get("facts"), dict):
        parser.error("Plan must have facts object and steps array")
    if len(plan["steps"]) > 25 or not plan["steps"]:
        parser.error("Plan must have 1 to 25 steps")
    if any(not isinstance(step, dict) or step.get("type") not in SUPPORTED for step in plan["steps"]):
        parser.error("Plan contains invalid step types")
    # State goes to workspace/temp, not the scripts folder.
    state_file = root / "temp" / ("task-state-" + sha256(str(plan_path).encode())[:12] + ".json")
    if args.action == "status":
        if not state_file.exists():
            print("No recorded execution for this plan")
            return 1
        print(state_file.read_text(encoding="utf-8"))
        return 0
    if not (root / "output").is_dir() or not (root / "temp").is_dir():
        parser.error("Workspace must contain existing output/ and temp/ directories")
    state = {"plan": str(plan_path), "workspace": str(root)}
    return execute(plan, root, state, state_file)


if __name__ == "__main__":
    raise SystemExit(main())
