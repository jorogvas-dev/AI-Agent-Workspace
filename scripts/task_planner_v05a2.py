#!/usr/bin/env python3
"""Bounded intent-to-plan compiler for Task Planner v0.5a.2.

Qwen emits a small intent document. Python validates that intent, compiles it
from fixed capability registries, revalidates the generated v0.5a plan, and
returns a non-persistent preview. No retrieval, writes, approvals, or
execution are performed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from scripts import task_planner_v05a as plan_validator


VERSION = "0.5a.2"
INTENT_VERSION = "0.5a.2-intent"
LM_STUDIO_WINDOWS_ENDPOINT = "http://localhost:1234/v1/chat/completions"
LM_STUDIO_DOCKER_ENDPOINT = "http://host.docker.internal:1234/v1/chat/completions"
LM_STUDIO_ENDPOINT = LM_STUDIO_WINDOWS_ENDPOINT
DEFAULT_MODEL = "qwen2.5-7b-nerd-uncensored-v1.0"
MAX_INTENT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_GENERATIONS = 2
CODE_PATTERN = re.compile(r"(?i)(?:\.repeat\s*\(|\b(?:eval|exec|__import__)\s*\(|\b(?:os\.system|subprocess)\b|javascript:|cmd\.exe|powershell\b)")


def resolve_lm_studio_endpoint(value: str | None = None) -> str:
    endpoint = value or os.environ.get("LM_STUDIO_ENDPOINT", LM_STUDIO_WINDOWS_ENDPOINT)
    if endpoint not in {LM_STUDIO_WINDOWS_ENDPOINT, LM_STUDIO_DOCKER_ENDPOINT}:
        raise ValueError("LM_STUDIO_ENDPOINT is not an approved local endpoint")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port != 1234 or parsed.path != "/v1/chat/completions":
        raise ValueError("LM_STUDIO_ENDPOINT has an invalid form")
    if parsed.hostname not in {"localhost", "host.docker.internal"}:
        raise ValueError("LM_STUDIO_ENDPOINT host is not approved")
    return endpoint


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not permitted", headers, fp)


class IntentError(ValueError):
    code = "invalid_intent"


class LMError(RuntimeError):
    code = "lm_studio_error"


class StructuredOutputUnsupported(LMError):
    code = "structured_output_unsupported"


class LMUnavailable(LMError):
    code = "lm_studio_unavailable"


class ModelOutputError(LMError):
    code = "model_output_invalid"


@dataclass(frozen=True)
class ExtractionCapability:
    source: str
    unit: str
    pattern: str
    refine_query: str
    query_terms: str
    label: str


@dataclass(frozen=True)
class CalculationCapability:
    operation: str
    operands: tuple[str, ...]
    unit: str
    required_fields: tuple[str, ...] = ()
    required_calculations: tuple[str, ...] = ()
    required_assumptions: tuple[str, ...] = ()


EXTRACTION_REGISTRY: dict[str, ExtractionCapability] = {
    "hourly_wage": ExtractionCapability(
        source="sample.pdf",
        unit="USD/hour",
        pattern=r"(\d+(?:\.\d+)?)\s*\$\s*per\s*hour",
        refine_query="sample.pdf hourly wage per hour",
        query_terms="hourly wage",
        label="Hourly wage",
    ),
    "weekly_hours": ExtractionCapability(
        source="sample.pdf",
        unit="hours/week",
        pattern=r"(\d+(?:\.\d+)?)\s+hours\s+per\s+week",
        refine_query="sample.pdf hours per week",
        query_terms="hours per week",
        label="Weekly hours",
    ),
    "housing_per_day": ExtractionCapability(
        source="sample.pdf",
        unit="USD/day",
        pattern=r"housing\s+cost\s*[:\-]?\s*\$?\s*(\d+(?:\.\d+)?)(?:(?!\d).){0,160}?per\s+day",
        refine_query="sample.pdf housing cost per day",
        query_terms="housing cost per day",
        label="Housing",
    ),
}

CALCULATION_REGISTRY: dict[str, CalculationCapability] = {
    "weekly_gross": CalculationCapability("multiply", ("hourly_wage", "weekly_hours"), "USD/week", required_fields=("hourly_wage", "weekly_hours")),
    "weekly_housing": CalculationCapability("multiply", ("housing_per_day", "days_per_week"), "USD/week", required_fields=("housing_per_day",), required_assumptions=("days_per_week",)),
    "difference": CalculationCapability("subtract", ("weekly_gross", "weekly_housing"), "USD/week", required_calculations=("weekly_gross", "weekly_housing")),
}


def calculation_dependency_closure(requested: list[str] | tuple[str, ...]) -> tuple[list[str], set[str], set[str]]:
    ordered: list[str] = []
    fields: set[str] = set()
    assumptions: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in ordered:
            return
        if name in visiting:
            raise IntentError("calculation dependency cycle")
        capability = CALCULATION_REGISTRY.get(name)
        if capability is None:
            raise IntentError(f"unsupported calculation: {name}")
        visiting.add(name)
        for prerequisite in capability.required_calculations:
            visit(prerequisite)
        visiting.remove(name)
        ordered.append(name)
        fields.update(capability.required_fields)
        assumptions.update(capability.required_assumptions)

    for name in requested:
        visit(name)
    return ordered, fields, assumptions


def _sanitize(message: Any) -> str:
    return " ".join(str(message).split())[:240] or "intent validation failed"


def _parse_strict(value: str | bytes) -> dict[str, Any]:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    if len(raw) > MAX_INTENT_BYTES:
        raise IntentError("intent exceeds the 64 KiB limit")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise IntentError("intent contains duplicate fields")
            result[key] = item
        return result

    def constant(value: str) -> Any:
        raise IntentError(f"non-standard JSON value is not allowed: {value}")

    try:
        text = raw.decode("utf-8")
        if CODE_PATTERN.search(text) or "```" in text:
            raise IntentError("intent contains code or fenced content")
        parsed = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except IntentError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntentError("intent is not strict JSON") from exc
    if not isinstance(parsed, dict):
        raise IntentError("intent must be a JSON object")
    if _contains_code(parsed):
        raise IntentError("intent contains executable content")
    return parsed


def _contains_code(value: Any) -> bool:
    if isinstance(value, str):
        return bool(CODE_PATTERN.search(value))
    if isinstance(value, dict):
        return any(_contains_code(key) or _contains_code(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_code(item) for item in value)
    return False


def intent_json_schema() -> dict[str, Any]:
    field_names = sorted(EXTRACTION_REGISTRY)
    calculation_names = sorted(CALCULATION_REGISTRY)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent_version", "document", "calculations", "report", "assumptions"],
        "properties": {
            "intent_version": {"const": INTENT_VERSION},
            "document": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source"],
                "properties": {"source": {"enum": ["sample.pdf"]}},
            },
            "fields": {"type": "array", "maxItems": 3, "items": {"enum": field_names}},
            "calculations": {
                "type": "array",
                "maxItems": 3,
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name"],
                    "properties": {"name": {"enum": calculation_names}},
                },
            },
            "report": {
                "type": "object",
                "additionalProperties": False,
                "required": ["requested"],
                "properties": {
                    "requested": {"type": "boolean"},
                    "include_fields": {"type": "array", "items": {"enum": field_names}},
                    "include_calculations": {"type": "array", "items": {"enum": calculation_names}},
                },
            },
            "assumptions": {
                "type": "array",
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "value", "unit"],
                    "properties": {
                        "name": {"const": "days_per_week"},
                        "value": {"const": 7},
                        "unit": {"const": "days/week"},
                    },
                },
            },
        },
    }


def _require_keys(value: dict[str, Any], required: set[str], allowed: set[str], label: str) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise IntentError(f"{label} missing required fields: {', '.join(missing)}")
    if unknown:
        raise IntentError(f"{label} has unsupported fields: {', '.join(unknown)}")


def validate_intent(intent: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(intent, dict):
        raise IntentError("intent must be an object")
    _require_keys(intent, {"intent_version", "document", "calculations", "report", "assumptions"}, {"intent_version", "document", "fields", "calculations", "report", "assumptions"}, "intent")
    if intent["intent_version"] != INTENT_VERSION:
        raise IntentError(f"intent_version must be {INTENT_VERSION}")
    document = intent["document"]
    if not isinstance(document, dict):
        raise IntentError("document must be an object")
    _require_keys(document, {"source"}, {"source"}, "document")
    if document["source"] != "sample.pdf":
        raise IntentError("document source is not supported")
    raw_fields = intent.get("fields", [])
    if not isinstance(raw_fields, list) or len(raw_fields) > 3:
        raise IntentError("fields must contain at most 3 supported fields")
    fields: list[str] = []
    for field in raw_fields:
        if field not in fields:
            fields.append(field)
    if any(field not in EXTRACTION_REGISTRY for field in fields):
        raise IntentError("fields contains an unsupported document field")

    calculations = intent["calculations"]
    if not isinstance(calculations, list) or len(calculations) > 3:
        raise IntentError("calculations must contain at most 3 items")
    calculation_names: list[str] = []
    for item in calculations:
        if not isinstance(item, dict):
            raise IntentError("calculation entries must be objects")
        _require_keys(item, {"name"}, {"name"}, "calculation")
        name = item["name"]
        if name not in CALCULATION_REGISTRY or name in calculation_names:
            raise IntentError("calculations contains an unsupported or duplicate calculation")
        calculation_names.append(name)

    assumptions = intent["assumptions"]
    if not isinstance(assumptions, list) or len(assumptions) > 1:
        raise IntentError("assumptions must contain at most one item")
    assumption_names = set()
    for item in assumptions:
        if not isinstance(item, dict):
            raise IntentError("assumption entries must be objects")
        _require_keys(item, {"name", "value", "unit"}, {"name", "value", "unit"}, "assumption")
        if item != {"name": "days_per_week", "value": 7, "unit": "days/week"}:
            raise IntentError("only the verified days_per_week=7 assumption is supported")
        assumption_names.add(item["name"])

    report = intent["report"]
    if not isinstance(report, dict):
        raise IntentError("report must be an object")
    _require_keys(report, {"requested"}, {"requested", "include_fields", "include_calculations"}, "report")
    report_fields = report.get("include_fields", [])
    report_calculations = report.get("include_calculations", [])
    if type(report["requested"]) is not bool or not isinstance(report_fields, list) or not isinstance(report_calculations, list):
        raise IntentError("report fields are invalid")
    if any(field not in EXTRACTION_REGISTRY for field in report_fields):
        raise IntentError("report.include_fields contains an unsupported field")
    if any(name not in CALCULATION_REGISTRY for name in report_calculations):
        raise IntentError("report.include_calculations contains an unsupported calculation")
    if not report["requested"] and (report_fields or report_calculations):
        raise IntentError("non-requested reports cannot include report content")

    calculation_order, derived_fields, derived_assumptions = calculation_dependency_closure(calculation_names)
    if not derived_assumptions.issubset(assumption_names):
        raise IntentError("one or more calculation assumptions are missing")
    effective_fields = set(fields) | derived_fields
    if not set(report_fields).issubset(effective_fields):
        raise IntentError("report.include_fields contains a field not requested or derived")
    if not set(report_calculations).issubset(set(calculation_names) | set(calculation_order)):
        raise IntentError("report.include_calculations contains an unavailable calculation")
    return {"valid": True, "intent_version": INTENT_VERSION, "fields": list(fields), "calculations": calculation_names, "report_requested": report["requested"]}


def _term_is_excluded(text: str, start: int) -> bool:
    prefix = text[max(0, start - 70):start]
    return bool(re.search(r"(?:do\s+not|don't|does\s+not|without|exclude(?:d|ing)?|no|omit(?:ted|ting)?|skip(?:ped|ping)?)\s+(?:calculate\s+|include\s+|extract\s+|retrieve\s+|report\s+|the\s+)*$", prefix))


def _positive_term(text: str, patterns: tuple[str, ...]) -> bool:
    return any(not _term_is_excluded(text, match.start()) for pattern in patterns for match in re.finditer(pattern, text))


def _excluded_term(text: str, patterns: tuple[str, ...]) -> bool:
    return any(_term_is_excluded(text, match.start()) for pattern in patterns for match in re.finditer(pattern, text))


def _explicit_calculations(text: str) -> tuple[set[str], set[str]]:
    patterns = {
        "weekly_gross": (r"weekly\s+gross",),
        "weekly_housing": (r"weekly\s+housing", r"housing\s+costs?"),
        "difference": (r"difference", r"remaining\s+after"),
    }
    positive: set[str] = set()
    excluded: set[str] = set()
    for name, terms in patterns.items():
        for term in terms:
            for match in re.finditer(term, text):
                (excluded if _term_is_excluded(text, match.start()) else positive).add(name)
    return positive, excluded


def _only_calculations(text: str) -> set[str]:
    found: set[str] = set()
    for match in re.finditer(r"\bonly\b([^.!?;\n]{0,120})", text):
        scope = match.group(1)
        positive, _excluded = _explicit_calculations(scope)
        found.update(positive)
    return found


def _job_request_requirements(request: str) -> tuple[set[str], set[str], bool]:
    lowered = request.lower()
    positive_calculations, excluded_calculations = _explicit_calculations(lowered)
    only_calculations = _only_calculations(lowered)
    if only_calculations and (positive_calculations - only_calculations):
        raise IntentError("request contradicts its only-calculation restriction")
    if positive_calculations & excluded_calculations:
        raise IntentError("request both requires and excludes a calculation")
    selected_calculations = only_calculations or positive_calculations

    field_terms = {
        "hourly_wage": (r"hourly\s+wage", r"wage\s+per\s+hour"),
        "weekly_hours": (r"weekly\s+hours", r"hours\s+per\s+week"),
        "housing_per_day": (r"housing\s+per\s+day", r"daily\s+housing", r"housing\s+costs?"),
    }
    positive_fields = {field for field, patterns in field_terms.items() if _positive_term(lowered, patterns)}
    housing_positive = "housing_per_day" in positive_fields or "weekly_housing" in selected_calculations
    housing_excluded = _excluded_term(lowered, (r"housing",)) or "weekly_housing" in excluded_calculations
    if housing_positive and housing_excluded:
        raise IntentError("request both requires and excludes housing")

    if only_calculations:
        required_calculations = only_calculations
    elif selected_calculations:
        required_calculations = selected_calculations
    elif housing_positive and ("job offer" in lowered or "wage" in lowered or "weekly" in lowered):
        required_calculations = set(CALCULATION_REGISTRY)
    elif "job offer" in lowered and ("gross" in lowered or "wage" in lowered or "hours" in lowered):
        required_calculations = {"weekly_gross"}
    elif "calculate" in lowered and "report" in lowered:
        required_calculations = set()
    else:
        required_calculations = set()

    if required_calculations:
        _order, derived_fields, _assumptions = calculation_dependency_closure(sorted(required_calculations))
        required_fields = derived_fields | positive_fields
    else:
        required_fields = positive_fields
    if not required_fields and not required_calculations:
        raise IntentError("request is not representable by the supported intent compiler")
    return required_fields, required_calculations, "report" in lowered or "preview" in lowered


def check_request_completeness(request: str, intent: dict[str, Any]) -> None:
    required_fields, required_calculations, report_required = _job_request_requirements(request)
    requested_calculations = [item["name"] for item in intent["calculations"]]
    _order, derived_fields, _assumptions = calculation_dependency_closure(requested_calculations)
    effective_fields = set(intent.get("fields", [])) | derived_fields
    if not required_fields.issubset(effective_fields):
        raise IntentError("intent omits one or more requested source fields")
    if not required_calculations.issubset(set(requested_calculations)):
        raise IntentError("intent omits one or more requested calculations")
    if report_required and not intent["report"]["requested"]:
        raise IntentError("intent omits the requested in-memory report")


def compile_intent(intent: dict[str, Any], selected_fields: set[str] | None = None) -> dict[str, Any]:
    validate_intent(intent)
    source = intent["document"]["source"]
    # ``fields`` is optional in the intent schema. Calculations derive their
    # source fields through calculation_dependency_closure(), so a
    # calculation-only intent must not raise KeyError here.
    fields = [field for field in intent.get("fields", []) if selected_fields is None or field in selected_fields]
    calculations = [item["name"] for item in intent["calculations"]]
    calculation_order, derived_fields, _assumptions = calculation_dependency_closure(calculations)
    effective_fields = list(dict.fromkeys(fields + sorted(derived_fields)))
    variables: dict[str, dict[str, str]] = {"offer_results": {"type": "search_results"}}
    for field in effective_fields:
        capability = EXTRACTION_REGISTRY[field]
        variables[field] = {"type": "number", "unit": capability.unit}
    for name in calculation_order:
        variables[name] = {"type": "number", "unit": CALCULATION_REGISTRY[name].unit}

    query = " ".join([source] + [EXTRACTION_REGISTRY[field].query_terms for field in effective_fields])
    steps: list[dict[str, Any]] = [{"id": "search_requested_facts", "operation": "search_knowledge", "args": {"query": query, "limit": 10, "save_as": "offer_results"}}]
    for field in effective_fields:
        capability = EXTRACTION_REGISTRY[field]
        steps.append({
            "id": f"extract_{field}",
            "operation": "extract_number",
            "depends_on": ["search_requested_facts"],
            "args": {"from": "search_requested_facts", "field": field, "source": capability.source, "patterns": [capability.pattern], "save_as": field, "unit": capability.unit, "refine_query": capability.refine_query, "refine_limit": 5},
        })

    for name in calculation_order:
        capability = CALCULATION_REGISTRY[name]
        operation, operands = capability.operation, capability.operands
        compiled_operands: list[str | int] = []
        dependencies: list[str] = []
        for operand in operands:
            if operand == "days_per_week":
                compiled_operands.append(7)
                continue
            compiled_operands.append(operand)
            dependencies.append(f"extract_{operand}" if operand in EXTRACTION_REGISTRY else f"calculate_{operand}")
        steps.append({"id": f"calculate_{name}", "operation": "calculate", "depends_on": dependencies, "args": {"operation": operation, "operands": compiled_operands, "save_as": name}})

    if intent["report"]["requested"]:
        report_lines = ["# Task Plan Preview", "", "## Requested source facts", ""]
        report_fields = effective_fields
        report_calculations = calculation_order
        calculation_labels = {"weekly_gross": "Weekly gross", "weekly_housing": "Weekly housing", "difference": "Difference"}
        for field in report_fields:
            capability = EXTRACTION_REGISTRY[field]
            if capability.unit.startswith("USD/"):
                value_format = f"${{{field}}}{capability.unit[3:]}"
            else:
                value_format = f"{{{field}}} {capability.unit}"
            report_lines.append(f"- {capability.label}: {value_format}; source: {{{field}_source}}; location: {{{field}_location}}; chunk: {{{field}_chunk_id}}; evidence: {{{field}_evidence_excerpt}}")
        report_lines.extend(["", "## Requested calculations", ""])
        for name in report_calculations:
            report_lines.append(f"- {calculation_labels.get(name, name)}: ${{{name}}}")
        report_content = "\n".join(report_lines) + "\n"
        report_dependencies = [step["id"] for step in steps if step["operation"] in {"extract_number", "calculate"}]
        steps.append({"id": "write_report", "operation": "write_report", "depends_on": report_dependencies, "args": {"path": "output/task-plan-v05a2-preview.md", "content": report_content}})
        steps.append({"id": "verify_report", "operation": "verify_file", "depends_on": ["write_report"], "args": {"path": "output/task-plan-v05a2-preview.md", "reference": "write_report"}})

    plan = {"version": "0.5a", "variables": variables, "steps": steps}
    validation = plan_validator.validate_plan(plan)
    if not validation["valid"]:
        raise IntentError("compiler produced a plan rejected by the existing validator")
    return plan


def _intent_error(exc: BaseException) -> dict[str, str]:
    return {"code": getattr(exc, "code", "invalid_intent"), "message": _sanitize(exc)}


def _client_schema() -> dict[str, Any]:
    return intent_json_schema()


class IntentLMStudioClient:
    def __init__(self, model: str = DEFAULT_MODEL, opener: Callable[..., Any] | None = None, timeout: float = 30.0) -> None:
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError("model name is invalid")
        self.model = model
        self.endpoint = resolve_lm_studio_endpoint()
        self._opener = opener or urllib.request.build_opener(_NoRedirect).open
        self._timeout = timeout

    def generate(self, messages: list[dict[str, str]]) -> str:
        payload = {"model": self.model, "messages": messages, "temperature": 0, "stream": False, "response_format": {"type": "json_schema", "json_schema": {"name": "task_intent_v05a2", "strict": True, "schema": _client_schema()}}}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 404, 422}:
                raise StructuredOutputUnsupported("LM Studio/model rejected the intent JSON schema") from None
            raise LMUnavailable("LM Studio request failed") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise LMUnavailable("LM Studio is unavailable") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelOutputError("model response exceeds the response limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
            choice = payload["choices"][0]
            message = choice["message"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise ModelOutputError("LM Studio returned an invalid response") from None
        if choice.get("finish_reason") == "length":
            raise ModelOutputError("model output was truncated")
        if message.get("refusal"):
            raise ModelOutputError("model refused to produce an intent")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelOutputError("model returned empty intent content")
        return content


def _result() -> dict[str, Any]:
    return {"valid": False, "attempts": [], "errors": [], "intent": None, "compiled_plan": None, "safe_preview": None, "execution_performed": False}


def generate_compile_validate(request: str, client: IntentLMStudioClient | Any) -> dict[str, Any]:
    result = _result()
    try:
        _job_request_requirements(request)
    except IntentError as exc:
        result["errors"] = [_intent_error(exc)]
        return result
    system = "Return only a strict JSON intent object matching the supplied intent schema. Never output facts, quotes, values from documents, regex, formulas, templates, code, URLs, or unsupported fields. Use only allowlisted names and the explicit days_per_week=7 assumption when needed."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": request}]
    previous_raw_hash: str | None = None
    previous_intent_hash: str | None = None
    for number in range(1, MAX_GENERATIONS + 1):
        try:
            content = client.generate(messages)
            raw_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if raw_hash == previous_raw_hash:
                error = {"code": "identical_intent", "message": "model repeated identical intent; no further retry"}
                result["attempts"].append({"number": number, "valid": False, "errors": [error]})
                result["errors"] = result["errors"] + [error]
                break
            previous_raw_hash = raw_hash
            intent = _parse_strict(content)
            intent_hash = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if intent_hash == previous_intent_hash:
                error = {"code": "identical_intent", "message": "model repeated the previous invalid intent; no further retry"}
                result["attempts"].append({"number": number, "intent_sha256": intent_hash, "valid": False, "errors": [error]})
                result["errors"] = result["errors"] + [error]
                break
            previous_intent_hash = intent_hash
            result["intent"] = intent
            try:
                validate_intent(intent)
                check_request_completeness(request, intent)
                required_fields, required_calculations, _report_required = _job_request_requirements(request)
                _calc_order, derived_fields, _assumptions = calculation_dependency_closure(sorted(required_calculations))
                plan = compile_intent(intent, selected_fields=required_fields | derived_fields)
                result["attempts"].append({"number": number, "intent_sha256": intent_hash, "valid": True, "errors": []})
                result["valid"] = True
                result["compiled_plan"] = plan
                result["safe_preview"] = plan_validator.preview_plan(plan)
                return result
            except (IntentError, ValueError) as exc:
                error = _intent_error(exc)
                result["attempts"].append({"number": number, "intent_sha256": intent_hash, "valid": False, "errors": [error]})
                result["errors"] = [error]
                if number < MAX_GENERATIONS:
                    messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"request": request, "previous_intent": intent, "validation_errors": [error], "instruction": "Return a new intent object; do not repeat the invalid intent."}, ensure_ascii=False)}]
        except (LMError, IntentError) as exc:
            error = _intent_error(exc)
            result["attempts"].append({"number": number, "valid": False, "errors": [error]})
            result["errors"] = [error]
            if isinstance(exc, (LMUnavailable, StructuredOutputUnsupported)) or number == MAX_GENERATIONS:
                break
            messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"request": request, "validation_errors": [error], "instruction": "Return a new intent object only."})}]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    result = generate_compile_validate(args.request, IntentLMStudioClient(args.model))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
