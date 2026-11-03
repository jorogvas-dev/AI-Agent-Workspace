"""Standalone Open WebUI tool for the fixed production document-analysis API."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


DOCUMENT_ENDPOINT = "http://document-planner-prod:8105/analyze-document"
DOCUMENT_TOKEN_FILE = "/run/secrets/document_planner_prod_token"
MAX_REQUEST_CHARS = 4000
MAX_REQUEST_BYTES = 8 * 1024
MAX_RESPONSE_BYTES = 96 * 1024
REQUEST_TIMEOUT_SECONDS = 75


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirects are not permitted", headers, fp)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-standard JSON value")


def _pick(value, keys):
    if not isinstance(value, dict):
        raise ValueError("invalid document result metadata")
    return {key: value[key] for key in keys if key in value}


def _document_result(payload):
    """Preserve public evidence/statuses while excluding planning/debug payloads."""
    result = _pick(payload, (
        "valid", "success", "status", "source", "version", "pipeline_version",
        "execution_performed", "model_calls", "evidence_validation", "error",
    ))
    identity_keys = ("document_id", "folder", "source")
    evidence_keys = ("value", "value_type", "source", "chunk_id", "location", "quote")
    if "selected_document" in payload:
        selected = payload["selected_document"]
        result["selected_document"] = None if selected is None else _pick(selected, identity_keys)
    for key in ("errors", "choices", "fields"):
        if key in payload and not isinstance(payload[key], list):
            raise ValueError("invalid document result metadata")
    if "errors" in payload:
        result["errors"] = [_pick(item, ("code", "message", "field")) for item in payload["errors"]]
    if "choices" in payload:
        result["choices"] = [_pick(item, identity_keys) for item in payload["choices"]]
    if "choices_truncated" in payload:
        result["choices_truncated"] = payload["choices_truncated"]
    if "fields" in payload:
        result["fields"] = []
        for field in payload["fields"]:
            projected = _pick(field, ("name", "value_type", "status"))
            if field.get("status") == "found":
                projected.update(_pick(field, evidence_keys))
            elif field.get("status") == "ambiguous":
                candidates = field.get("candidates", [])
                if not isinstance(candidates, list):
                    raise ValueError("invalid document candidates")
                projected["candidates"] = [_pick(candidate, evidence_keys) for candidate in candidates]
            result["fields"].append(projected)
    return result


def _call_document(user_request: str) -> str:
    unavailable = {"valid": False, "error": "document API unavailable"}
    invalid = {"valid": False, "error": "request is invalid or too large"}
    if not isinstance(user_request, str) or not user_request.strip() or len(user_request) > MAX_REQUEST_CHARS:
        return json.dumps(invalid)
    try:
        # Preserve every clause for the unchanged pipeline's task classification.
        body = json.dumps({"request": user_request}, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            return json.dumps(invalid)
    except (UnicodeError, ValueError):
        return json.dumps(invalid)
    try:
        with open(DOCUMENT_TOKEN_FILE, "r", encoding="utf-8") as credential:
            raw_token = credential.read(1025)
        token = raw_token.strip()
        if len(raw_token) > 1024 or not token or len(token) > 512 or any(ord(char) < 32 for char in token):
            return json.dumps(unavailable)
        request = urllib.request.Request(
            DOCUMENT_ENDPOINT, data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST",
        )
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            geturl = getattr(response, "geturl", None)
            if callable(geturl) and geturl() != DOCUMENT_ENDPOINT:
                return json.dumps(unavailable)
            status = getattr(response, "status", 200)
            if isinstance(status, int) and not 200 <= status < 300:
                return json.dumps(unavailable)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            return json.dumps(unavailable)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_constant)
        if not isinstance(payload, dict):
            return json.dumps(unavailable)
        # Check the entire response before discarding internal-only fields.
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return json.dumps(_document_result(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        return json.dumps(unavailable)
    except Exception:
        return json.dumps(unavailable)


class Tools:
    """Only production document analysis; no configurable destinations or valves."""

    def analyze_document(self, user_request: str) -> str:
        """Extract requested text/numeric facts from named indexed documents.

        Forward the complete request unchanged. Analysis is read-only; arithmetic,
        summaries, and other unsupported tasks are not executed. Only fields with
        status "found" contain evidence-verified answers (structural checks only).
        Missing extraction does not establish absence from the document.
        Ambiguous candidates are not settled facts.
        """
        return _call_document(user_request)
