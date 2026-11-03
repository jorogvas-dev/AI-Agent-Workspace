"""Production document-analysis-only HTTP entrypoint; no execution routes.

Run with python -m scripts.document_service_production inside the production
container. Credentials, knowledge destinations, and the listening port are fixed.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from scripts import document_pipeline_v06c as pipeline
from scripts import knowledge_adapter_v04c as knowledge_adapter
from scripts.task_planner_v05a2 import IntentLMStudioClient


VERSION = "0.6j"
PROFILE = "production"
SERVICE_TOKEN_FILE = "/run/secrets/document_planner_prod_token"
KNOWLEDGE_TOKEN_FILE = "/run/secrets/agent_knowledge_token"
BIND_HOST = "0.0.0.0"
BIND_PORT = 8105
MAX_REQUEST_BYTES = 8 * 1024
MAX_REQUEST_CHARS = 4000
MAX_RESPONSE_BYTES = 96 * 1024
MAX_CONCURRENT_ANALYSES = 2


class ServiceError(RuntimeError):
    """A bounded service failure; unexpected error details are never returned."""


def authorize_service_request(supplied: str, expected: str) -> bool:
    if not isinstance(supplied, str) or not isinstance(expected, str) or not expected:
        return False
    try:
        return secrets.compare_digest(supplied, "Bearer " + expected)
    except TypeError:
        return False


def _service_token() -> str:
    try:
        with Path(SERVICE_TOKEN_FILE).open("r", encoding="utf-8") as credential:
            raw = credential.read(1025)
        token = raw.strip()
        if len(raw) > 1024 or not token or len(token) > 512 or any(ord(char) < 32 for char in token):
            raise ValueError("invalid credential")
        return token
    except (OSError, UnicodeError, ValueError):
        raise ServiceError("service credential is unavailable") from None


def _validate_user_request(request: Any) -> None:
    if not isinstance(request, str) or not request.strip() or len(request) > MAX_REQUEST_CHARS:
        raise ServiceError("user request is invalid or too large")
    try:
        request.encode("utf-8")
    except UnicodeEncodeError:
        raise ServiceError("user request is invalid or too large") from None


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-standard JSON value")


class DocumentService:
    def __init__(self, model_client: Any, adapter: knowledge_adapter.ProductionKnowledgeServiceAdapter) -> None:
        if not isinstance(adapter, knowledge_adapter.ProductionKnowledgeServiceAdapter):
            raise ServiceError("production knowledge adapter is required")
        self.model_client = model_client
        self.adapter = adapter
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_ANALYSES)

    def analyze_document(self, request: str) -> dict[str, Any]:
        _validate_user_request(request)
        if not self._slots.acquire(timeout=1.0):
            raise ServiceError("document service is busy")
        try:
            return pipeline.analyze_request(request, adapter=self.adapter, client=self.model_client)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    service: DocumentService | None = None
    expected_token: str = ""

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        try:
            if not isinstance(payload, dict):
                raise ValueError("invalid response")
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        except (UnicodeError, ValueError, TypeError, RecursionError):
            body = b'{"valid":false,"error":"document service failure"}'
            status = 500
        if len(body) > MAX_RESPONSE_BYTES:
            body = b'{"valid":false,"error":"response too large"}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "profile": PROFILE, "version": VERSION, "pipeline_version": pipeline.VERSION})
            return
        self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path != "/analyze-document":
            self._json(404, {"error": "Not found"})
            return
        if not authorize_service_request(self.headers.get("Authorization", ""), self.expected_token):
            self._json(401, {"error": "Unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ServiceError("request body is invalid")
            raw = self.rfile.read(length)
            if not isinstance(raw, bytes) or len(raw) != length:
                raise ServiceError("request body is invalid")
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_constant)
            if not isinstance(payload, dict) or set(payload) != {"request"}:
                raise ServiceError("request body must contain only request")
            _validate_user_request(payload["request"])
        except (UnicodeError, ValueError, RecursionError, ServiceError):
            self._json(400, {"valid": False, "error": "request is invalid or too large"})
            return
        except Exception:
            self._json(500, {"valid": False, "error": "document service failure"})
            return
        try:
            if self.service is None:
                raise ServiceError("document service unavailable")
            self._json(200, self.service.analyze_document(payload["request"]))
        except ServiceError as exc:
            if str(exc) in {"document service unavailable", "document service is busy"}:
                self._json(400, {"valid": False, "error": str(exc)})
            else:
                self._json(500, {"valid": False, "error": "document service failure"})
        except Exception:
            self._json(500, {"valid": False, "error": "document service failure"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_service(model_client: Any | None = None, adapter=None) -> DocumentService:
    knowledge = adapter if adapter is not None else knowledge_adapter.create_production_adapter(
        Path(KNOWLEDGE_TOKEN_FILE), timeout_seconds=10.0,
    )
    model = model_client if model_client is not None else IntentLMStudioClient(timeout=30.0)
    return DocumentService(model, knowledge)


def serve() -> None:
    token = _service_token()
    application = build_service()
    Handler.service = application
    Handler.expected_token = token
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
