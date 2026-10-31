#!/usr/bin/env python3
"""Dynamic Task Planner v0.4c modes: validate, dry_run, and isolated test."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any


VERSION = "0.4c"
BASE_VERSION = "0.4b"
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_PATH = SCRIPT_DIR / "task_planner_v04b.py"
ADAPTER_PATH = SCRIPT_DIR / "knowledge_adapter_v04c.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = _load(BASE_PATH, "task_planner_v04b")
ADAPTER = _load(ADAPTER_PATH, "knowledge_adapter_v04c")
PlanValidationError = BASE.PlanValidationError


def _concise_records(result: dict[str, Any]) -> dict[str, Any]:
    for record in result.get("records", []):
        fact = record.get("fact")
        if not isinstance(fact, dict) or "evidence" not in fact:
            continue
        fact["evidence_excerpt"] = fact.get("evidence_excerpt") or fact["evidence"][:240]
        fact.pop("evidence", None)
    return result


def _base_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("version") != VERSION:
        raise PlanValidationError(f"plan.version must be {VERSION}")
    converted = dict(plan)
    converted["version"] = BASE_VERSION
    return converted


def validate_plan(plan: Any, workspace_root: Path | None = None) -> dict[str, Any]:
    result = dict(BASE.validate_plan(_base_plan(plan), workspace_root))
    result["version"] = VERSION
    return result


def run_mode(
    mode: str,
    plan: Any,
    *,
    token_file: Path | None = None,
    workspace_root: Path | None = None,
    adapter: Any | None = None,
) -> dict[str, Any]:
    validation = validate_plan(plan, workspace_root)
    if mode == "validate":
        return {"mode": mode, **validation}
    if mode == "dry_run":
        result = BASE.dry_run_plan(_base_plan(plan), workspace_root=workspace_root)
        _concise_records(result)
        result["mode"] = mode
        result["version"] = VERSION
        return result
    if mode != "test":
        raise ValueError("mode must be validate, dry_run, or test")
    if adapter is None:
        if token_file is None:
            raise ADAPTER.KnowledgeAdapterError("Test mode requires an isolated credential file")
        adapter = ADAPTER.create_isolated_adapter(token_file)
    result = BASE.dry_run_plan(_base_plan(plan), knowledge_fetcher=adapter.search, workspace_root=workspace_root)
    _concise_records(result)
    result["mode"] = mode
    result["version"] = VERSION
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "dry_run", "test"))
    parser.add_argument("plan", type=Path)
    parser.add_argument("--token-file", type=Path, default=None, help="Read-only isolated test credential")
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        result = run_mode(args.mode, plan, token_file=args.token_file, workspace_root=args.workspace)
    except (OSError, json.JSONDecodeError, PlanValidationError, ADAPTER.KnowledgeAdapterError, ValueError) as exc:
        print(json.dumps({"mode": args.mode, "version": VERSION, "valid": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result.get("valid", False):
        return 2
    if args.mode == "validate":
        return 0
    return 0 if result.get("success", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
