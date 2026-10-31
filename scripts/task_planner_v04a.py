#!/usr/bin/env python3
"""Validate Dynamic Task Planner v0.4a plans without executing them.

The planner is deliberately a validation boundary. It accepts JSON-shaped
Python data or a JSON file through the CLI, validates a small allowlist of
operations, and reports a deterministic dependency order. It never invokes
the executor, performs network requests, or writes workspace files.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


VERSION = "0.4a"
MAX_STEPS = 10
MAX_QUERY_LENGTH = 1000
MAX_REPORT_LENGTH = 1_000_000
MAX_ID_LENGTH = 64
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STEP_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
ALLOWED_ROOTS = frozenset({"output", "temp"})
CALCULATIONS = frozenset({"add", "subtract", "multiply", "divide"})


class PlanValidationError(ValueError):
    """Raised when a plan violates the v0.4a validation contract."""


def _require_keys(value: dict[str, Any], required: set[str], allowed: set[str], label: str) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise PlanValidationError(f"{label} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise PlanValidationError(f"{label} contains unsupported fields: {', '.join(unknown)}")


def _require_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise PlanValidationError(f"{label} must be a valid identifier")
    return value


def validate_workspace_path(value: Any, label: str = "path") -> str:
    """Validate a relative path under the approved output/temp roots.

    Backslashes are rejected instead of normalized so Windows-style traversal
    cannot be interpreted differently by a later component.
    """
    if not isinstance(value, str) or not value or len(value) > 512:
        raise PlanValidationError(f"{label} must be a non-empty relative workspace path")
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise PlanValidationError(f"{label} must use a relative POSIX workspace path")
    if re.match(r"^[A-Za-z]:", value):
        raise PlanValidationError(f"{label} must not be an absolute drive path")
    parts = value.split("/")
    if len(parts) < 2 or parts[0] not in ALLOWED_ROOTS:
        raise PlanValidationError(f"{label} must be under output/ or temp/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PlanValidationError(f"{label} contains an invalid path component")
    if any(ord(char) < 32 for char in value):
        raise PlanValidationError(f"{label} contains a control character")
    return value


def _validate_search(args: Any) -> None:
    if not isinstance(args, dict):
        raise PlanValidationError("search_knowledge.args must be an object")
    _require_keys(args, {"query", "limit"}, {"query", "limit", "save_as"}, "search_knowledge.args")
    if not isinstance(args["query"], str) or not args["query"].strip() or len(args["query"]) > MAX_QUERY_LENGTH:
        raise PlanValidationError("search_knowledge.query must be a non-empty bounded string")
    if type(args["limit"]) is not int or not 1 <= args["limit"] <= 10:
        raise PlanValidationError("search_knowledge.limit must be an integer from 1 to 10")
    if "save_as" in args:
        _require_identifier(args["save_as"], "search_knowledge.save_as")


def _validate_calculate(args: Any) -> None:
    if not isinstance(args, dict):
        raise PlanValidationError("calculate.args must be an object")
    _require_keys(args, {"operation", "operands", "save_as"}, {"operation", "operands", "save_as"}, "calculate.args")
    if args["operation"] not in CALCULATIONS:
        raise PlanValidationError("calculate.operation is not approved")
    operands = args["operands"]
    if not isinstance(operands, list) or len(operands) != 2:
        raise PlanValidationError("calculate.operands must contain exactly two values")
    for operand in operands:
        if isinstance(operand, bool) or not isinstance(operand, (int, float, str)):
            raise PlanValidationError("calculate operands must be numbers or identifiers")
        if isinstance(operand, float) and not math.isfinite(operand):
            raise PlanValidationError("calculate operands must be finite")
        if isinstance(operand, str) and not IDENTIFIER.fullmatch(operand):
            raise PlanValidationError("calculate string operands must be identifiers")
    _require_identifier(args["save_as"], "calculate.save_as")


def _validate_write_report(args: Any) -> None:
    if not isinstance(args, dict):
        raise PlanValidationError("write_report.args must be an object")
    _require_keys(args, {"path", "content"}, {"path", "content", "overwrite"}, "write_report.args")
    validate_workspace_path(args["path"], "write_report.path")
    if not isinstance(args["content"], str) or not args["content"].strip():
        raise PlanValidationError("write_report.content must be a non-empty string")
    if len(args["content"].encode("utf-8")) > MAX_REPORT_LENGTH:
        raise PlanValidationError("write_report.content exceeds the size limit")
    if "overwrite" in args and type(args["overwrite"]) is not bool:
        raise PlanValidationError("write_report.overwrite must be a boolean")


def _validate_verify_file(args: Any) -> None:
    if not isinstance(args, dict):
        raise PlanValidationError("verify_file.args must be an object")
    _require_keys(args, {"path"}, {"path", "sha256"}, "verify_file.args")
    validate_workspace_path(args["path"], "verify_file.path")
    if "sha256" in args and (not isinstance(args["sha256"], str) or not SHA256.fullmatch(args["sha256"])):
        raise PlanValidationError("verify_file.sha256 must be a 64-character hexadecimal digest")


def _topological_order(steps: list[dict[str, Any]], dependencies: dict[str, list[str]]) -> list[str]:
    indegree = {step["id"]: len(dependencies[step["id"]]) for step in steps}
    dependents = {step["id"]: [] for step in steps}
    for step_id, parents in dependencies.items():
        for parent in parents:
            dependents[parent].append(step_id)

    ready = [step["id"] for step in steps if indegree[step["id"]] == 0]
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(order) != len(steps):
        raise PlanValidationError("steps contain a dependency cycle")
    return order


def validate_plan(plan: Any, workspace_root: Path | None = None) -> dict[str, Any]:
    """Validate a v0.4a plan and return a dry-run summary.

    ``workspace_root`` is intentionally used only to reject existing symlink
    components. No file is opened, created, modified, or deleted.
    """
    if not isinstance(plan, dict):
        raise PlanValidationError("plan must be a JSON object")
    _require_keys(plan, {"version", "steps"}, {"version", "steps"}, "plan")
    if plan["version"] != VERSION:
        raise PlanValidationError(f"plan.version must be {VERSION}")
    steps = plan["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise PlanValidationError(f"plan.steps must contain 1 to {MAX_STEPS} steps")

    ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for index, step in enumerate(steps, start=1):
        label = f"step {index}"
        if not isinstance(step, dict):
            raise PlanValidationError(f"{label} must be an object")
        _require_keys(step, {"id", "operation", "args"}, {"id", "operation", "args", "depends_on"}, label)
        step_id = step["id"]
        if not isinstance(step_id, str) or not STEP_ID.fullmatch(step_id) or len(step_id) > MAX_ID_LENGTH:
            raise PlanValidationError(f"{label}.id is invalid")
        if step_id in ids:
            raise PlanValidationError(f"duplicate step id: {step_id}")
        ids.add(step_id)
        operation = step["operation"]
        validators = {
            "search_knowledge": _validate_search,
            "calculate": _validate_calculate,
            "write_report": _validate_write_report,
            "verify_file": _validate_verify_file,
        }
        if operation not in validators:
            raise PlanValidationError(f"{label}.operation is not approved")
        validators[operation](step["args"])

        raw_dependencies = step.get("depends_on", [])
        if not isinstance(raw_dependencies, list) or any(not isinstance(item, str) for item in raw_dependencies):
            raise PlanValidationError(f"{label}.depends_on must be an array of step IDs")
        if len(raw_dependencies) != len(set(raw_dependencies)):
            raise PlanValidationError(f"{label}.depends_on contains duplicates")
        dependencies[step_id] = raw_dependencies

    for step_id, parents in dependencies.items():
        unknown = sorted(set(parents) - ids)
        if unknown:
            raise PlanValidationError(f"step {step_id} depends on unknown step: {unknown[0]}")

    if workspace_root is not None:
        root = workspace_root.resolve()
        for step in steps:
            args = step["args"]
            if step["operation"] in {"write_report", "verify_file"}:
                relative = args["path"]
                current = root
                for component in relative.split("/"):
                    current = current / component
                    if current.is_symlink():
                        raise PlanValidationError(f"{step['id']} path contains a symlink")

    order = _topological_order(steps, dependencies)
    return {
        "valid": True,
        "version": VERSION,
        "step_count": len(steps),
        "execution_order": order,
        "operations": [step["operation"] for step in steps],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="JSON plan to validate")
    parser.add_argument("--workspace", type=Path, default=None, help="Optional workspace root for symlink checks")
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        result = validate_plan(plan, args.workspace)
    except (OSError, json.JSONDecodeError, PlanValidationError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
