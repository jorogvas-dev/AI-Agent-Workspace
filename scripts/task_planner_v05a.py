#!/usr/bin/env python3
"""Planning-only natural-language task-plan validator for v0.5a.

This module accepts untrusted JSON produced by a model, reuses the v0.4c/b
validators, and creates a deterministic non-persistent preview. It never
retrieves knowledge, writes files, creates approvals, starts processes, or
loads the v0.4d execution boundary.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path
from typing import Any


VERSION = "0.5a"
BASE_VERSION = "0.4c"
MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 32 * 1024
SCRIPT_DIR = Path(__file__).resolve().parent
UNSAFE_REGEX = re.compile(r"(?:\\(?:[1-9]|g<)|\(\?<=[^)]*\)|\(\?<![^)]*\)|\(\?R\)|\(\?0\))")
NESTED_QUANTIFIER = re.compile(r"\([^()]{0,120}[+*][^()]{0,120}\)[+*]")


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = _load(SCRIPT_DIR / "task_planner_v04c.py", "task_planner_v04c")
VALIDATOR = BASE.BASE


def _sanitize(message: Any) -> str:
    text = " ".join(str(message).split())
    text = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", text)
    return text[:240] or "Plan validation failed"


def _error_code(message: str) -> str:
    lowered = message.lower()
    if "json" in lowered or "object" in lowered:
        return "malformed_json"
    if "unsupported" in lowered or "not approved" in lowered:
        return "unsupported_operation"
    if "cycle" in lowered or "depend" in lowered:
        return "invalid_dependencies"
    if "path" in lowered or "symlink" in lowered:
        return "unsafe_path"
    if "pattern" in lowered or "extract" in lowered or "numeric" in lowered:
        return "invalid_extraction"
    if "step" in lowered or "size" in lowered or "limit" in lowered:
        return "plan_limits"
    return "invalid_plan"


def _parse_untrusted(value: Any, label: str) -> tuple[Any | None, dict[str, Any] | None]:
    try:
        if isinstance(value, bytes):
            raw = value
        elif isinstance(value, str):
            raw = value.encode("utf-8")
        elif isinstance(value, dict):
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        else:
            return None, {"valid": False, "errors": [{"code": "malformed_json", "message": f"{label} must be a JSON object or JSON text"}]}
        if len(raw) > MAX_INPUT_BYTES:
            return None, {"valid": False, "errors": [{"code": "input_too_large", "message": f"{label} exceeds the {MAX_INPUT_BYTES}-byte limit"}]}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, {"valid": False, "errors": [{"code": "malformed_json", "message": _sanitize(exc)}]}
        if not isinstance(parsed, dict):
            return None, {"valid": False, "errors": [{"code": "malformed_json", "message": f"{label} must be a JSON object"}]}
        return parsed, None
    except (TypeError, ValueError, UnicodeError) as exc:
        return None, {"valid": False, "errors": [{"code": "malformed_json", "message": _sanitize(exc)}]}


def _as_base_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("version") != VERSION:
        raise BASE.PlanValidationError(f"plan.version must be {VERSION}")
    converted = dict(plan)
    converted["version"] = BASE_VERSION
    return converted


def _validate_model_regex_safety(plan: dict[str, Any]) -> None:
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or step.get("operation") != "extract_number":
            continue
        for pattern in step.get("args", {}).get("patterns", []):
            if isinstance(pattern, str) and (UNSAFE_REGEX.search(pattern) or NESTED_QUANTIFIER.search(pattern)):
                raise BASE.PlanValidationError("extraction pattern uses a disallowed regex construct")


def _evaluate(value: Any, label: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    parsed, parse_error = _parse_untrusted(value, label)
    if parse_error is not None:
        parse_error["version"] = VERSION
        return parse_error, None
    try:
        _validate_model_regex_safety(parsed)
        result = dict(BASE.validate_plan(_as_base_plan(parsed)))
        result["version"] = VERSION
        return {"valid": True, "version": VERSION, "validation": result}, parsed
    except (BASE.PlanValidationError, ValueError, TypeError) as exc:
        message = _sanitize(exc)
        return {
            "valid": False,
            "version": VERSION,
            "errors": [{"code": _error_code(message), "message": message}],
        }, parsed


def validate_plan(proposed_plan: Any, correction: Any | None = None) -> dict[str, Any]:
    """Validate once, with at most one separately supplied correction attempt."""
    first, first_plan = _evaluate(proposed_plan, "proposed_plan")
    if first["valid"] or correction is None:
        first["correction_attempted"] = False
        return first

    second, second_plan = _evaluate(correction, "correction")
    return {
        "valid": second["valid"],
        "version": VERSION,
        "correction_attempted": True,
        "initial_validation": first,
        "correction_validation": second,
        "selected_plan": "correction" if second["valid"] else None,
    }


def capability_catalog() -> dict[str, Any]:
    """Build the catalog from the validator functions actually in use."""
    operation_validators = (
        ("search_knowledge", "_validate_search"),
        ("extract_number", "_validate_extract"),
        ("calculate", "_validate_calculate"),
        ("write_report", "_validate_write_report"),
        ("verify_file", "_validate_verify"),
    )
    operations = [
        {"name": name, "validator": validator}
        for name, validator in operation_validators
        if callable(getattr(VALIDATOR, validator, None))
    ]
    return {
        "version": VERSION,
        "operations": operations,
        "limits": {
            "max_plan_bytes": MAX_INPUT_BYTES,
            "max_steps": VALIDATOR.MAX_STEPS,
            "workspace_roots": sorted(VALIDATOR.ALLOWED_ROOTS),
            "calculations": sorted(VALIDATOR.CALCULATIONS),
        },
        "execution": "validation and in-memory preview only",
        "unsupported": ["shell", "python", "arbitrary_http", "persistent_write"],
    }


def _preview_for(plan: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    steps_by_id = {step["id"]: step for step in plan["steps"]}
    preview_steps = []
    for step_id in validation["validation"]["execution_order"]:
        step = steps_by_id[step_id]
        args = step["args"]
        item: dict[str, Any] = {
            "id": step_id,
            "operation": step["operation"],
            "depends_on": list(step.get("depends_on", [])),
            "status": "not_executed",
        }
        if step["operation"] == "search_knowledge":
            item["query_length"] = len(args["query"])
            item["result_variable"] = args["save_as"]
            item["network"] = "not contacted"
        elif step["operation"] == "extract_number":
            item.update({"field": args["field"], "source": args["source"], "pattern_count": len(args["patterns"])})
        elif step["operation"] == "calculate":
            item.update({"operation": "calculate", "arithmetic": args["operation"], "result_variable": args["save_as"]})
        elif step["operation"] == "write_report":
            item.update({"path": args["path"], "bytes": len(args["content"].encode("utf-8")), "effect": "rendered in memory only"})
        elif step["operation"] == "verify_file":
            item.update({"path": args["path"], "reference": args["reference"], "effect": "hash comparison only"})
        preview_steps.append(item)
    return {
        "steps": preview_steps,
        "persistent_effects": [],
        "network_effects": [],
        "approval_records": [],
        "execution": "not performed",
    }


def preview_plan(proposed_plan: Any, correction: Any | None = None) -> dict[str, Any]:
    """Return validation plus a deterministic, non-persistent preview."""
    outcome = validate_plan(proposed_plan, correction)
    if not outcome["valid"]:
        return outcome
    selected = correction if outcome.get("selected_plan") == "correction" else proposed_plan
    parsed, parse_error = _parse_untrusted(selected, "selected_plan")
    if parse_error is not None or parsed is None:
        return {"valid": False, "version": VERSION, "errors": [{"code": "malformed_json", "message": "selected plan could not be parsed"}]}
    selected_validation = outcome.get("correction_validation") or outcome.get("validation")
    if isinstance(selected_validation, dict) and "validation" in selected_validation:
        selected_validation = selected_validation["validation"]
    return {
        **outcome,
        "capabilities": capability_catalog(),
        "preview": _preview_for(parsed, {"validation": selected_validation}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capabilities", "validate", "preview"))
    parser.add_argument("plan", nargs="?")
    parser.add_argument("--correction")
    args = parser.parse_args(argv)
    if args.mode == "capabilities":
        print(json.dumps(capability_catalog(), sort_keys=True))
        return 0
    if args.plan is None:
        print(json.dumps({"valid": False, "errors": [{"code": "missing_input", "message": "plan JSON is required"}]}))
        return 2
    def cli_input(value: str | None) -> str | bytes | None:
        if value is None:
            return None
        candidate = Path(value)
        if candidate.is_file():
            return candidate.read_bytes()
        return value

    plan_input = cli_input(args.plan)
    correction_input = cli_input(args.correction)
    result = preview_plan(plan_input, correction_input) if args.mode == "preview" else validate_plan(plan_input, correction_input)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("valid") else 2


if __name__ == "__main__":
    raise SystemExit(main())
