#!/usr/bin/env python3
"""Deterministic LM Studio generation pipeline for Task Planner v0.5a.1.

This is planning-only. Python owns parsing, validation, completeness checks,
and the maximum two-generation retry policy. No adapter, executor, approval,
filesystem write, or production component is imported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from scripts import task_planner_v05a as validator


VERSION = "0.5a.1"
LM_STUDIO_ENDPOINT = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen2.5-7b-nerd-uncensored-v1.0"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_GENERATIONS = 2
CODE_PATTERN = re.compile(r"(?i)(?:\.repeat\s*\(|\b(?:eval|exec|__import__)\s*\(|\b(?:os\.system|subprocess)\b|javascript:|cmd\.exe|powershell\b)")
NO_DUPLICATE_KEYS = object()


class PipelineError(RuntimeError):
    code = "pipeline_error"


class LMStudioUnavailable(PipelineError):
    code = "lm_studio_unavailable"


class StructuredOutputUnsupported(PipelineError):
    code = "structured_output_unsupported"


class ModelOutputError(PipelineError):
    code = "model_output_invalid"


class UnsupportedTask(PipelineError):
    code = "unsupported_task"


def _duplicate_key_error(pairs: list[tuple[str, Any]]) -> Any:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


def parse_model_json(content: str | bytes) -> dict[str, Any]:
    if isinstance(content, bytes):
        raw = content
    else:
        raw = content.encode("utf-8")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelOutputError("model output exceeds the response limit")
    try:
        text = raw.decode("utf-8")
        if CODE_PATTERN.search(text) or "```" in text:
            raise ModelOutputError("model output contains executable or fenced content")
        parsed = json.loads(
            text,
            object_pairs_hook=_duplicate_key_error,
            parse_constant=_reject_constant,
        )
    except ModelOutputError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ModelOutputError("model output is not strict JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelOutputError("model output must be a JSON object")
    if _contains_code(parsed):
        raise ModelOutputError("model output contains executable content")
    return parsed


def _contains_code(value: Any) -> bool:
    if isinstance(value, str):
        return bool(CODE_PATTERN.search(value))
    if isinstance(value, dict):
        return any(_contains_code(key) or _contains_code(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_code(item) for item in value)
    return False


def _json_schema() -> dict[str, Any]:
    """Strict response schema compatible with OpenAI-style LM Studio APIs."""
    identifier = {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"}
    step_id = {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"}
    variable = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type"],
        "properties": {"type": {"enum": ["number", "search_results"]}, "unit": {"type": "string", "maxLength": 64}},
    }
    step = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "operation", "args"],
        "properties": {
            "id": step_id,
            "operation": {"enum": ["search_knowledge", "extract_number", "calculate", "write_report", "verify_file"]},
            "depends_on": {"type": "array", "uniqueItems": True, "items": step_id},
            "args": {"type": "object"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["version", "variables", "steps"],
        "properties": {
            "version": {"const": "0.5a"},
            "variables": {"type": "object", "minProperties": 1, "additionalProperties": variable},
            "steps": {"type": "array", "minItems": 1, "maxItems": 10, "items": step},
        },
    }


class LMStudioClient:
    """Fixed-endpoint LM Studio client with structured-output negotiation."""

    def __init__(self, model: str = DEFAULT_MODEL, opener: Callable[..., Any] | None = None, timeout: float = 30.0) -> None:
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError("model name is invalid")
        if not 1.0 <= timeout <= 60.0:
            raise ValueError("timeout must be between 1 and 60 seconds")
        self.model = model
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout

    def generate(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "task_plan_v05a", "strict": True, "schema": _json_schema()},
            },
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise PipelineError("generation request exceeds the request limit")
        request = urllib.request.Request(
            LM_STUDIO_ENDPOINT,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 404, 422}:
                raise StructuredOutputUnsupported("LM Studio/model rejected the structured JSON response format") from None
            raise LMStudioUnavailable("LM Studio request failed") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise LMStudioUnavailable("LM Studio is unavailable") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelOutputError("LM Studio response exceeds the response limit")
        try:
            response_payload = json.loads(raw.decode("utf-8"))
            choice = response_payload["choices"][0]
            message = choice["message"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise ModelOutputError("LM Studio returned an invalid chat response") from None
        if choice.get("finish_reason") == "length":
            raise ModelOutputError("model output was truncated")
        if message.get("refusal"):
            raise ModelOutputError("model refused to generate a plan")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelOutputError("model returned empty plan content")
        return content


@dataclass(frozen=True)
class Requirements:
    operations: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    calculations: tuple[tuple[str, str, tuple[str, ...]], ...] = ()


def requirements_from_request(request: str) -> Requirements:
    if not isinstance(request, str) or not request.strip() or len(request.encode("utf-8")) > 4000:
        raise UnsupportedTask("request is empty or exceeds the request limit")
    lowered = request.lower()
    unsupported_markers = ("shell", "powershell", "command", "delete files", "send email", "arbitrary url", "browse the web")
    if any(marker in lowered for marker in unsupported_markers):
        raise UnsupportedTask("request is outside the planning capability catalog")
    if all(term in lowered for term in ("wage", "housing", "weekly")) or "job offer" in lowered:
        return Requirements(
            operations=("search_knowledge", "extract_number", "calculate", "write_report", "verify_file"),
            fields=("hourly_wage", "weekly_hours", "housing_per_day"),
            calculations=(
                ("weekly_gross", "multiply", ("hourly_wage", "weekly_hours")),
                ("weekly_housing", "multiply", ("housing_per_day", "7")),
                ("difference", "subtract", ("weekly_gross", "weekly_housing")),
            ),
        )
    if "calculate" in lowered and "report" in lowered:
        return Requirements(operations=("calculate", "write_report", "verify_file"))
    raise UnsupportedTask("request cannot be represented by the supported planning operations")


def check_completeness(plan: dict[str, Any], requirements: Requirements) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    operations = {step["operation"] for step in plan["steps"]}
    for operation in requirements.operations:
        if operation not in operations:
            errors.append({"code": "incomplete_task", "message": f"required operation is missing: {operation}"})
    variables = plan["variables"]
    steps = plan["steps"]
    for field in requirements.fields:
        if variables.get(field, {}).get("type") != "number":
            errors.append({"code": "incomplete_task", "message": f"required numeric field is missing: {field}"})
        if not any(step["operation"] == "extract_number" and step["args"].get("field") == field for step in steps):
            errors.append({"code": "incomplete_task", "message": f"required extraction is missing: {field}"})
    for name, operation, operands in requirements.calculations:
        if not any(
            step["operation"] == "calculate"
            and step["args"].get("save_as") == name
            and step["args"].get("operation") == operation
            and tuple(str(item) for item in step["args"].get("operands", [])) == operands
            for step in steps
        ):
            errors.append({"code": "incomplete_task", "message": f"required calculation is missing: {name}"})
    return errors


def _base_result() -> dict[str, Any]:
    return {"valid": False, "attempts": [], "errors": [], "plan": None, "safe_preview": None, "execution_performed": False}


def _error(exc: BaseException) -> dict[str, str]:
    code = getattr(exc, "code", "generation_error")
    return {"code": code, "message": " ".join(str(exc).split())[:240] or "generation failed"}


def generate_and_validate(
    request: str,
    client: LMStudioClient | Any,
    requirements: Requirements | None = None,
) -> dict[str, Any]:
    result = _base_result()
    try:
        required = requirements or requirements_from_request(request)
    except UnsupportedTask as exc:
        result["errors"] = [_error(exc)]
        return result

    system = (
        "Return only one JSON object matching the supplied task-plan schema. "
        "Use only approved operations. Never output JavaScript, Python, shell, URLs, markdown, or comments."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": request},
    ]
    previous_output_hash: str | None = None
    previous_plan_hash: str | None = None
    last_plan: dict[str, Any] | None = None
    last_errors: list[dict[str, str]] = []
    for generation in range(1, MAX_GENERATIONS + 1):
        try:
            content = client.generate(messages)
            output_hash = hashlib.sha256(content.encode("utf-8") if isinstance(content, str) else content).hexdigest()
            if output_hash == previous_output_hash:
                attempt = {"number": generation, "valid": False, "plan_sha256": output_hash, "errors": [{"code": "identical_plan", "message": "model repeated identical output; no further retry"}]}
                result["attempts"].append(attempt)
                last_errors = attempt["errors"]
                break
            previous_output_hash = output_hash
            plan = parse_model_json(content)
            plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            attempt: dict[str, Any] = {"number": generation, "plan_sha256": plan_hash}
            if plan_hash == previous_plan_hash:
                attempt["valid"] = False
                attempt["errors"] = [{"code": "identical_plan", "message": "model repeated the previous invalid plan; no further retry"}]
                result["attempts"].append(attempt)
                last_errors = attempt["errors"]
                break
            previous_plan_hash = plan_hash
            last_plan = plan
            structural = validator.validate_plan(plan)
            if not structural["valid"]:
                errors = structural.get("errors", [{"code": "invalid_plan", "message": "plan validation failed"}])
            else:
                errors = check_completeness(plan, required)
            attempt["valid"] = not errors
            attempt["errors"] = errors
            result["attempts"].append(attempt)
            if not errors:
                result["valid"] = True
                result["plan"] = plan
                result["safe_preview"] = validator.preview_plan(plan)
                result["errors"] = []
                return result
            last_errors = errors
            if generation < MAX_GENERATIONS:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps({"request": request, "previous_plan": plan, "validation_errors": errors}, ensure_ascii=False)},
                ]
        except PipelineError as exc:
            error = _error(exc)
            result["attempts"].append({"number": generation, "valid": False, "errors": [error]})
            last_errors = [error]
            if isinstance(exc, (LMStudioUnavailable, StructuredOutputUnsupported)):
                break
            if generation < MAX_GENERATIONS:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps({"request": request, "validation_errors": [error], "instruction": "Generate a new plan; do not repeat the previous output."})},
                ]
        except Exception:
            error = {"code": "generation_error", "message": "controlled plan generation failed"}
            result["attempts"].append({"number": generation, "valid": False, "errors": [error]})
            last_errors = [error]
            break
    result["plan"] = last_plan
    result["errors"] = last_errors
    if last_plan is not None:
        preview = validator.preview_plan(last_plan)
        result["safe_preview"] = preview if preview.get("valid") else None
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    result = generate_and_validate(args.request, LMStudioClient(args.model))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
