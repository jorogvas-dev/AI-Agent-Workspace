"""Offline catalog/scoped-retrieval tests; no credentials or HTTP are read."""
import hashlib
import json
import socket
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import knowledge_adapter_v04c as adapter_module


def document(folder="input", source="reports/record.pdf"):
    return {
        "document_id": hashlib.sha256(f"{folder}:{source}".encode()).hexdigest(),
        "folder": folder,
        "source": source,
    }


def catalog(documents=None, *, limit=50, offset=0, total=None):
    if documents is None:
        documents = [document()]
    if total is None:
        total = len(documents) + offset
    next_offset = offset + len(documents)
    has_more = next_offset < total
    return {
        "status": "success", "documents": documents, "result_count": len(documents),
        "pagination": {
            "limit": limit, "offset": offset, "total": total,
            "has_more": has_more,
            "next_offset": next_offset if has_more and next_offset <= 10000 else None,
        },
    }


def search_result(folder="input", source="reports/record.pdf", chunk_id="chunk-1"):
    return {
        "folder": folder, "source": source, "chunk_id": chunk_id,
        "location": "Page 2", "text": "Evidence from the selected document.\nA second line.",
    }


def search_payload(results=None):
    if results is None:
        results = [search_result()]
    return {"status": "success", "result_count": len(results), "results": results}


class Response:
    def __init__(self, payload, *, url=None, status=200):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.url = url
        self.status = status
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_limit = limit
        # Intentionally return the whole fixture to test the defensive bound.
        return self.raw


