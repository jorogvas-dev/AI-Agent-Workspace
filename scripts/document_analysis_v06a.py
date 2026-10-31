"""Generic, read-only document field extraction for Task Planner v0.6a.

This module deliberately owns no credentials, network configuration, file
access, or execution capability. Callers provide a resolved document identity,
a natural-language request, a bounded retrieval callback, and a structured LLM
callback.

The structured LLM callback has the following interface::

    callback(stage: str, payload: dict[str, Any]) -> dict[str, Any] | str

``stage`` is either ``"request"`` or ``"extract"``. The request stage turns
the user's language into a small field-extraction request. The extract stage
receives only the validated field request and sanitized chunks returned by the
retrieval callback. Both outputs are schema-validated locally.

Evidence checks prove only structural relationships: source, chunk ID,
location, quotation membership, and raw-value membership. They do not prove
that a model selected the semantically correct fact.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence, TypeAlias


VERSION = "0.6a"
SUPPORTED_TASK_TYPE = "field_extraction"
SUPPORTED_VALUE_TYPES = frozenset({"text", "numeric"})

MAX_REQUEST_BYTES = 8 * 1024
MAX_FIELDS = 8
MAX_FIELD_NAME_BYTES = 96
MAX_QUESTION_BYTES = 768
MAX_RETRIEVAL_QUERY_BYTES = 4 * 1024
MAX_CHUNKS = 8
MAX_CHUNK_ID_BYTES = 192
MAX_SOURCE_BYTES = 1024
MAX_LOCATION_BYTES = 512
MAX_CHUNK_BYTES = 16 * 1024
MAX_TOTAL_CHUNK_BYTES = 64 * 1024
MAX_QUOTE_BYTES = 4 * 1024
MAX_CANDIDATES = 4
MAX_MODEL_RESPONSE_BYTES = 64 * 1024
MAX_MODEL_INPUT_BYTES = 128 * 1024
MAX_MODEL_CALLS = 2
MAX_RAW_NUMERIC_BYTES = 128
MAX_NORMALIZED_NUMERIC_BYTES = 256
MAX_RESULT_BYTES = 96 * 1024

_NUMERIC_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_.+-])"
    r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))"
    r"(?:[eE][+-]?[0-9]+)?"
    r"(?![A-Za-z0-9_.])"
)


class AnalysisError(ValueError):
    """Base error for bounded, structured validation failures."""

    code = "analysis_error"


class InvalidRequestError(AnalysisError):
    code = "invalid_request"


class InvalidExtractionRequestError(AnalysisError):
    code = "invalid_extraction_request"


class UnsupportedTaskError(AnalysisError):
    code = "unsupported_task_type"


class RetrievalError(AnalysisError):
    code = "retrieval_error"


class SourceMismatchError(RetrievalError):
    code = "source_mismatch"


class ModelOutputError(AnalysisError):
    code = "malformed_model_output"


class EvidenceError(AnalysisError):
    code = "invalid_evidence"


def _bounded_text(value: Any, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"{label} must be text")
    if not allow_empty and not value.strip():
        raise InvalidRequestError(f"{label} must not be empty")
    if len(value.encode("utf-8")) > maximum:
        raise InvalidRequestError(f"{label} exceeds its size limit")
    if any(ord(character) < 32 and character not in "\t\r\n" for character in value):
        raise InvalidRequestError(f"{label} contains a control character")
    return value


def _bounded_model_text(value: Any, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    """Validate model text without turning model failures into input failures."""
    if not isinstance(value, str):
        raise ModelOutputError(f"{label} is invalid")
    if not allow_empty and not value.strip():
        raise ModelOutputError(f"{label} is invalid")
    if len(value.encode("utf-8")) > maximum:
        raise ModelOutputError(f"{label} exceeds its size limit")
    if any(ord(character) < 32 and character not in "\t\r\n" for character in value):
        raise ModelOutputError(f"{label} contains a control character")
    return value


def _require_keys(value: Mapping[str, Any], required: set[str], allowed: set[str], label: str, error_type: type[AnalysisError]) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise error_type(f"{label} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise error_type(f"{label} contains unsupported fields: {', '.join(unknown)}")


def _strict_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ModelOutputError("structured model output contains duplicate fields")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ModelOutputError(f"structured model output contains non-standard number: {value}")


def _parse_model_object(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        if len(value) > MAX_MODEL_RESPONSE_BYTES:
            raise ModelOutputError("structured model output is too large")
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
        if len(raw) > MAX_MODEL_RESPONSE_BYTES:
            raise ModelOutputError("structured model output is too large")
    elif isinstance(value, dict):
        raw = None
    else:
        raise ModelOutputError("structured model output must be a JSON object")

    if raw is not None:
        try:
            parsed = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_strict_pairs,
                parse_constant=_reject_json_constant,
            )
        except ModelOutputError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelOutputError("structured model output is not valid JSON") from None
    else:
        parsed = value

    if not isinstance(parsed, dict):
        raise ModelOutputError("structured model output must be a JSON object")
    try:
        encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ModelOutputError("structured model output is not strict JSON data") from None
    if len(encoded) > MAX_MODEL_RESPONSE_BYTES:
        raise ModelOutputError("structured model output is too large")
    return parsed


def _validate_json_size(value: Mapping[str, Any], label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise AnalysisError(f"{label} is not bounded JSON data") from None
    if len(encoded) > MAX_MODEL_INPUT_BYTES:
        raise AnalysisError(f"{label} exceeds the model input limit")


def _safe_field_name(value: Any, label: str, error_type: type[AnalysisError]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{label} must be a non-empty field name")
    if len(value.encode("utf-8")) > MAX_FIELD_NAME_BYTES:
        raise error_type(f"{label} exceeds its size limit")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise error_type(f"{label} contains invalid characters")
    if not any(character.isalnum() for character in value):
        raise error_type(f"{label} must contain an alphanumeric character")
    allowed = set(" _-")
    if any(not (character.isalnum() or character in allowed) for character in value):
        raise error_type(f"{label} contains unsupported characters")
    return value


@dataclass(frozen=True)
class DocumentIdentity:
    """Resolved source identity supplied by the caller.

    The engine treats ``source`` as an opaque exact-match identity. It does
    not interpret it as a path, URL, filename, or network destination.
    """

    source: str
    document_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source identity must be a non-empty string")
        if len(self.source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("source identity exceeds its size limit")
        if any(ord(character) < 32 for character in self.source):
            raise ValueError("source identity contains a control character")
        if self.document_id is not None:
            if not isinstance(self.document_id, str) or not self.document_id.strip():
                raise ValueError("document_id must be a non-empty string when supplied")
            if len(self.document_id.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise ValueError("document_id exceeds its size limit")
            if any(ord(character) < 32 for character in self.document_id):
                raise ValueError("document_id contains a control character")

    def as_dict(self) -> dict[str, str]:
        result = {"source": self.source}
        if self.document_id is not None:
            result["document_id"] = self.document_id
        return result


ResolvedDocument = DocumentIdentity


@dataclass(frozen=True)
class DocumentChunk:
    """Sanitized retrieval chunk passed to the extraction model."""

    chunk_id: str
    source: str
    location: str
    text: str

    def as_dict(self) -> dict[str, str]:
        return {
            "chunk_id": self.chunk_id,
            "source": self.source,
            "location": self.location,
            "text": self.text,
        }


RetrievalCallback: TypeAlias = Callable[[DocumentIdentity, str, int], Any]
StructuredLLMCallback: TypeAlias = Callable[[str, dict[str, Any]], Any]


def _coerce_identity(value: DocumentIdentity | str) -> DocumentIdentity:
    if isinstance(value, DocumentIdentity):
        return value
    if isinstance(value, str):
        try:
            return DocumentIdentity(value)
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from None
    raise InvalidRequestError("document identity is invalid")


def extraction_request_schema() -> dict[str, Any]:
    """Return the strict schema expected from the request-planning model."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["task_type", "fields"],
        "properties": {
            "task_type": {"const": SUPPORTED_TASK_TYPE},
            "fields": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_FIELDS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "question", "value_type"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": MAX_FIELD_NAME_BYTES},
                        "question": {"type": "string", "minLength": 1, "maxLength": MAX_QUESTION_BYTES},
                        "value_type": {"enum": sorted(SUPPORTED_VALUE_TYPES)},
                    },
                },
            },
        },
    }


