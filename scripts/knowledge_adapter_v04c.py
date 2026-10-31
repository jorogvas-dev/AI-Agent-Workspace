#!/usr/bin/env python3
"""Authenticated, isolated knowledge-service adapter for planner v0.4c.

The endpoint is intentionally fixed to the test Docker service. Production
knowledge retrieval remains owned by the existing v0.3 workflow and is not
reachable through this adapter.
"""
from __future__ import annotations

import json
import hashlib
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


ISOLATED_ENDPOINT = "http://knowledge-service-test:8002/search"
ISOLATED_DOCUMENTS_ENDPOINT = "http://knowledge-service-test:8002/documents"
MAX_QUERY_LENGTH = 1000
MAX_SCOPED_QUERY_BYTES = 4000
MAX_LIMIT = 10
MAX_REQUEST_BYTES = 4096
MAX_SCOPED_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 1_000_000
MAX_EVIDENCE_BYTES = 40_000
MAX_DOCUMENT_SOURCE_BYTES = 512
MAX_DOCUMENT_PAGE = 50
MAX_DOCUMENT_OFFSET = 10_000
MAX_CATALOG_RESPONSE_BYTES = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirects are not permitted", headers, fp)


def _valid_text(value: Any, max_bytes: int, *, multiline: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    try:
        byte_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return (
        bool(value.strip()) and byte_length <= max_bytes
        and not any(
            (ord(char) < 32 or 127 <= ord(char) <= 159)
            and not (multiline and char in "\t\r\n") for char in value
        )
    )


def _validate_document(folder: Any, source: Any) -> None:
    if (
        not isinstance(folder, str) or folder not in {"input", "knowledge"}
        or not _valid_text(source, MAX_DOCUMENT_SOURCE_BYTES)
        or source != source.strip() or "\\" in source or ":" in source
        or any(part in {"", ".", ".."} for part in source.split("/"))
    ):
        raise KnowledgeAdapterError("Knowledge document identity is invalid")


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-standard JSON number")


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
        self._opener = opener or urllib.request.build_opener(_NoRedirect).open
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
            if not isinstance(text, str) or not text.strip():
                raise KnowledgeAdapterError("Knowledge service returned an invalid response")
            try:
                text_bytes = len(text.encode("utf-8"))
            except UnicodeEncodeError:
                raise KnowledgeAdapterError("Knowledge service returned an invalid response") from None
            if text_bytes > MAX_EVIDENCE_BYTES:
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
        payload = self._request("POST", self._endpoint, {"query": query, "limit": limit})
        return self._validate_response(payload, limit)

    def _request(
        self, method: str, url: str, payload: dict[str, Any] | None = None,
        *, max_request_bytes: int = MAX_REQUEST_BYTES,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        ensure_ascii: bool = True,
    ) -> Any:
        body = None if payload is None else json.dumps(
            payload, separators=(",", ":"), ensure_ascii=ensure_ascii, allow_nan=False
        ).encode("utf-8")
        if body is not None and len(body) > max_request_bytes:
            raise KnowledgeAdapterError("Knowledge request is too large")
        token = self._token()
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                # The default opener rejects redirects before forwarding auth.
                # Also reject changed URLs/statuses from injected transports.
                geturl = getattr(response, "geturl", None)
                if callable(geturl) and geturl() != url:
                    raise KnowledgeAdapterError("Knowledge service request failed")
                status = getattr(response, "status", 200)
                if isinstance(status, int) and status not in range(200, 300):
                    if status in {401, 403}:
                        raise KnowledgeAdapterError("Knowledge authentication failed")
                    raise KnowledgeAdapterError("Knowledge service request failed")
                raw = response.read(max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise KnowledgeAdapterError("Knowledge authentication failed") from None
            raise KnowledgeAdapterError("Knowledge service request failed") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise KnowledgeAdapterError("Knowledge service request failed") from None
        if not isinstance(raw, bytes) or len(raw) > max_response_bytes:
            raise KnowledgeAdapterError("Knowledge service response is too large")
        try:
            return json.loads(
                raw.decode("utf-8"), object_pairs_hook=_strict_object,
                parse_constant=_reject_constant
            )
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise KnowledgeAdapterError("Knowledge service returned invalid JSON") from None

    def search_document(self, query: str, *, folder: str, source: str, limit: int = 5) -> dict[str, Any]:
        """Search only one resolved indexed source; never use a global fallback."""
        _validate_document(folder, source)
        if not _valid_text(query, MAX_SCOPED_QUERY_BYTES, multiline=True):
            raise KnowledgeAdapterError("Knowledge scoped query is invalid")
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise KnowledgeAdapterError("Knowledge result limit is invalid")
        payload = self._request(
            "POST", ISOLATED_ENDPOINT,
            {"query": query, "limit": limit, "document": {"folder": folder, "source": source}},
            max_request_bytes=MAX_SCOPED_REQUEST_BYTES,
            ensure_ascii=False,
        )
        result = self._validate_response(payload, limit)
        if type(result.get("result_count")) is not int or result["result_count"] != len(result["results"]):
            raise KnowledgeAdapterError("Knowledge service returned an invalid response")
        seen_ids = set()
        for item in result["results"]:
            chunk_id = item.get("chunk_id")
            if (
                item.get("folder") != folder or item.get("source") != source
                or not _valid_text(chunk_id, 192)
                or not _valid_text(item.get("location"), 512)
                or not _valid_text(item.get("text"), MAX_EVIDENCE_BYTES, multiline=True)
                or chunk_id in seen_ids
            ):
                raise KnowledgeAdapterError("Knowledge service returned an invalid scoped response")
            seen_ids.add(chunk_id)
        return result

    def list_documents(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Read one bounded catalog page from the fixed isolated service."""
        if type(limit) is not int or not 1 <= limit <= MAX_DOCUMENT_PAGE:
            raise KnowledgeAdapterError("Knowledge catalog limit is invalid")
        if type(offset) is not int or not 0 <= offset <= MAX_DOCUMENT_OFFSET:
            raise KnowledgeAdapterError("Knowledge catalog offset is invalid")
        payload = self._request(
            "GET", f"{ISOLATED_DOCUMENTS_ENDPOINT}?limit={limit}&offset={offset}",
            max_response_bytes=MAX_CATALOG_RESPONSE_BYTES,
        )
        invalid = "Knowledge service returned an invalid document catalog"
        if not isinstance(payload, dict) or set(payload) != {"status", "documents", "result_count", "pagination"}:
            raise KnowledgeAdapterError(invalid)
        documents = payload["documents"]
        pagination = payload["pagination"]
        if (
            payload["status"] != "success" or not isinstance(documents, list) or len(documents) > limit
            or type(payload["result_count"]) is not int or payload["result_count"] != len(documents)
            or not isinstance(pagination, dict)
            or set(pagination) != {"limit", "offset", "total", "has_more", "next_offset"}
            or type(pagination["limit"]) is not int or pagination["limit"] != limit
            or type(pagination["offset"]) is not int or pagination["offset"] != offset
            or type(pagination["total"]) is not int or pagination["total"] < 0
            or type(pagination["has_more"]) is not bool
        ):
            raise KnowledgeAdapterError(invalid)
        total = pagination["total"]
        next_offset = offset + len(documents)
        has_more = next_offset < total
        expected_next = next_offset if has_more and next_offset <= MAX_DOCUMENT_OFFSET else None
        if (
            len(documents) != min(limit, max(0, total - offset))
            or pagination["has_more"] != has_more
            or (pagination["next_offset"] is not None and type(pagination["next_offset"]) is not int)
            or pagination["next_offset"] != expected_next
        ):
            raise KnowledgeAdapterError(invalid)
        seen_ids = set()
        for item in documents:
            if not isinstance(item, dict) or set(item) != {"document_id", "folder", "source"}:
                raise KnowledgeAdapterError(invalid)
            try:
                _validate_document(item["folder"], item["source"])
            except KnowledgeAdapterError:
                raise KnowledgeAdapterError(invalid) from None
            expected_id = hashlib.sha256(f"{item['folder']}:{item['source']}".encode("utf-8")).hexdigest()
            if item["document_id"] != expected_id or expected_id in seen_ids:
                raise KnowledgeAdapterError(invalid)
            seen_ids.add(expected_id)
        return payload


def create_isolated_adapter(token_file: Path, **kwargs: Any) -> KnowledgeServiceAdapter:
    """Create the only adapter exposed to v0.4c test mode."""
    return KnowledgeServiceAdapter(token_file, endpoint=ISOLATED_ENDPOINT, **kwargs)
