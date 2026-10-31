#!/usr/bin/env python3
"""Validate and safely simulate Dynamic Task Planner v0.4b plans.

This module is a dry-run-only execution boundary. It validates typed plans and
can simulate search, numeric extraction, calculations, report rendering, and
verification entirely in memory. It never starts processes, writes files, or
opens network connections. Tests may inject a mocked knowledge fetcher.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from string import Formatter
from typing import Any, Callable


VERSION = "0.4b"
MAX_STEPS = 10
MAX_QUERY_LENGTH = 1000
MAX_REPORT_LENGTH = 1_000_000
MAX_EVIDENCE_LENGTH = 40_000
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STEP_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
ALLOWED_ROOTS = frozenset({"output", "temp"})
CALCULATIONS = frozenset({"add", "subtract", "multiply", "divide"})
VARIABLE_TYPES = frozenset({"number", "search_results"})
REPORT_FACT_SUFFIXES = frozenset({"source", "location", "chunk_id", "evidence_excerpt", "unit"})


class PlanValidationError(ValueError):
    """Raised when a v0.4b plan is structurally or semantically invalid."""


class PlanExecutionError(ValueError):
    """Raised internally for a failed dry-run operation."""


def _require_keys(value: dict[str, Any], required: set[str], allowed: set[str], label: str) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise PlanValidationError(f"{label} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise PlanValidationError(f"{label} contains unsupported fields: {', '.join(unknown)}")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise PlanValidationError(f"{label} must be a valid identifier")
    return value


def validate_workspace_path(value: Any, label: str = "path") -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise PlanValidationError(f"{label} must be a non-empty relative workspace path")
    if "\x00" in value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise PlanValidationError(f"{label} must be a relative POSIX workspace path")
    parts = value.split("/")
    if len(parts) < 2 or parts[0] not in ALLOWED_ROOTS:
        raise PlanValidationError(f"{label} must be under output/ or temp/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PlanValidationError(f"{label} contains an invalid path component")
    if any(ord(char) < 32 for char in value):
        raise PlanValidationError(f"{label} contains a control character")
    return value


def _validate_variables(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise PlanValidationError("plan.variables must be a non-empty object")
    variables: dict[str, str] = {}
    for name, spec in raw.items():
        _identifier(name, "variable name")
        if not isinstance(spec, dict):
            raise PlanValidationError(f"variable {name} must be an object")
        _require_keys(spec, {"type"}, {"type", "unit"}, f"variable {name}")
        if spec["type"] not in VARIABLE_TYPES:
            raise PlanValidationError(f"variable {name} has an unsupported type")
        if "unit" in spec and (not isinstance(spec["unit"], str) or not spec["unit"].strip() or len(spec["unit"]) > 64):
            raise PlanValidationError(f"variable {name}.unit is invalid")
        variables[name] = spec["type"]
    return variables


def _validate_search(args: Any, variables: dict[str, str]) -> None:
    if not isinstance(args, dict):
        raise PlanValidationError("search_knowledge.args must be an object")
    _require_keys(args, {"query", "limit", "save_as"}, {"query", "limit", "save_as"}, "search_knowledge.args")
    if not isinstance(args["query"], str) or not args["query"].strip() or len(args["query"]) > MAX_QUERY_LENGTH:
        raise PlanValidationError("search_knowledge.query must be a non-empty bounded string")
    if type(args["limit"]) is not int or not 1 <= args["limit"] <= 10:
        raise PlanValidationError("search_knowledge.limit must be an integer from 1 to 10")
    name = _identifier(args["save_as"], "search_knowledge.save_as")
    if variables.get(name) != "search_results":
        raise PlanValidationError(f"{name} must be declared as search_results")


def _validate_extract(args: Any, variables: dict[str, str]) -> None:
    if not isinstance(args, dict):
        raise PlanValidationError("extract_number.args must be an object")
    _require_keys(
        args,
        {"from", "field", "source", "patterns", "save_as"},
        {"from", "field", "source", "patterns", "save_as", "unit", "refine_query", "refine_limit"},
        "extract_number.args",
    )
    _identifier(args["from"], "extract_number.from")
    field = _identifier(args["field"], "extract_number.field")
    save_as = _identifier(args["save_as"], "extract_number.save_as")
    if field != save_as:
        raise PlanValidationError("extract_number.field and save_as must match")
    if variables.get(save_as) != "number":
        raise PlanValidationError(f"{save_as} must be declared as number")
    source = args["source"]
    if not isinstance(source, str) or not source.strip() or "/" in source or "\\" in source or len(source) > 512:
        raise PlanValidationError("extract_number.source must be one bounded document name")
    patterns = args["patterns"]
    if not isinstance(patterns, list) or not 1 <= len(patterns) <= 3:
        raise PlanValidationError("extract_number.patterns must contain 1 to 3 patterns")
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 350:
            raise PlanValidationError("extract_number patterns must be bounded strings")
        try:
            compiled = re.compile(pattern, flags=re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            raise PlanValidationError("extract_number contains an invalid regular expression") from exc
        if compiled.groups != 1:
            raise PlanValidationError("each extraction pattern must have exactly one capture group")
    if "unit" in args and (not isinstance(args["unit"], str) or not args["unit"].strip() or len(args["unit"]) > 64):
        raise PlanValidationError("extract_number.unit is invalid")
    if "refine_query" in args:
        if not isinstance(args["refine_query"], str) or not args["refine_query"].strip() or len(args["refine_query"]) > MAX_QUERY_LENGTH:
            raise PlanValidationError("extract_number.refine_query is invalid")
        if "refine_limit" not in args or type(args["refine_limit"]) is not int or not 1 <= args["refine_limit"] <= 10:
            raise PlanValidationError("extract_number.refine_limit must be an integer from 1 to 10")
    elif "refine_limit" in args:
        raise PlanValidationError("extract_number.refine_limit requires refine_query")


def _validate_calculate(args: Any, variables: dict[str, str]) -> None:
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
        if isinstance(operand, str):
            name = _identifier(operand, "calculate operand")
            if variables.get(name) != "number":
                raise PlanValidationError(f"calculate operand {name} is not a number")
    name = _identifier(args["save_as"], "calculate.save_as")
    if variables.get(name) != "number":
        raise PlanValidationError(f"{name} must be declared as number")


def _placeholders(content: str) -> list[str]:
    found: list[str] = []
    try:
        parsed = Formatter().parse(content)
        for _, field, format_spec, conversion in parsed:
            if field is None:
                continue
            if format_spec or conversion or not IDENTIFIER.fullmatch(field):
                raise PlanValidationError("write_report placeholders must be simple variable names")
            found.append(field)
    except ValueError as exc:
        raise PlanValidationError("write_report.content has invalid placeholders") from exc
    return found


def _placeholder_base(name: str, variables: dict[str, str]) -> tuple[str, str | None] | None:
    if name in variables:
        return name, None
    for suffix in REPORT_FACT_SUFFIXES:
        marker = "_" + suffix
        if name.endswith(marker):
            base = name[: -len(marker)]
            if variables.get(base) == "number":
                return base, suffix
    return None


def _validate_write_report(args: Any, variables: dict[str, str]) -> None:
    if not isinstance(args, dict):
        raise PlanValidationError("write_report.args must be an object")
    _require_keys(args, {"path", "content"}, {"path", "content", "overwrite"}, "write_report.args")
    validate_workspace_path(args["path"], "write_report.path")
    if not isinstance(args["content"], str) or not args["content"].strip():
        raise PlanValidationError("write_report.content must be a non-empty string")
    if len(args["content"].encode("utf-8")) > MAX_REPORT_LENGTH:
        raise PlanValidationError("write_report.content exceeds the size limit")
    for name in _placeholders(args["content"]):
        if _placeholder_base(name, variables) is None:
            raise PlanValidationError(f"write_report placeholder {name} must reference a number or fact field")
    if "overwrite" in args and type(args["overwrite"]) is not bool:
        raise PlanValidationError("write_report.overwrite must be a boolean")


def _validate_verify(args: Any) -> None:
    if not isinstance(args, dict):
        raise PlanValidationError("verify_file.args must be an object")
    _require_keys(args, {"path", "reference"}, {"path", "reference", "sha256"}, "verify_file.args")
    validate_workspace_path(args["path"], "verify_file.path")
    _identifier(args["reference"], "verify_file.reference")
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


def _ancestors(step_id: str, dependencies: dict[str, list[str]], memo: dict[str, set[str]]) -> set[str]:
    if step_id in memo:
        return memo[step_id]
    result: set[str] = set()
    for parent in dependencies[step_id]:
        result.add(parent)
        result.update(_ancestors(parent, dependencies, memo))
    memo[step_id] = result
    return result


def validate_plan(plan: Any, workspace_root: Path | None = None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise PlanValidationError("plan must be a JSON object")
    _require_keys(plan, {"version", "variables", "steps"}, {"version", "variables", "steps"}, "plan")
    if plan["version"] != VERSION:
        raise PlanValidationError(f"plan.version must be {VERSION}")
    variables = _validate_variables(plan["variables"])
    steps = plan["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise PlanValidationError(f"plan.steps must contain 1 to {MAX_STEPS} steps")

    validators = {
        "search_knowledge": lambda args: _validate_search(args, variables),
        "extract_number": lambda args: _validate_extract(args, variables),
        "calculate": lambda args: _validate_calculate(args, variables),
        "write_report": lambda args: _validate_write_report(args, variables),
        "verify_file": _validate_verify,
    }
    ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for index, step in enumerate(steps, start=1):
        label = f"step {index}"
        if not isinstance(step, dict):
            raise PlanValidationError(f"{label} must be an object")
        _require_keys(step, {"id", "operation", "args"}, {"id", "operation", "args", "depends_on"}, label)
        step_id = step["id"]
        if not isinstance(step_id, str) or not STEP_ID.fullmatch(step_id) or step_id in ids:
            raise PlanValidationError(f"{label}.id is invalid or duplicated")
        operation = step["operation"]
        if operation not in validators:
            raise PlanValidationError(f"{label}.operation is not approved")
        validators[operation](step["args"])
        raw_dependencies = step.get("depends_on", [])
        if not isinstance(raw_dependencies, list) or any(not isinstance(item, str) for item in raw_dependencies):
            raise PlanValidationError(f"{label}.depends_on must be an array of step IDs")
        if len(raw_dependencies) != len(set(raw_dependencies)):
            raise PlanValidationError(f"{label}.depends_on contains duplicates")
        ids.add(step_id)
        dependencies[step_id] = raw_dependencies
        by_id[step_id] = step

    for step_id, parents in dependencies.items():
        unknown = sorted(set(parents) - ids)
        if unknown:
            raise PlanValidationError(f"step {step_id} depends on unknown step: {unknown[0]}")
    order = _topological_order(steps, dependencies)
    ancestor_memo: dict[str, set[str]] = {}
    producers: dict[str, str] = {}
    for step in steps:
        args = step["args"]
        if step["operation"] in {"search_knowledge", "extract_number", "calculate"}:
            name = args["save_as"]
            if name in producers:
                raise PlanValidationError(f"variable {name} is produced more than once")
            producers[name] = step["id"]
        if step["operation"] == "extract_number":
            source_step = by_id.get(args["from"])
            if source_step is None or source_step["operation"] != "search_knowledge":
                raise PlanValidationError(f"extract_number.from must reference search_knowledge: {args['from']}")
            if args["from"] not in _ancestors(step["id"], dependencies, ancestor_memo):
                raise PlanValidationError(f"{step['id']} must depend on {args['from']}")
        if step["operation"] == "calculate":
            for operand in args["operands"]:
                if isinstance(operand, str):
                    producer = producers.get(operand)
                    if producer is None or producer not in _ancestors(step["id"], dependencies, ancestor_memo):
                        raise PlanValidationError(f"{step['id']} must depend on numeric variable {operand}")
        if step["operation"] == "write_report":
            for name in _placeholders(args["content"]):
                placeholder = _placeholder_base(name, variables)
                if placeholder is None:
                    raise PlanValidationError(f"{step['id']} has an invalid report placeholder: {name}")
                base, suffix = placeholder
                producer = producers.get(base)
                if producer is None or producer not in _ancestors(step["id"], dependencies, ancestor_memo):
                    raise PlanValidationError(f"{step['id']} must depend on report variable {base}")
                if suffix is not None and by_id[producer]["operation"] != "extract_number":
                    raise PlanValidationError(f"{name} is only available for extracted numeric facts")
        if step["operation"] == "verify_file":
            reference = by_id.get(args["reference"])
            if reference is None or reference["operation"] != "write_report":
                raise PlanValidationError(f"{step['id']}.reference must identify write_report")
            if reference["args"]["path"] != args["path"]:
                raise PlanValidationError(f"{step['id']} path does not match its report reference")
            if args["reference"] not in _ancestors(step["id"], dependencies, ancestor_memo):
                raise PlanValidationError(f"{step['id']} must depend on {args['reference']}")

    if workspace_root is not None:
        root = workspace_root.resolve()
        for step in steps:
            if step["operation"] in {"write_report", "verify_file"}:
                current = root
                for component in step["args"]["path"].split("/"):
                    current = current / component
                    if current.is_symlink():
                        raise PlanValidationError(f"{step['id']} path contains a symlink")

    return {
        "valid": True,
        "version": VERSION,
        "variable_types": variables,
        "step_count": len(steps),
        "execution_order": order,
        "operations": [step["operation"] for step in steps],
    }


def extract_numeric_fact(results: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Extract one corroborated finite number using v0.3-style safeguards."""
    if not isinstance(results, list):
        raise PlanExecutionError("knowledge search results are missing")
    source = args["source"]
    candidates: list[dict[str, Any]] = []
    source_matches = 0
    usable_evidence = 0
    for result in results:
        if not isinstance(result, dict) or result.get("source") != source:
            continue
        source_matches += 1
        evidence = result.get("text")
        location = result.get("location")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > MAX_EVIDENCE_LENGTH:
            continue
        if not isinstance(location, str) or not location.strip():
            continue
        usable_evidence += 1
        for pattern in args["patterns"]:
            regex = re.compile(pattern, flags=re.IGNORECASE | re.DOTALL)
            matches = list(regex.finditer(evidence))
            if len(matches) > 1:
                raise PlanExecutionError(f"ambiguous numeric evidence for {args['field']}")
            if not matches:
                continue
            try:
                match = matches[0]
                value = Decimal(match.group(1))
            except (InvalidOperation, ValueError) as exc:
                raise PlanExecutionError(f"nonnumeric evidence for {args['field']}") from exc
            if not value.is_finite() or value < 0:
                raise PlanExecutionError(f"numeric evidence for {args['field']} is not finite and nonnegative")
            candidates.append({
                "value": value,
                "source": source,
                "field": args["field"],
                "unit": args.get("unit"),
                "evidence": evidence,
                "evidence_excerpt": _matched_excerpt(evidence, match),
                "location": location,
                "chunk_id": str(result.get("chunk_id", ""))[:120],
            })
            break
    if not candidates:
        if source_matches == 0:
            detail = "source document was not returned"
        elif usable_evidence == 0:
            detail = "source document returned no usable evidence"
        else:
            detail = "numeric pattern did not match source evidence"
        raise PlanExecutionError(f"no matching numeric evidence for {args['field']} in {source}: {detail}")
    values = {candidate["value"] for candidate in candidates}
    if len(values) != 1:
        raise PlanExecutionError(f"conflicting numeric evidence for {args['field']}")
    candidates.sort(key=lambda item: (item["location"], item["chunk_id"], item["evidence"]))
    selected = candidates[0]
    selected["type"] = "number"
    selected["value"] = f"{selected['value']:.2f}"
    selected["supporting_evidence_count"] = len(candidates)
    return selected