def extraction_response_schema() -> dict[str, Any]:
    """Return the strict schema expected from the extraction model."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["task_type", "fields"],
        "properties": {
            "task_type": {"const": SUPPORTED_TASK_TYPE},
            "fields": {
                "type": "array",
                "maxItems": MAX_FIELDS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "status", "candidates"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": MAX_FIELD_NAME_BYTES},
                        "status": {"enum": ["found", "missing", "ambiguous"]},
                        "candidates": {
                            "type": "array",
                            "maxItems": MAX_CANDIDATES,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["value", "source", "chunk_id", "location", "quote"],
                                "properties": {
                                    "value": {"type": ["string", "number"]},
                                    "source": {"type": "string", "minLength": 1, "maxLength": MAX_SOURCE_BYTES},
                                    "chunk_id": {"type": "string", "minLength": 1, "maxLength": MAX_CHUNK_ID_BYTES},
                                    "location": {"type": "string", "minLength": 1, "maxLength": MAX_LOCATION_BYTES},
                                    "quote": {"type": "string", "minLength": 1, "maxLength": MAX_QUOTE_BYTES},
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def validate_extraction_request(value: Any) -> dict[str, Any]:
    """Validate and normalize a model-produced field-extraction request."""
    try:
        payload = _parse_model_object(value)
    except ModelOutputError as exc:
        raise InvalidExtractionRequestError(str(exc)) from None
    _require_keys(payload, {"task_type", "fields"}, {"task_type", "fields"}, "extraction request", InvalidExtractionRequestError)
    if payload["task_type"] != SUPPORTED_TASK_TYPE:
        raise UnsupportedTaskError("only field extraction is supported")
    fields = payload["fields"]
    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
        raise InvalidExtractionRequestError(f"fields must contain 1 to {MAX_FIELDS} items")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(fields, start=1):
        if not isinstance(item, dict):
            raise InvalidExtractionRequestError(f"field {index} must be an object")
        _require_keys(item, {"name", "question", "value_type"}, {"name", "question", "value_type"}, f"field {index}", InvalidExtractionRequestError)
        name = _safe_field_name(item["name"], f"field {index}.name", InvalidExtractionRequestError)
        key = name.casefold()
        if key in seen:
            raise InvalidExtractionRequestError("field names must be unique")
        seen.add(key)
        question = _bounded_model_text(item["question"], f"field {index}.question", MAX_QUESTION_BYTES)
        value_type = item["value_type"]
        if value_type not in SUPPORTED_VALUE_TYPES:
            raise InvalidExtractionRequestError("field value_type must be text or numeric")
        normalized.append({"name": name, "question": question, "value_type": value_type})
    result = {"task_type": SUPPORTED_TASK_TYPE, "fields": normalized}
    _validate_json_size(result, "extraction request")
    return result


def _normalize_chunks(value: Any, identity: DocumentIdentity) -> list[DocumentChunk]:
    if isinstance(value, dict):
        status = value.get("status")
        if status is not None and status != "success":
            raise RetrievalError("retrieval did not return success")
        items = value.get("results")
        if not isinstance(items, list):
            raise RetrievalError("retrieval response has no valid results")
        result_count = value.get("result_count")
        if result_count is not None and (type(result_count) is not int or result_count != len(items)):
            raise RetrievalError("retrieval response count is invalid")
    elif isinstance(value, list):
        items = value
    else:
        raise RetrievalError("retrieval response is invalid")

    if len(items) > MAX_CHUNKS:
        raise RetrievalError("retrieval returned too many chunks")
    chunks: list[DocumentChunk] = []
    seen_ids: set[str] = set()
    total_bytes = 0
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise RetrievalError(f"retrieval chunk {index} is invalid")
        required = {"chunk_id", "source", "location", "text"}
        if not required.issubset(item):
            raise RetrievalError(f"retrieval chunk {index} is missing identity metadata")
        chunk_id = item["chunk_id"]
        source = item["source"]
        location = item["location"]
        text = item["text"]
        if not isinstance(chunk_id, str) or not chunk_id.strip() or len(chunk_id.encode("utf-8")) > MAX_CHUNK_ID_BYTES:
            raise RetrievalError(f"retrieval chunk {index} has an invalid chunk ID")
        if chunk_id in seen_ids:
            raise RetrievalError("retrieval returned duplicate chunk IDs")
        if not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise RetrievalError(f"retrieval chunk {index} has an invalid source")
        if source != identity.source:
            raise SourceMismatchError("retrieval returned a chunk from a different source")
        if not isinstance(location, str) or not location.strip() or len(location.encode("utf-8")) > MAX_LOCATION_BYTES:
            raise RetrievalError(f"retrieval chunk {index} has an invalid location")
        if not isinstance(text, str) or not text.strip():
            raise RetrievalError(f"retrieval chunk {index} has invalid text")
        text_bytes = len(text.encode("utf-8"))
        if text_bytes > MAX_CHUNK_BYTES:
            raise RetrievalError("retrieval chunk exceeds its size limit")
        total_bytes += text_bytes
        if total_bytes > MAX_TOTAL_CHUNK_BYTES:
            raise RetrievalError("retrieved chunks exceed the total size limit")
        if any(ord(character) < 32 and character not in "\t\r\n" for character in text):
            raise RetrievalError("retrieval chunk contains a control character")
        seen_ids.add(chunk_id)
        chunks.append(DocumentChunk(chunk_id, source, location, text))
    return chunks


def validate_extraction_response(value: Any, requested_fields: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """Validate response structure, preserving missing and ambiguous states.

    Evidence membership and numeric normalization require the retrieved chunks,
    so those checks are performed by :func:`analyze_document`.
    """
    try:
        payload = _parse_model_object(value)
    except ModelOutputError:
        raise
    _require_keys(payload, {"task_type", "fields"}, {"task_type", "fields"}, "extraction response", ModelOutputError)
    if payload["task_type"] != SUPPORTED_TASK_TYPE:
        raise UnsupportedTaskError("only field extraction is supported")
    raw_fields = payload["fields"]
    if not isinstance(raw_fields, list) or len(raw_fields) > MAX_FIELDS:
        raise ModelOutputError("extraction response fields are invalid")
    requested: dict[str, Mapping[str, str]] = {field["name"].casefold(): field for field in requested_fields}
    by_name: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_fields, start=1):
        if not isinstance(item, dict):
            raise ModelOutputError(f"extraction response field {index} is invalid")
        _require_keys(item, {"name", "status", "candidates"}, {"name", "status", "candidates"}, f"extraction response field {index}", ModelOutputError)
        name = _safe_field_name(item["name"], f"extraction response field {index}.name", ModelOutputError)
        key = name.casefold()
        if key not in requested:
            raise ModelOutputError("extraction response contains an unknown field")
        if key in by_name:
            raise ModelOutputError("extraction response contains duplicate fields")
        status = item["status"]
        if status not in {"found", "missing", "ambiguous"}:
            raise ModelOutputError("extraction response contains an invalid field status")
        candidates = item["candidates"]
        if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
            raise ModelOutputError("extraction response candidates are invalid")
        if status == "found" and len(candidates) != 1:
            raise ModelOutputError("found fields must contain exactly one candidate")
        if status == "missing" and candidates:
            raise ModelOutputError("missing fields cannot contain candidates")
        if status == "ambiguous" and len(candidates) < 2:
            raise ModelOutputError("ambiguous fields must contain multiple candidates")
        normalized_candidates: list[dict[str, Any]] = []
        for candidate_index, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, dict):
                raise ModelOutputError(f"candidate {candidate_index} is invalid")
            _require_keys(
                candidate,
                {"value", "source", "chunk_id", "location", "quote"},
                {"value", "source", "chunk_id", "location", "quote"},
                "extraction candidate",
                ModelOutputError,
            )
            candidate_source = candidate["source"]
            chunk_id = candidate["chunk_id"]
            location = candidate["location"]
            quote = candidate["quote"]
            if not isinstance(candidate_source, str) or not candidate_source.strip() or len(candidate_source.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise ModelOutputError("extraction candidate source is invalid")
            if not isinstance(chunk_id, str) or not chunk_id.strip() or len(chunk_id.encode("utf-8")) > MAX_CHUNK_ID_BYTES:
                raise ModelOutputError("extraction candidate chunk_id is invalid")
            if not isinstance(location, str) or not location.strip() or len(location.encode("utf-8")) > MAX_LOCATION_BYTES:
                raise ModelOutputError("extraction candidate location is invalid")
            if not isinstance(quote, str) or not quote.strip() or len(quote.encode("utf-8")) > MAX_QUOTE_BYTES:
                raise ModelOutputError("extraction candidate quote is invalid")
            raw_value = candidate["value"]
            if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)):
                raise ModelOutputError("extraction candidate value is invalid")
            if isinstance(raw_value, float) and not math.isfinite(raw_value):
                raise ModelOutputError("extraction candidate value is not finite")
            normalized_candidates.append({
                "value": raw_value,
                "source": candidate_source,
                "chunk_id": chunk_id,
                "location": location,
                "quote": quote,
            })
        by_name[key] = {"name": name, "status": status, "candidates": normalized_candidates}

    normalized_fields: list[dict[str, Any]] = []
    for field in requested_fields:
        key = field["name"].casefold()
        normalized_fields.append(by_name.get(key, {"name": field["name"], "status": "missing", "candidates": []}))
    result = {"task_type": SUPPORTED_TASK_TYPE, "fields": normalized_fields}
    _validate_json_size(result, "extraction response")
    return result


def _build_query(fields: Sequence[Mapping[str, str]]) -> str:
    query = " ".join(field["question"].strip() for field in fields).strip()
    if not query or len(query.encode("utf-8")) > MAX_RETRIEVAL_QUERY_BYTES:
        raise InvalidRequestError("field questions produce an oversized retrieval query")
    return query


def _raw_numeric(value: Any) -> tuple[str, Decimal]:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise EvidenceError("numeric evidence has an invalid value")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvidenceError("numeric evidence is not finite")
        raw = str(value)
    else:
        raw = value if isinstance(value, str) else str(value)
    raw = raw.strip()
    if not raw:
        raise EvidenceError("numeric evidence is empty")
    if len(raw.encode("utf-8")) > MAX_RAW_NUMERIC_BYTES:
        raise EvidenceError("numeric evidence token exceeds its size limit")
    try:
        decimal_value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise EvidenceError("numeric evidence is not a valid decimal") from None
    if not decimal_value.is_finite():
        raise EvidenceError("numeric evidence is not finite")
    return raw, decimal_value


def _fixed_decimal_length(value: Decimal) -> int:
    """Return fixed-point length without allocating the formatted value."""
    if value == 0:
        return 1
    sign_length = 1 if value.as_tuple().sign else 0
    digits_length = len(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    if exponent >= 0:
        return sign_length + digits_length + exponent
    integer_digits = digits_length + exponent
    if integer_digits > 0:
        return sign_length + digits_length + 1
    return sign_length + 2 + (-integer_digits) + digits_length


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    if _fixed_decimal_length(value) > MAX_NORMALIZED_NUMERIC_BYTES:
        raise EvidenceError("normalized numeric evidence exceeds its size limit")
    # The expansion bound is checked before this potentially large allocation.
    formatted = format(value, "f")
    if len(formatted.encode("utf-8")) > MAX_NORMALIZED_NUMERIC_BYTES:
        raise EvidenceError("normalized numeric evidence exceeds its size limit")
    return formatted


def _numeric_token_matches(raw_value: str, decimal_value: Decimal, quote: str) -> bool:
    """Match only a complete quotation token with the same Decimal value."""
    for match in _NUMERIC_LITERAL.finditer(quote):
        token = match.group(0)
        if len(token.encode("utf-8")) > MAX_RAW_NUMERIC_BYTES:
            raise EvidenceError("quoted numeric token exceeds its size limit")
        try:
            token_decimal = Decimal(token)
        except InvalidOperation:
            continue
        if not token_decimal.is_finite():
            continue
        if token == raw_value and decimal_value.compare(token_decimal) == Decimal(0):
            return True
    return False


def _validate_candidate(
    field: Mapping[str, str],
    candidate: Mapping[str, Any],
    identity: DocumentIdentity,
    chunks: Mapping[str, DocumentChunk],
) -> dict[str, Any]:
    chunk = chunks.get(candidate["chunk_id"])
    if chunk is None:
        raise EvidenceError(f"field {field['name']} references an unknown chunk")
    if chunk.source != identity.source or candidate["source"] != identity.source or candidate["source"] != chunk.source:
        raise EvidenceError(f"field {field['name']} references the wrong source")
    if candidate["location"] != chunk.location:
        raise EvidenceError(f"field {field['name']} references an invalid location")
    quote = candidate["quote"]
    if quote not in chunk.text:
        raise EvidenceError(f"field {field['name']} quote is not present in the cited chunk")
    value = candidate["value"]
    if field["value_type"] == "numeric":
        raw_value, decimal_value = _raw_numeric(value)
        if not _numeric_token_matches(raw_value, decimal_value, quote):
            raise EvidenceError(f"field {field['name']} value is not present in its quote")
        normalized_value: str | Any = _decimal_text(decimal_value)
    else:
        if not isinstance(value, str) or not value.strip():
            raise EvidenceError(f"field {field['name']} text evidence is invalid")
        if value not in quote:
            raise EvidenceError(f"field {field['name']} value is not present in its quote")
        normalized_value = value
    return {
        "value": normalized_value,
        "value_type": field["value_type"],
        "source": chunk.source,
        "location": chunk.location,
        "chunk_id": chunk.chunk_id,
        "quote": quote,
    }


def _initial_result(source: str | None = None) -> dict[str, Any]:
    return {
        "version": VERSION,
        "valid": False,
        "success": False,
        "status": "not_started",
        "source": source,
        "extraction_request": None,
        "fields": [],
        "errors": [],
        "model_calls": 0,
        "evidence_validation": "structural_only; semantic_correctness_not_verified",
    }


def _error(result: dict[str, Any], exc: AnalysisError, *, field: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if field is not None:
        item["field"] = field
    result["errors"].append(item)
    result["status"] = exc.code
    result["valid"] = False
    result["success"] = False
    return result


def _missing_result(result: dict[str, Any], fields: Sequence[Mapping[str, str]], *, error_code: str = "missing_field") -> dict[str, Any]:
    result["valid"] = True
    result["success"] = False
    result["status"] = "missing"
    result["fields"] = [{"name": field["name"], "value_type": field["value_type"], "status": "missing"} for field in fields]
    for field in fields:
        result["errors"].append({"code": error_code, "message": "No verified candidate was returned for this field.", "field": field["name"]})
    return result


def _analyze_document(
    document: DocumentIdentity | str,
    request: str,
    retrieve: RetrievalCallback,
    structured_llm: StructuredLLMCallback,
) -> dict[str, Any]:
    """Run bounded read-only field extraction using injected callbacks.

    The request-planning model receives no document text. The extraction model
    receives only sanitized chunks for the exact resolved source identity. No
    callback exception or raw model output is returned to callers.
    """
    result = _initial_result()
    try:
        identity = _coerce_identity(document)
        result["source"] = identity.source
        _bounded_text(request, "request", MAX_REQUEST_BYTES)
        if not callable(retrieve) or not callable(structured_llm):
            raise InvalidRequestError("retrieval and structured-LLM callbacks are required")
    except AnalysisError as exc:
        return _error(result, exc)
    except ValueError as exc:
        return _error(result, InvalidRequestError(str(exc)))

    request_payload = {
        "source": identity.as_dict(),
        "user_request": request,
        "response_schema": extraction_request_schema(),
        "instructions": "Return only a field_extraction request. Do not return code, regex, formulas, commands, URLs, or document facts.",
    }
    try:
        _validate_json_size(request_payload, "request model input")
        request_output = structured_llm("request", request_payload)
        result["model_calls"] = 1
        extraction_request = validate_extraction_request(request_output)
    except UnsupportedTaskError as exc:
        return _error(result, exc)
    except (AnalysisError, ValueError):
        return _error(result, ModelOutputError("request-planning model output is invalid"))
    except Exception:
        return _error(result, ModelOutputError("request-planning model call failed"))

    result["extraction_request"] = extraction_request
    fields = extraction_request["fields"]
    try:
        query = _build_query(fields)
        raw_retrieval = retrieve(identity, query, MAX_CHUNKS)
        chunks = _normalize_chunks(raw_retrieval, identity)
    except SourceMismatchError as exc:
        return _error(result, exc)
    except (RetrievalError, InvalidRequestError):
        return _error(result, RetrievalError("bounded retrieval failed"))
    except Exception:
        return _error(result, RetrievalError("bounded retrieval failed"))

    if not chunks:
        return _missing_result(result, fields)

    chunk_map = {chunk.chunk_id: chunk for chunk in chunks}
    extraction_payload = {
        "source": identity.as_dict(),
        "fields": fields,
        "chunks": [chunk.as_dict() for chunk in chunks],
        "response_schema": extraction_response_schema(),
        "instructions": "Use only the supplied chunks. Return missing or ambiguous instead of guessing. Cite exact source, location, chunk_id, and quote.",
    }
    try:
        _validate_json_size(extraction_payload, "extraction model input")
        extraction_output = structured_llm("extract", extraction_payload)
        result["model_calls"] = 2
        response = validate_extraction_response(extraction_output, fields)
    except UnsupportedTaskError as exc:
        return _error(result, exc)
    except ModelOutputError:
        return _error(result, ModelOutputError("extraction model output is invalid"))
    except Exception:
        return _error(result, ModelOutputError("extraction model call failed"))

    output_fields: list[dict[str, Any]] = []
    has_missing = False
    has_ambiguous = False
    for field, field_output in zip(fields, response["fields"]):
        status = field_output["status"]
        if status == "missing":
            has_missing = True
            output_fields.append({"name": field["name"], "value_type": field["value_type"], "status": "missing"})
            result["errors"].append({"code": "missing_field", "message": "No verified candidate was returned for this field.", "field": field["name"]})
            continue
        candidates: list[dict[str, Any]] = []
        try:
            for candidate in field_output["candidates"]:
                candidates.append(_validate_candidate(field, candidate, identity, chunk_map))
        except EvidenceError as exc:
            output_fields.append({"name": field["name"], "value_type": field["value_type"], "status": "invalid_evidence"})
            result["fields"] = output_fields
            return _error(result, exc, field=field["name"])
        if status == "ambiguous":
            has_ambiguous = True
            output_fields.append({"name": field["name"], "value_type": field["value_type"], "status": "ambiguous", "candidates": candidates})
            result["errors"].append({"code": "ambiguous_field", "message": "Multiple structurally supported candidates were returned; none was selected.", "field": field["name"]})
        else:
            output_fields.append({"name": field["name"], "status": "found", **candidates[0]})

    result["fields"] = output_fields
    result["valid"] = True
    if has_ambiguous:
        result["status"] = "ambiguous"
        result["success"] = False
    elif has_missing:
        result["status"] = "missing"
        result["success"] = False
    else:
        result["status"] = "success"
        result["success"] = True
    return result


def _compact_result_overflow(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": VERSION,
        "valid": False,
        "success": False,
        "status": "result_too_large",
        "source": result.get("source"),
        "extraction_request": None,
        "fields": [],
        "errors": [{"code": "result_too_large", "message": "analysis result exceeds its size limit."}],
        "model_calls": result.get("model_calls", 0),
        "evidence_validation": "structural_only; semantic_correctness_not_verified",
    }


def _finalize_result(result: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return _compact_result_overflow(result)
    if len(encoded) > MAX_RESULT_BYTES:
        return _compact_result_overflow(result)
    return result


def analyze_document(
    document: DocumentIdentity | str,
    request: str,
    retrieve: RetrievalCallback,
    structured_llm: StructuredLLMCallback,
) -> dict[str, Any]:
    """Run bounded read-only field extraction and enforce result-size limits."""
    return _finalize_result(_analyze_document(document, request, retrieve, structured_llm))


__all__ = [
    "AnalysisError",
    "DocumentChunk",
    "DocumentIdentity",
    "EvidenceError",
    "InvalidExtractionRequestError",
    "InvalidRequestError",
    "MAX_CANDIDATES",
    "MAX_CHUNKS",
    "MAX_FIELDS",
    "MAX_NORMALIZED_NUMERIC_BYTES",
    "MAX_RAW_NUMERIC_BYTES",
    "MAX_RESULT_BYTES",
    "ModelOutputError",
    "ResolvedDocument",
    "RetrievalCallback",
    "RetrievalError",
    "SourceMismatchError",
    "StructuredLLMCallback",
    "SUPPORTED_TASK_TYPE",
    "SUPPORTED_VALUE_TYPES",
    "UnsupportedTaskError",
    "VERSION",
    "analyze_document",
    "extraction_request_schema",
    "extraction_response_schema",
    "validate_extraction_request",
    "validate_extraction_response",
]
