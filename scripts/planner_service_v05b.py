#!/usr/bin/env python3
"""Test-only HTTP bridge for v0.5a.2 planning and v0.4d PREPARE.

The service exposes POST /analyze and POST /analyze-document. It never exposes COMMIT, reconcile,
approval records, credentials, Docker, or the executor. Configuration is
service-side; request JSON contains only a natural-language request.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from scripts import knowledge_adapter_v04c as knowledge_adapter
from scripts import document_pipeline_v06c
from scripts import task_planner_bridge_v05a2_v04d as bridge
from scripts import task_planner_v05a2 as planner


VERSION = "0.5b"
MAX_REQUEST_BYTES = 8 * 1024
MAX_REQUEST_CHARS = 4000
MAX_RESPONSE_BYTES = 96 * 1024
MAX_CONCURRENT_ANALYSES = 2
SERVICE_TOKEN_ENV = "PLANNER_API_TOKEN"
SERVICE_TOKEN_FILE_ENV = "PLANNER_API_TOKEN_FILE"
DEFAULT_SERVICE_TOKEN_FILE = "/run/secrets/planner_api_token"
DEFAULT_KNOWLEDGE_TOKEN_FILE = "/run/secrets/agent_knowledge_token"
DEFAULT_ISOLATED_ROOT = "/planner-test"
DEFAULT_APPROVAL_DIR = "/planner-test/approvals"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8105


class ServiceError(RuntimeError):
    pass


def authorize_service_request(supplied: str, expected: str) -> bool:
    if not isinstance(supplied, str) or not isinstance(expected, str) or not expected:
        return False
    try:
        return secrets.compare_digest(supplied, "Bearer " + expected)
    except TypeError:
        return False


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


def _safe_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:240] or "planner service error"


def _read_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ServiceError("service credential is unavailable") from exc
    if not value or len(value) > 512:
        raise ServiceError("service credential is unavailable")
    return value


def _service_token() -> str:
    token_file = os.environ.get(SERVICE_TOKEN_FILE_ENV, DEFAULT_SERVICE_TOKEN_FILE)
    if token_file != DEFAULT_SERVICE_TOKEN_FILE:
        raise ServiceError("planner API credential path is not approved")
    if os.environ.get(SERVICE_TOKEN_ENV):
        return os.environ[SERVICE_TOKEN_ENV]
    return _read_secret(Path(token_file))


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("valid"):
        return {
            "valid": False,
            "version": VERSION,
            "attempts": result.get("planning", {}).get("attempts", []),
            "errors": result.get("planning", {}).get("errors", [{"code": "analysis_failed", "message": "analysis failed"}]),
            "report": None,
            "evidence": [],
            "calculations": [],
            "execution_performed": False,
        }
    prepared = result.get("prepare") or {}
    records = prepared.get("records", [])
    report_record = next((record for record in records if record.get("status") == "would_write"), None)
    evidence = []
    calculations = []
    for record in records:
        if record.get("operation") == "extract_number" and record.get("status") == "simulated_success":
            fact = record.get("fact", {})
            evidence.append({
                "field": fact.get("field"),
                "value": fact.get("value"),
                "unit": fact.get("unit"),
                "source": fact.get("source"),
                "location": fact.get("location"),
                "chunk_id": fact.get("chunk_id"),
                "evidence_excerpt": fact.get("evidence_excerpt"),
            })
        if record.get("operation") == "calculate" and record.get("status") == "simulated_success":
            calculations.append({"field": record.get("id"), "value": record.get("value")})
    if report_record is None or not isinstance(report_record.get("content"), str):
        raise ServiceError("PREPARE did not produce a report preview")
    response = {
        "valid": True,
        "version": VERSION,
        "attempts": result.get("planning", {}).get("attempts", []),
        "errors": [],
        "report": {"content": report_record["content"], "sha256": report_record.get("sha256")},
        "evidence": evidence,
        "calculations": calculations,
        "execution_performed": False,
    }
    encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ServiceError("analysis response exceeds the response limit")
    return response


class PlannerService:
    def __init__(self, model_client: Any, adapter: Any, isolated_root: Path, approval_dir: Path) -> None:
        self.model_client = model_client
        self.adapter = adapter
        self.isolated_root = isolated_root
        self.approval_dir = approval_dir
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_ANALYSES)

    def analyze(self, request: str) -> dict[str, Any]:
        _validate_user_request(request)
        if not self._slots.acquire(timeout=1.0):
            raise ServiceError("planner service is busy")
        try:
            result = bridge.prepare_from_request(
                request,
                model_client=self.model_client,
                isolated_root=self.isolated_root,
                approval_dir=self.approval_dir,
                adapter=self.adapter,
            )
            return _public_result(result)
        except Exception:
            return {
                "valid": False,
                "version": VERSION,
                "attempts": [],
                "errors": [{"code": "analysis_failed", "message": "isolated planning analysis failed"}],
                "report": None,
                "evidence": [],
                "calculations": [],
                "execution_performed": False,
            }
        finally:
            self._slots.release()

    def analyze_document(self, request: str) -> dict[str, Any]:
        """Return the read-only generic pipeline result without legacy filtering."""
        _validate_user_request(request)
        if not self._slots.acquire(timeout=1.0):
            raise ServiceError("planner service is busy")
        try:
            return document_pipeline_v06c.analyze_request(
                request, adapter=self.adapter, client=self.model_client
            )
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    service: PlannerService | None = None
    expected_token: str = ""

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            body = b'{"valid":false,"error":"response too large"}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path not in ("/analyze", "/analyze-document"):
            self._json(404, {"error": "Not found"})
            return
        supplied = self.headers.get("Authorization", "")
        if not authorize_service_request(supplied, self.expected_token):
            self._json(401, {"error": "Unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ServiceError("request body is invalid")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ServiceError("request body is invalid")
            payload = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(payload, dict) or set(payload) != {"request"}:
                raise ServiceError("request body must contain only request")
            _validate_user_request(payload["request"])
        except (UnicodeError, ValueError, RecursionError):
            self._json(400, {"valid": False, "error": "malformed JSON"})
            return
        except ServiceError as exc:
            self._json(400, {"valid": False, "error": _safe_error(exc)})
            return
        except Exception:
            self._json(500, {"valid": False, "error": "planner service failure"})
            return
        try:
            if self.service is None:
                raise ServiceError("planner service unavailable")
            analysis = self.service.analyze if self.path == "/analyze" else self.service.analyze_document
            self._json(200, analysis(payload["request"]))
        except ServiceError as exc:
            if str(exc) in {"planner service unavailable", "planner service is busy"}:
                self._json(400, {"valid": False, "error": str(exc)})
            else:
                self._json(500, {"valid": False, "error": "planner service failure"})
        except Exception:
            self._json(500, {"valid": False, "error": "planner service failure"})

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "version": VERSION})
            return
        self._json(404, {"error": "Not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_service(model_client: Any | None = None, adapter: Any | None = None) -> PlannerService:
    model = model_client or planner.IntentLMStudioClient(timeout=30.0)
    configured_knowledge_path = os.environ.get("KNOWLEDGE_TOKEN_FILE", DEFAULT_KNOWLEDGE_TOKEN_FILE)
    if configured_knowledge_path != DEFAULT_KNOWLEDGE_TOKEN_FILE:
        raise ServiceError("knowledge credential path is not approved")
    knowledge = adapter or knowledge_adapter.create_isolated_adapter(Path(DEFAULT_KNOWLEDGE_TOKEN_FILE), timeout_seconds=10.0)
    root = Path(os.environ.get("PLANNER_ISOLATED_ROOT", DEFAULT_ISOLATED_ROOT))
    approvals = Path(os.environ.get("PLANNER_APPROVAL_DIR", DEFAULT_APPROVAL_DIR))
    return PlannerService(model, knowledge, root, approvals)


def serve() -> None:
    token = _service_token()
    bind_host = os.environ.get("PLANNER_BIND_HOST", DEFAULT_BIND_HOST)
    if bind_host not in {"127.0.0.1", "0.0.0.0"}:
        raise ServiceError("planner bind host is not approved")
    bind_port = int(os.environ.get("PLANNER_BIND_PORT", str(DEFAULT_BIND_PORT)))
    if bind_port != DEFAULT_BIND_PORT:
        raise ServiceError("planner bind port is not approved")
    server = ThreadingHTTPServer((bind_host, bind_port), Handler)
    Handler.service = build_service()
    Handler.expected_token = token
    server.serve_forever()


if __name__ == "__main__":
    serve()