def _matched_excerpt(evidence: str, match: re.Match[str]) -> str:
    """Return nearby field context, stopping before unrelated later text."""
    group_start, group_end = match.span(1)
    before = evidence[max(0, group_start - 48):group_start]
    after = evidence[group_end:group_end + 64]
    boundary = re.search(r"[.!?;\r\n]", after)
    if boundary:
        after = after[:boundary.end()]
    excerpt = " ".join((before + match.group(1) + after).split())
    return excerpt[:120]


def _number(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PlanExecutionError(f"{name} is not numeric") from exc
    if not number.is_finite():
        raise PlanExecutionError(f"{name} is not finite")
    return number


def _report_values(variables: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, value in variables.items():
        if not isinstance(value, dict) or value.get("type") != "number":
            continue
        values[name] = str(value["value"])
        for suffix in REPORT_FACT_SUFFIXES:
            if suffix in value and value[suffix] is not None:
                values[f"{name}_{suffix}"] = str(value[suffix])
    return values


def dry_run_plan(
    plan: Any,
    knowledge_fetcher: Callable[[str, int], dict[str, Any]] | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Simulate a validated plan in memory and return structured records."""
    validation = validate_plan(plan, workspace_root)
    steps_by_id = {step["id"]: step for step in plan["steps"]}
    outputs: dict[str, Any] = {}
    variables: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    for step_id in validation["execution_order"]:
        step = steps_by_id[step_id]
        operation = step["operation"]
        if any(next(record for record in records if record["id"] == parent)["status"] not in {"simulated_success", "would_write", "would_verify"} for parent in step.get("depends_on", [])):
            records.append({"id": step_id, "operation": operation, "status": "blocked", "error": "dependency did not complete"})
            continue
        try:
            args = step["args"]
            if operation == "search_knowledge":
                if knowledge_fetcher is None:
                    records.append({"id": step_id, "operation": operation, "status": "not_executed", "reason": "no offline fetcher supplied"})
                    outputs[step_id] = None
                    continue
                response = knowledge_fetcher(args["query"], args["limit"])
                if not isinstance(response, dict) or response.get("status") != "success" or not isinstance(response.get("results"), list):
                    raise PlanExecutionError("knowledge fetcher returned an invalid response")
                outputs[step_id] = response["results"]
                variables[args["save_as"]] = response["results"]
                records.append({"id": step_id, "operation": operation, "status": "simulated_success", "result_count": len(response["results"])})
            elif operation == "extract_number":
                results = outputs.get(args["from"])
                if results is None:
                    raise PlanExecutionError("source search was not executed")
                try:
                    fact = extract_numeric_fact(results, args)
                except PlanExecutionError as exc:
                    # One bounded, field-focused refinement is allowed only
                    # when the initial search has no matching evidence.
                    if "no matching numeric evidence" not in str(exc) or "refine_query" not in args or knowledge_fetcher is None:
                        raise
                    refined = knowledge_fetcher(args["refine_query"], args["refine_limit"])
                    if not isinstance(refined, dict) or refined.get("status") != "success" or not isinstance(refined.get("results"), list):
                        raise PlanExecutionError("refinement search returned an invalid response")
                    fact = extract_numeric_fact(refined["results"], args)
                outputs[step_id] = fact
                variables[args["save_as"]] = fact
                public_fact = {key: value for key, value in fact.items() if key != "value"}
                public_fact["value"] = fact["value"]
                records.append({"id": step_id, "operation": operation, "status": "simulated_success", "fact": public_fact})
            elif operation == "calculate":
                operands = []
                for operand in args["operands"]:
                    operands.append(_number(variables[operand]["value"], operand) if isinstance(operand, str) else _number(operand, "literal"))
                a, b = operands
                result = {"add": a + b, "subtract": a - b, "multiply": a * b, "divide": a / b if b != 0 else None}[args["operation"]]
                if result is None or not result.is_finite():
                    raise PlanExecutionError("calculation produced an invalid result")
                fact = {"type": "number", "field": args["save_as"], "value": f"{result:.2f}"}
                variables[args["save_as"]] = fact
                outputs[step_id] = fact
                records.append({"id": step_id, "operation": operation, "status": "simulated_success", "value": fact["value"]})
            elif operation == "write_report":
                content = args["content"].format_map(_report_values(variables))
                data = content.encode("utf-8")
                receipt = {"path": args["path"], "sha256": hashlib.sha256(data).hexdigest(), "content": content}
                outputs[step_id] = receipt
                records.append({"id": step_id, "operation": operation, "status": "would_write", "path": args["path"], "sha256": receipt["sha256"], "bytes": len(data), "content": content})
            elif operation == "verify_file":
                receipt = outputs.get(args["reference"])
                if not isinstance(receipt, dict) or receipt.get("path") != args["path"]:
                    raise PlanExecutionError("report reference has no simulated write receipt")
                if args.get("sha256") and args["sha256"].lower() != receipt["sha256"].lower():
                    raise PlanExecutionError("simulated file hash does not match expected hash")
                records.append({"id": step_id, "operation": operation, "status": "would_verify", "path": args["path"], "sha256": receipt["sha256"]})
        except (PlanExecutionError, ZeroDivisionError, KeyError) as exc:
            records.append({"id": step_id, "operation": operation, "status": "failed", "error": str(exc)})
    return {
        "valid": True,
        "dry_run": True,
        "execution_performed": knowledge_fetcher is not None,
        "success": all(record["status"] in {"simulated_success", "would_write", "would_verify"} for record in records),
        "execution_order": validation["execution_order"],
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        result = dry_run_plan(plan, workspace_root=args.workspace)
    except (OSError, json.JSONDecodeError, PlanValidationError) as exc:
        print(json.dumps({"valid": False, "dry_run": True, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
