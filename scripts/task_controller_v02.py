#!/usr/bin/env python3
"""Deterministic, allowlisted local task runner (standard library only).

Usage:
    py task_controller.py run job_offer_plan.json
    py task_controller.py status job_offer_plan.json

Reads a JSON plan with supported steps:
  lookup_number: retrieve one numeric fact from an authenticated local knowledge search
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
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from string import Formatter

SUPPORTED = {"lookup_number", "calculate", "write_template", "verify_file"}
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


# Execute inside the already-running knowledge-service container. Its own
# KNOWLEDGE_TOKEN environment variable is used in-container and never printed.
# No host port needs publishing, and no secret is stored in this plan.
IN_CONTAINER_SEARCH = r"""
import json, os, sys, urllib.request, urllib.error
query, limit = sys.argv[1], int(sys.argv[2])
token = os.environ.get('KNOWLEDGE_TOKEN', '')
if not token:
    raise SystemExit('Knowledge token missing in container')
payload = json.dumps({'query': query, 'limit': limit}).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8002/search', data=payload,
    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
try:
    with urllib.request.urlopen(req, timeout=55) as response:
        print(response.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    raise SystemExit('Knowledge search returned HTTP ' + str(e.code))
"""


def fetch_knowledge(query: str, limit: int) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('Invalid knowledge query')
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 10):
        raise ValueError('Search limit must be between 1 and 10')
    try:
        completed = subprocess.run(
            ['docker', 'exec', 'knowledge-service', 'python', '-c',
             IN_CONTAINER_SEARCH, query, str(limit)],
            capture_output=True, text=True, timeout=80, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Unable to reach knowledge-service through Docker') from exc
    if completed.returncode != 0:
        # Do not surface raw stderr; it could contain implementation details.
        raise RuntimeError('Knowledge-service request failed; check Docker service health')
    try:
        data = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError('Invalid knowledge-service JSON response') from exc
    if not isinstance(data, dict) or data.get('status') != 'success' or not isinstance(data.get('results'), list):
        raise RuntimeError('Knowledge-service did not return successful results')
    return data


def lookup_number(step: dict, values: dict, fetch=None) -> str:
    name = step['save_as']
    if not isinstance(name, str) or not KEY.fullmatch(name):
        raise ValueError('Invalid fact name')
    if name in values:
        raise ValueError('Fact already exists; cannot overwrite')
    query, source = step['query'], step['source']
    patterns = step['patterns']
    if not isinstance(source, str) or not source.strip() or '/' in source or '\\' in source:
        raise ValueError('Source must be one filename')
    if not isinstance(patterns, list) or not (1 <= len(patterns) <= 3) or any(not isinstance(p, str) or len(p) > 350 for p in patterns):
        raise ValueError('Provide 1 to 3 bounded extraction patterns')
    regexes = [re.compile(p, flags=re.IGNORECASE | re.DOTALL) for p in patterns]
    for regex in regexes:
        if regex.groups != 1:
            raise ValueError('Each pattern must capture exactly one numeric value')
    data = (fetch or fetch_knowledge)(query, step.get('limit', 8))
    matches = []
    for result in data['results']:
        if not isinstance(result, dict) or result.get('source') != source:
            continue
        chunk = result.get('text', '')
        if not isinstance(chunk, str) or len(chunk) > 40000:
            continue
        for regex in regexes:
            m = regex.search(chunk)
            if m:
                try:
                    value = Decimal(m.group(1))
                except (InvalidOperation, ValueError) as exc:
                    raise ValueError('Retrieved value is not a number') from exc
                if not value.is_finite() or value < 0:
                    raise ValueError('Retrieved value must be finite and nonnegative')
                matches.append((value, str(result.get('location', 'Unknown'))[:100], str(result.get('chunk_id', ''))[:120]))
                break
    if not matches:
        raise ValueError(f'Could not verify {name} in search results from {source}')
    unique = {m[0] for m in matches}
    if len(unique) != 1:
        raise ValueError(f'Conflicting values retrieved for {name}; refusing to guess')
    value, location, chunk_id = matches[0]
    values[name] = str(value)
    values[name + '_location'] = location
    return f'{name}={value} (source={source}; location={location}; chunk_id={chunk_id})'


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
            if kind == "lookup_number":
                result = lookup_number(step, values)
            elif kind == "calculate":
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
