"""Restricted Open WebUI clients for isolated legacy and document analysis."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


PLANNER_ENDPOINT = "http://planner-service:8105/analyze"
DOCUMENT_ENDPOINT = "http://planner-service:8105/analyze-document"
PLANNER_TOKEN_FILE = "/run/secrets/planner_api_token"
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


def _call_planner(endpoint: str, user_request: str) -> str:
    unavailable = {"valid": False, "error": "planner API unavailable"}
    invalid = {"valid": False, "error": "request is invalid or too large"}
    if endpoint not in {PLANNER_ENDPOINT, DOCUMENT_ENDPOINT}:
        return json.dumps(unavailable)
    if not isinstance(user_request, str) or not user_request.strip() or len(user_request) > MAX_REQUEST_CHARS:
        return json.dumps(invalid)
    try:
        # Forward the complete request unchanged, including unsupported parts.
        body = json.dumps({"request": user_request}, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            return json.dumps(invalid)
    except (UnicodeError, ValueError):
        return json.dumps(invalid)
    try:
        with open(PLANNER_TOKEN_FILE, "r", encoding="utf-8") as credential:
            raw_token = credential.read(1025)
        token = raw_token.strip()
        if len(raw_token) > 1024 or not token or len(token) > 512:
            return json.dumps(unavailable)
        request = urllib.request.Request(
            endpoint, data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            method="POST",
        )
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            geturl = getattr(response, "geturl", None)
            if callable(geturl) and geturl() != endpoint:
                return json.dumps(unavailable)
            status = getattr(response, "status", 200)
            if isinstance(status, int) and not 200 <= status < 300:
                return json.dumps(unavailable)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            return json.dumps(unavailable)
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(payload, dict):
            return json.dumps(unavailable)
        result = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        result.encode("utf-8")
        return result
    except urllib.error.HTTPError as exc:
        exc.close()
        return json.dumps(unavailable)
    except (urllib.error.URLError, TimeoutError, OSError,
            ValueError, TypeError, RecursionError):
        return json.dumps(unavailable)
    except Exception:
        return json.dumps(unavailable)


class Tools:
    """API-only analysis tools; only the fixed service-authentication file is read."""

    def analyze_with_planner(self, user_request: str) -> str:
        """Handle the restricted job-offer calculation workflow using the entire user request."""
        return _call_planner(PLANNER_ENDPOINT, user_request)

    def analyze_document(self, user_request: str) -> str:
        """Extract arbitrary requested text/numeric fields from named indexed documents.

        Forward the entire user request unchanged. Extraction is read-only;
        calculations, summaries, and other unsupported tasks are not executed.
        """
        return _call_planner(DOCUMENT_ENDPOINT, user_request)
