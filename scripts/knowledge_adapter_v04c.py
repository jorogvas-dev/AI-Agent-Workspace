#!/usr/bin/env python3
"""Authenticated, isolated knowledge-service adapter for planner v0.4c.

The endpoint is intentionally fixed to the test Docker service. Production
knowledge retrieval remains owned by the existing v0.3 workflow and is not
reachable through this adapter.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


ISOLATED_ENDPOINT = "http://knowledge-service-test:8002/search"
MAX_QUERY_LENGTH = 1000
MAX_LIMIT = 10
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 1_000_000
MAX_EVIDENCE_BYTES = 40_000


class KnowledgeAdapterError(RuntimeError):
    """Safe, client-facing adapter error without token or response leakage."""


class KnowledgeServiceAdapter:
    """Read-only adapter restricted to the isolated knowledge service."""

    def __init__(
        self,
        token_file: Path,
        *,
        endpoint: str = ISOLATED_ENDPOINT,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if endpoint != ISOLATED_ENDPOINT:
            raise ValueError("Only the isolated knowledge-service endpoint is permitted")
        if not isinstance(token_file, Path):
            raise ValueError("token_file must be a Path")
        if not 1.0 <= timeout_seconds <= 30.0:
            raise ValueError("timeout_seconds must be between 1 and 30")
        self._token_file = token_file
        self._endpoint = endpoint
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout_seconds

    def _token(self) -> str:
        try:
            token = self._token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise KnowledgeAdapterError("Knowledge credential is unavailable") from exc
        if not token:
            raise KnowledgeAdapterError("Knowledge credential is unavailable")
        return token

    @staticmethod
    def _validate_request(query: Any, limit: Any) -> None:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            raise KnowledgeAdapterError("Knowledge query is invalid")
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise KnowledgeAdapterError("Knowledge result limit is invalid")

    @staticmethod
    def _validate_response(payload: Any, limit: int) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise KnowledgeAdapterError("Knowledge service returned an invalid response")
        results = payload.get("results")
        if not isinstance(results, list) or len(results) > limit:
            raise KnowledgeAdapterError("Knowledge service returned an invalid response")
        result_count = payload.get("result_count")
        if result_count is not None and (type(result_count) is not int or result_count != len(results)):
            raise KnowledgeAdapterError("Knowledge service returned an invalid response")
        checked: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                raise KnowledgeAdapterError("Knowledge service returned an invalid response")
            text = item.get("text")
            source = item.get("source")
            location = item.get("location")
            chunk_id = item.get("chunk_id")
            if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_EVIDENCE_BYTES:
                raise KnowledgeAdapterError("Knowledge service returned an invalid response")
            if not isinstance(source, str) or not source.strip():
                raise KnowledgeAdapterError("Knowledge service returned an invalid response")
            if not isinstance(location, str) or not location.strip():
                raise KnowledgeAdapterError("Knowledge service returned an invalid response")
            if chunk_id is not None and not isinstance(chunk_id, str):
                raise KnowledgeAdapterError("Knowledge service returned an invalid response")
            checked.append(dict(item))
        result = dict(payload)
        result["results"] = checked
        return result

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        self._validate_request(query, limit)
        token = self._token()
        body = json.dumps({"query": query, "limit": limit}, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise KnowledgeAdapterError("Knowledge request is too large")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise KnowledgeAdapterError("Knowledge authentication failed") from None
            raise KnowledgeAdapterError("Knowledge service request failed") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise KnowledgeAdapterError("Knowledge service request failed") from None
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise KnowledgeAdapterError("Knowledge service response is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise KnowledgeAdapterError("Knowledge service returned invalid JSON") from None
        return self._validate_response(payload, limit)


def create_isolated_adapter(token_file: Path, **kwargs: Any) -> KnowledgeServiceAdapter:
    """Create the only adapter exposed to v0.4c test mode."""
    return KnowledgeServiceAdapter(token_file, endpoint=ISOLATED_ENDPOINT, **kwargs)