class KnowledgeAdapterV06bTests(unittest.TestCase):
    def setUp(self):
        # Any unexpected credential file access fails this test immediately.
        patcher = patch.object(Path, "read_text", side_effect=AssertionError("no credential reads"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_adapter(self, payload, *, status=200, response_url=None, error=None):
        def open_request(request, *, timeout):
            if error is not None:
                raise error
            self.last_response = Response(payload, url=response_url or request.full_url, status=status)
            return self.last_response
        opener = Mock(side_effect=open_request)
        adapter = adapter_module.create_isolated_adapter(Path("never-read-unit-test-credential"), opener=opener)
        token = patch.object(adapter, "_token", return_value="synthetic-unit-test-token")
        token.start()
        self.addCleanup(token.stop)
        return adapter, opener

    def test_authenticated_catalog_has_fixed_url_and_bounded_read(self):
        expected = catalog([document(), document("knowledge")], limit=2, offset=3, total=7)
        adapter, opener = self.make_adapter(expected)
        self.assertEqual(adapter.list_documents(limit=2, offset=3), expected)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "http://knowledge-service-test:8002/documents?limit=2&offset=3")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-unit-test-token")
        self.assertEqual(opener.call_args.kwargs["timeout"], 10.0)
        self.assertEqual(self.last_response.read_limit, adapter_module.MAX_CATALOG_RESPONSE_BYTES + 1)
        self.assertEqual({item["folder"] for item in expected["documents"]}, {"input", "knowledge"})
        self.assertNotEqual(expected["documents"][0]["document_id"], expected["documents"][1]["document_id"])

    def test_catalog_defaults_empty_last_page_and_maximum_offset(self):
        for expected in (
            catalog(), catalog([], total=0), catalog([document()], limit=2, offset=4, total=5),
            catalog([], limit=1, offset=10000, total=0),
            catalog([document()], limit=1, offset=10000, total=10002),
        ):
            with self.subTest(page=expected["pagination"]):
                adapter, _ = self.make_adapter(expected)
                page = expected["pagination"]
                self.assertEqual(adapter.list_documents(page["limit"], page["offset"]), expected)

    def test_catalog_rejects_bad_pagination_before_authentication_or_network(self):
        adapter, opener = self.make_adapter(catalog())
        for kwargs in (
            {"limit": 0}, {"limit": 51}, {"limit": True}, {"limit": "1"}, {"limit": 1.0},
            {"offset": -1}, {"offset": 10001}, {"offset": False}, {"offset": "0"}, {"offset": 0.0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.list_documents(**kwargs)
        adapter._token.assert_not_called()
        opener.assert_not_called()

    def test_catalog_rejects_invalid_or_excess_metadata(self):
        invalid_documents = []
        for key, value in (
            ("document_id", "wrong-identity"), ("folder", "output"), ("source", "../secret.pdf"),
            ("source", "/host/absolute.pdf"), ("source", "E:/private.pdf"), ("source", None),
            ("source", "https://example.invalid/private.pdf"), ("source", "x" * 513),
            ("source", "bad\x00.pdf"), ("sha256", "must-not-expose"), ("text", "must-not-expose"),
        ):
            item = document()
            item[key] = value
            invalid_documents.append(item)
        invalid_documents += [{"folder": "input", "source": "a.pdf"}, None, []]
        for item in invalid_documents:
            with self.subTest(item=item):
                adapter, _ = self.make_adapter(catalog([item]))
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.list_documents()
        adapter, _ = self.make_adapter(catalog([document(), document()]))
        with self.assertRaises(adapter_module.KnowledgeAdapterError):
            adapter.list_documents()

    def test_catalog_rejects_inconsistent_page_metadata(self):
        candidates = []
        for key, value in (
            ("limit", True), ("limit", 49), ("offset", False), ("offset", 1),
            ("total", -1), ("total", True), ("total", 2), ("has_more", True),
            ("has_more", 0), ("next_offset", 1), ("unexpected", "value"),
        ):
            payload = catalog()
            payload["pagination"][key] = value
            candidates.append(payload)
        for key, value in (("result_count", True), ("result_count", 2), ("status", "empty"), ("documents", {})):
            payload = catalog()
            payload[key] = value
            candidates.append(payload)
        for payload in candidates:
            with self.subTest(payload=payload):
                adapter, _ = self.make_adapter(payload)
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.list_documents()

    def test_scoped_search_authentication_selector_and_fewer_results(self):
        payload = search_payload()
        adapter, opener = self.make_adapter(payload)
        self.assertEqual(adapter.search_document("Which fields?", folder="input", source="reports/record.pdf", limit=8), payload)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, adapter_module.ISOLATED_ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-unit-test-token")
        self.assertEqual(json.loads(request.data), {
            "query": "Which fields?", "limit": 8,
            "document": {"folder": "input", "source": "reports/record.pdf"},
        })
        self.assertEqual(self.last_response.read_limit, adapter_module.MAX_RESPONSE_BYTES + 1)

    def test_scoped_zero_results_is_success_without_fallback(self):
        adapter, opener = self.make_adapter(search_payload([]))
        result = adapter.search_document("question", folder="input", source="empty.md")
        self.assertEqual(result, {"status": "success", "result_count": 0, "results": []})
        opener.assert_called_once()

    def test_mixed_sources_and_same_filename_different_folder_are_rejected(self):
        for competing in (
            search_result(folder="knowledge", chunk_id="other-folder"),
            search_result(source="other/record.pdf", chunk_id="other-source"),
        ):
            with self.subTest(competing=competing):
                adapter, _ = self.make_adapter(search_payload([search_result(), competing]))
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.search_document("question", folder="input", source="reports/record.pdf")

    def test_scoped_results_require_chunk_location_text_and_exact_identity(self):
        for key, value in (
            ("folder", None), ("source", None), ("chunk_id", None), ("chunk_id", ""),
            ("chunk_id", "x" * 193), ("chunk_id", "bad\nidentity"),
            ("location", ""), ("location", "x" * 513), ("location", "bad\x00location"),
            ("text", ""), ("text", 3), ("text", "x" * (adapter_module.MAX_EVIDENCE_BYTES + 1)),
            ("text", "bad\x00text"), ("text", "\ud800"),
        ):
            with self.subTest(key=key, value_type=type(value).__name__):
                item = search_result()
                item[key] = value
                adapter, _ = self.make_adapter(search_payload([item]))
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.search_document("question", folder="input", source="reports/record.pdf")
        for payload in (
            {"status": "success", "results": []},
            {"status": "success", "result_count": False, "results": []},
            search_payload([search_result(), search_result()]),
        ):
            adapter, _ = self.make_adapter(payload)
            with self.assertRaises(adapter_module.KnowledgeAdapterError):
                adapter.search_document("question", folder="input", source="reports/record.pdf")

    def test_invalid_selectors_rejected_before_authentication_or_network(self):
        adapter, opener = self.make_adapter(search_payload())
        for folder, source in (
            (None, "a.pdf"), ([], "a.pdf"), ("output", "a.pdf"), ("input", None),
            ("input", ""), ("input", "../a.pdf"), ("input", "a/../b.pdf"), ("input", "/a.pdf"),
            ("input", "C:/a.pdf"), ("input", "a\\b.pdf"), ("input", "a//b.pdf"),
            ("input", "a/./b.pdf"), ("input", "https://example.invalid/a.pdf"),
            ("input", "x" * 513), ("input", "bad\n.pdf"), ("input", "\ud800"),
            ("input", "bad\u0085.pdf"),
        ):
            with self.subTest(folder=folder, source=source):
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.search_document("question", folder=folder, source=source)
        adapter._token.assert_not_called()
        opener.assert_not_called()

    def test_scoped_4000_byte_query_boundary_and_legacy_limits(self):
        adapter, opener = self.make_adapter(search_payload([]))
        query = "é" * 2000
        adapter.search_document(query, folder="input", source="a.pdf")
        self.assertEqual(json.loads(opener.call_args.args[0].data)["query"], query)
        self.assertLessEqual(len(opener.call_args.args[0].data), adapter_module.MAX_SCOPED_REQUEST_BYTES)
        opener.reset_mock()
        for query in ("é" * 2001, "x" * 4001, "", " ", None):
            with self.subTest(query_type=type(query).__name__):
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.search_document(query, folder="input", source="a.pdf")
        for limit in (0, 11, True, "5", 1.0):
            with self.assertRaises(adapter_module.KnowledgeAdapterError):
                adapter.search_document("question", folder="input", source="a.pdf", limit=limit)
        with self.assertRaises(adapter_module.KnowledgeAdapterError):
            adapter.search("x" * 1001)
        with self.assertRaisesRegex(adapter_module.KnowledgeAdapterError, "request is too large"):
            adapter.search("é" * 1000)
        opener.assert_not_called()

    def test_legacy_search_schema_and_request_are_unchanged(self):
        legacy_item = {"text": "Legacy evidence\fpage break", "source": "legacy.pdf", "location": "Page 1"}
        adapter, opener = self.make_adapter(search_payload([legacy_item]))
        result = adapter.search("legacy", 5)
        self.assertEqual(result["results"], [legacy_item])
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, adapter_module.ISOLATED_ENDPOINT)
        self.assertEqual(json.loads(request.data), {"query": "legacy", "limit": 5})
        self.assertEqual(adapter_module.MAX_REQUEST_BYTES, 4096)
        self.assertEqual(adapter_module.MAX_QUERY_LENGTH, 1000)
        self.assertEqual(adapter_module.MAX_LIMIT, 10)

    def test_http_authentication_errors_and_timeouts_are_sanitized(self):
        for status in (401, 403, 302, 500):
            error = urllib.error.HTTPError(adapter_module.ISOLATED_ENDPOINT, status, "private detail", {}, None)
            self.addCleanup(error.close)
            for method in ("catalog", "scoped"):
                with self.subTest(status=status, method=method):
                    adapter, _ = self.make_adapter({}, error=error)
                    with self.assertRaises(adapter_module.KnowledgeAdapterError) as caught:
                        if method == "catalog":
                            adapter.list_documents()
                        else:
                            adapter.search_document("question", folder="input", source="a.pdf")
                    self.assertNotIn("private detail", str(caught.exception))
                    self.assertNotIn("synthetic-unit-test-token", str(caught.exception))
                    if status in {401, 403}:
                        self.assertEqual(str(caught.exception), "Knowledge authentication failed")
        for error in (socket.timeout("private timeout"), urllib.error.URLError("private connection detail")):
            adapter, _ = self.make_adapter({}, error=error)
            with self.assertRaisesRegex(adapter_module.KnowledgeAdapterError, "request failed"):
                adapter.list_documents()

    def test_response_size_and_strict_json_limits(self):
        for raw in (
            b"not-json", b'{"status":"success","status":"other"}', b'{"value":NaN}',
            b"x" * (adapter_module.MAX_RESPONSE_BYTES + 1),
        ):
            with self.subTest(size=len(raw)):
                adapter, _ = self.make_adapter(raw)
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.search_document("question", folder="input", source="a.pdf")
        adapter, _ = self.make_adapter(b"x" * (adapter_module.MAX_CATALOG_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(adapter_module.KnowledgeAdapterError, "response is too large"):
            adapter.list_documents()

    def test_redirected_responses_are_rejected(self):
        for url in ("http://knowledge-service:8002/search", "http://other.invalid/search"):
            adapter, _ = self.make_adapter(search_payload(), response_url=url)
            with self.assertRaises(adapter_module.KnowledgeAdapterError):
                adapter.search_document("question", folder="input", source="reports/record.pdf")
        adapter, _ = self.make_adapter(search_payload(), status=302)
        with self.assertRaises(adapter_module.KnowledgeAdapterError):
            adapter.search("legacy")

    def test_default_transport_blocks_redirects_before_forwarding_credentials(self):
        director = Mock()
        with patch.object(adapter_module.urllib.request, "build_opener", return_value=director) as builder:
            adapter = adapter_module.create_isolated_adapter(Path("never-read-unit-test-credential"))
        director.open.assert_not_called()
        handler = builder.call_args.args[0]()
        self.assertIsInstance(handler, adapter_module._NoRedirect)
        request = urllib.request.Request(adapter_module.ISOLATED_ENDPOINT)
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    handler.redirect_request(request, None, status, "redirect", {}, "http://knowledge-service:8002/search")
                caught.exception.close()

    def test_credential_failure_is_sanitized_and_production_endpoint_is_disallowed(self):
        adapter, opener = self.make_adapter(catalog())
        adapter._token.side_effect = adapter_module.KnowledgeAdapterError("Knowledge credential is unavailable")
        with self.assertRaisesRegex(adapter_module.KnowledgeAdapterError, "credential is unavailable"):
            adapter.list_documents()
        opener.assert_not_called()
        with self.assertRaises(ValueError):
            adapter_module.KnowledgeServiceAdapter(
                Path("never-read-unit-test-credential"), endpoint="http://knowledge-service:8002/search"
            )


if __name__ == "__main__":
    unittest.main()
