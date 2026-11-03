"""Offline production-profile tests: no real credentials, sockets, or models."""
import builtins
import copy
import hashlib
import inspect
import io
import json
import os
import socket
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, mock_open, patch
from urllib.parse import urlsplit

from scripts import document_service_production as service
from scripts import knowledge_adapter_v04c as knowledge
from tools import analyze_document_production as tool


TOKEN = "unit-production-document-token"
KNOWLEDGE_TOKEN = "unit-production-knowledge-token"
REQUEST = (" \tExtract sample_mass from input/reports/record.pdf.\n"
           "Include its quotation, source, unit, and page location. ")
REAL_ANALYZE = service.pipeline.analyze_request


def document(folder="input", source="reports/record.pdf"):
    return {"document_id": hashlib.sha256(f"{folder}:{source}".encode()).hexdigest(), "folder": folder, "source": source}


def catalog():
    return {"status": "success", "documents": [document()], "result_count": 1,
            "pagination": {"limit": 50, "offset": 0, "total": 1, "has_more": False, "next_offset": None}}


def chunk():
    return {"folder": "input", "source": document()["source"], "chunk_id": "production-chunk",
            "location": "Page 2", "text": "Sample mass:\r\n16.00 g\r\nReference number: 7302"}


def evidence():
    return {"status": "success", "result_count": 1, "results": [chunk()]}


def pipeline_result(status="success"):
    numeric = {"name": "sample_mass", "status": "found", "value_type": "numeric", "value": "16.00",
               "source": document()["source"], "chunk_id": "production-chunk", "location": "Page 2",
               "quote": chunk()["text"]}
    text = {"name": "material", "status": "found", "value_type": "text", "value": "Alloy",
            "source": document()["source"], "chunk_id": "material-chunk", "location": "Page 3",
            "quote": "Material:\tAlloy\n"}
    result = {"version": "0.6a", "pipeline_version": "0.6i", "valid": True, "success": status == "success",
              "status": status, "source": document()["source"], "selected_document": document(),
              "extraction_request": {"task_type": "field_extraction", "fields": [
                  {"name": "sample_mass", "value_type": "numeric", "question": "What is the sample mass?"},
              ]}, "fields": [numeric, text], "errors": [], "model_calls": 2,
              "execution_performed": False, "evidence_validation": "structural_only; semantic_correctness_not_verified"}
    if status == "missing":
        result["fields"][0] = {"name": "sample_mass", "value_type": "numeric", "status": "missing"}
        result["errors"] = [{"code": "missing_field", "message": "No verified candidate was returned.", "field": "sample_mass"}]
    elif status == "ambiguous":
        candidate = {key: value for key, value in numeric.items() if key not in {"name", "status"}}
        other = {**candidate, "value": "17.50", "chunk_id": "other-mass", "quote": "Sample mass: 17.50 g"}
        result["fields"][0] = {"name": "sample_mass", "value_type": "numeric", "status": "ambiguous",
                               "candidates": [candidate, other]}
        result["errors"] = [{"code": "ambiguous_field", "message": "No candidate was selected.", "field": "sample_mass"}]
    elif status == "invalid_evidence":
        result.update(valid=False, fields=[{"name": "sample_mass", "value_type": "numeric", "status": status}],
                      errors=[{"code": status, "message": "Invalid source evidence.", "field": "sample_mass"}])
    elif status in {"unsupported_task_type", "document_ambiguous"}:
        result.update(valid=False, fields=[], extraction_request=None, model_calls=1 if status == "unsupported_task_type" else 0,
                      errors=[{"code": status, "message": "Request was not analyzed."}])
        if status == "document_ambiguous":
            result.update(source=None, selected_document=None, choices=[document(), document("knowledge")], choices_truncated=False)
    return result


def public_result(payload):
    return {key: copy.deepcopy(value) for key, value in payload.items() if key != "extraction_request"}


class Response:
    def __init__(self, payload, url, status=200):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.url, self.status, self.read_limit = url, status, None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_limit = limit
        return self.raw  # Deliberately permit faulty over-reads for defensive-bound tests.


class LocalHandler(service.Handler):
    def __init__(self, application, path="/analyze-document", body=None, authorization="Bearer " + TOKEN):
        body = json.dumps({"request": REQUEST}, ensure_ascii=False).encode() if body is None else body
        self.path, self.service, self.expected_token = path, application, TOKEN
        self.headers = {"Authorization": authorization, "Content-Length": str(len(body))}
        self.rfile = Mock(wraps=io.BytesIO(body))
        self.wfile = io.BytesIO()
        self.status, self.response_headers = None, {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass

    def result(self):
        return json.loads(self.wfile.getvalue())


class OfflineTests(unittest.TestCase):
    def setUp(self):
        for target in (
            patch.object(Path, "read_text", side_effect=AssertionError("no real credential reads")),
            patch.object(Path, "open", side_effect=AssertionError("no real credential reads")),
            patch.object(Path, "write_text", side_effect=AssertionError("no writes")),
            patch.object(urllib.request.OpenerDirector, "open", side_effect=AssertionError("no real HTTP")),
            patch.object(socket, "create_connection", side_effect=AssertionError("no real sockets")),
        ):
            self.start_patch(target)

    def start_patch(self, target):
        result = target.start()
        self.addCleanup(target.stop)
        return result


class ProductionAdapterTests(OfflineTests):
    def make_adapter(self, payload, production=True, *, status=200, response_url=None, error=None):
        def open_request(request, *, timeout):
            if error is not None:
                raise error
            self.last_response = Response(payload, response_url or request.full_url, status)
            return self.last_response
        opener = Mock(side_effect=open_request)
        factory = knowledge.create_production_adapter if production else knowledge.create_isolated_adapter
        adapter = factory(Path("unused-unit-credential"), opener=opener)
        self.start_patch(patch.object(adapter, "_token", return_value=KNOWLEDGE_TOKEN))
        return adapter, opener

    def test_fixed_profiles_use_separate_search_scoped_search_and_catalog_destinations(self):
        for production, search, documents in (
            (True, "http://knowledge-service:8002/search", "http://knowledge-service:8002/documents"),
            (False, "http://knowledge-service-test:8002/search", "http://knowledge-service-test:8002/documents"),
        ):
            for method in ("search", "scoped", "catalog"):
                with self.subTest(production=production, method=method):
                    expected = catalog() if method == "catalog" else evidence()
                    adapter, opener = self.make_adapter(expected, production)
                    if method == "catalog":
                        result = adapter.list_documents()
                    elif method == "scoped":
                        result = adapter.search_document("sample mass", folder="input", source=document()["source"], limit=4)
                    else:
                        result = adapter.search("sample mass", limit=4)
                    self.assertEqual(result, expected)
                    request = opener.call_args.args[0]
                    self.assertEqual(request.full_url, documents + "?limit=50&offset=0" if method == "catalog" else search)
                    self.assertEqual(request.get_header("Authorization"), "Bearer " + KNOWLEDGE_TOKEN)
                    self.assertEqual(opener.call_args.kwargs["timeout"], 10.0)
                    self.assertEqual(self.last_response.read_limit,
                                     (knowledge.MAX_CATALOG_RESPONSE_BYTES if method == "catalog" else knowledge.MAX_RESPONSE_BYTES) + 1)
                    if method == "scoped":
                        self.assertEqual(json.loads(request.data)["document"], {"folder": "input", "source": document()["source"]})

    def test_public_constructors_and_factories_cannot_cross_environments_or_select_arbitrary_urls(self):
        for cls, allowed, denied in (
            (knowledge.KnowledgeServiceAdapter, knowledge.ISOLATED_ENDPOINT, knowledge.PRODUCTION_ENDPOINT),
            (knowledge.ProductionKnowledgeServiceAdapter, knowledge.PRODUCTION_ENDPOINT, knowledge.ISOLATED_ENDPOINT),
        ):
            for endpoint in (denied, "http://other.invalid/search", allowed + "?endpoint=other", allowed + "/", "https://knowledge-service:8002/search"):
                with self.assertRaises(ValueError):
                    cls(Path("unused"), endpoint=endpoint)
        for factory, denied in ((knowledge.create_isolated_adapter, knowledge.PRODUCTION_ENDPOINT),
                                (knowledge.create_production_adapter, knowledge.ISOLATED_ENDPOINT)):
            with self.assertRaises(TypeError):
                factory(Path("unused"), endpoint=denied)
        for cls in (knowledge.KnowledgeServiceAdapter, knowledge.ProductionKnowledgeServiceAdapter):
            with self.assertRaises(ValueError):
                cls("not-a-Path")
            for timeout in (0, 31):
                with self.assertRaises(ValueError):
                    cls(Path("unused"), timeout_seconds=timeout)

    def test_transport_rejects_foreign_profile_urls_before_credentials_or_io(self):
        for production in (False, True):
            adapter, opener = self.make_adapter(evidence(), production)
            foreign = knowledge.ISOLATED_ENDPOINT if production else knowledge.PRODUCTION_ENDPOINT
            for method, url in (("POST", foreign), ("GET", foreign.replace("/search", "/documents")),
                                ("POST", "http://other.invalid/search"), ("DELETE", adapter._endpoint)):
                with self.assertRaises(knowledge.KnowledgeAdapterError):
                    adapter._request(method, url)
            adapter._token.assert_not_called()
            opener.assert_not_called()

    def test_scoped_validation_rejects_wrong_source_folder_duplicate_and_invalid_evidence(self):
        invalid = []
        for key, value in (("folder", "knowledge"), ("source", "other.pdf"), ("chunk_id", ""),
                           ("chunk_id", "x" * 193), ("location", "bad\x00location"), ("location", "x" * 513),
                           ("text", ""), ("text", "bad\x00text"), ("text", "\ud800"),
                           ("text", "x" * (knowledge.MAX_EVIDENCE_BYTES + 1))):
            item = chunk()
            item[key] = value
            invalid.append({"status": "success", "result_count": 1, "results": [item]})
        invalid += [{"status": "success", "result_count": 2, "results": [chunk(), chunk()]},
                    {"status": "success", "result_count": True, "results": [chunk()]},
                    {"status": "success", "results": [chunk()]}]
        for payload in invalid:
            adapter, opener = self.make_adapter(payload)
            with self.assertRaises(knowledge.KnowledgeAdapterError):
                adapter.search_document("sample mass", folder="input", source=document()["source"], limit=4)
            opener.assert_called_once()
            self.assertEqual(opener.call_args.args[0].full_url, knowledge.PRODUCTION_ENDPOINT)

    def test_catalog_validation_and_invalid_selectors_fail_closed(self):
        for mutate in (
            lambda page: page["documents"][0].update(document_id="wrong"),
            lambda page: page["documents"][0].update(source="../secret.pdf"),
            lambda page: page["documents"][0].update(text="private document body"),
            lambda page: page["pagination"].update(next_offset=1),
            lambda page: page["pagination"].update(total=True),
            lambda page: page.update(result_count=2),
        ):
            payload = catalog()
            mutate(payload)
            adapter, _ = self.make_adapter(payload)
            with self.assertRaises(knowledge.KnowledgeAdapterError):
                adapter.list_documents()
        adapter, opener = self.make_adapter(evidence())
        for folder, source in (("production", "record.pdf"), ("input", "http://knowledge-service-test:8002/documents"),
                               ("knowledge", "../record.pdf")):
            with self.assertRaises(knowledge.KnowledgeAdapterError):
                adapter.search_document("question", folder=folder, source=source)
        for options in ({"limit": True}, {"offset": -1}, {"limit": 51}, {"offset": 10001}):
            with self.assertRaises(knowledge.KnowledgeAdapterError):
                adapter.list_documents(**options)
        adapter._token.assert_not_called()
        opener.assert_not_called()

    def test_empty_results_and_transport_failures_never_fall_back(self):
        empty = {"status": "success", "result_count": 0, "results": []}
        adapter, opener = self.make_adapter(empty)
        self.assertEqual(adapter.search_document("question", folder="input", source=document()["source"]), empty)
        opener.assert_called_once()
        for error in (socket.timeout("private detail"), urllib.error.URLError("private detail"),
                      urllib.error.HTTPError(knowledge.PRODUCTION_ENDPOINT, 401, "private detail", {}, None)):
            self.addCleanup(getattr(error, "close", lambda: None))
            adapter, opener = self.make_adapter({}, error=error)
            with self.assertRaises(knowledge.KnowledgeAdapterError) as caught:
                adapter.search_document("question", folder="input", source=document()["source"])
            self.assertNotIn("private", str(caught.exception))
            if isinstance(error, urllib.error.HTTPError):
                self.assertTrue(error.closed)
            opener.assert_called_once()
            self.assertEqual(opener.call_args.args[0].full_url, knowledge.PRODUCTION_ENDPOINT)

    def test_request_and_response_transport_bounds_and_strict_json(self):
        for production in (False, True):
            for method, maximum, payload in (("catalog", knowledge.MAX_CATALOG_RESPONSE_BYTES, catalog()),
                                              ("scoped", knowledge.MAX_RESPONSE_BYTES, evidence())):
                base = json.dumps(payload).encode()
                for extra in (0, 1):
                    adapter, _ = self.make_adapter(base + b" " * (maximum - len(base) + extra), production)
                    action = adapter.list_documents if method == "catalog" else lambda: adapter.search_document(
                        "question", folder="input", source=document()["source"])
                    if extra:
                        with self.assertRaises(knowledge.KnowledgeAdapterError):
                            action()
                    else:
                        self.assertEqual(action(), payload)
                    self.assertEqual(self.last_response.read_limit, maximum + 1)
            adapter, opener = self.make_adapter(evidence(), production)
            overhead = len(json.dumps({"query": ""}, separators=(",", ":")).encode())
            for extra in (0, 1):
                payload = {"query": "x" * (knowledge.MAX_REQUEST_BYTES - overhead + extra)}
                if extra:
                    with self.assertRaises(knowledge.KnowledgeAdapterError):
                        adapter._request("POST", adapter._endpoint, payload)
                else:
                    adapter._request("POST", adapter._endpoint, payload)
                    self.assertEqual(len(opener.call_args.args[0].data), knowledge.MAX_REQUEST_BYTES)
            self.assertEqual(opener.call_count, 1)
        for raw in (b"\xff", b"not-json", b'{"status":"success","status":"success"}', b'{"value":NaN}'):
            adapter, _ = self.make_adapter(raw)
            with self.assertRaises(knowledge.KnowledgeAdapterError):
                adapter.list_documents()

    def test_redirects_are_rejected_before_forwarding_auth_for_both_profiles(self):
        for production in (False, True):
            for code in (301, 302, 303, 307, 308):
                requests, forwarded = [], []
                target = knowledge.ISOLATED_ENDPOINT if production else knowledge.PRODUCTION_ENDPOINT
                def build(handler_class):
                    def redirect(request, timeout):
                        requests.append(request)
                        redirected = handler_class().redirect_request(request, None, code, "private detail", {}, target)
                        forwarded.append(redirected)
                        raise AssertionError("redirect was allowed")
                    return types.SimpleNamespace(open=redirect)
                with patch.object(knowledge.urllib.request, "build_opener", side_effect=build):
                    factory = knowledge.create_production_adapter if production else knowledge.create_isolated_adapter
                    adapter = factory(Path("unused"))
                    with patch.object(adapter, "_token", return_value=KNOWLEDGE_TOKEN):
                        with self.assertRaises(knowledge.KnowledgeAdapterError):
                            adapter.list_documents()
                self.assertEqual(len(requests), 1)
                self.assertEqual(forwarded, [])
                self.assertEqual(urlsplit(requests[0].full_url).hostname,
                                 "knowledge-service" if production else "knowledge-service-test")

    def test_injected_transports_cannot_hide_redirects_or_error_statuses(self):
        for production in (False, True):
            wrong = knowledge.ISOLATED_ENDPOINT if production else knowledge.PRODUCTION_ENDPOINT
            for options in ({"response_url": wrong}, {"status": 302}, {"status": 401}, {"status": 500}):
                adapter, opener = self.make_adapter(evidence(), production, **options)
                with self.assertRaises(knowledge.KnowledgeAdapterError):
                    adapter.search_document("question", folder="input", source=document()["source"])
                opener.assert_called_once()


class ProductionServiceTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.knowledge_open = Mock(side_effect=AssertionError("unexpected knowledge HTTP"))
        self.adapter = knowledge.create_production_adapter(Path(service.KNOWLEDGE_TOKEN_FILE), opener=self.knowledge_open)
        self.start_patch(patch.object(self.adapter, "_token", return_value=KNOWLEDGE_TOKEN))
        self.model = Mock(name="structured_model")
        self.application = service.DocumentService(self.model, self.adapter)
        self.analyze = self.start_patch(patch.object(service.pipeline, "analyze_request", return_value=pipeline_result()))

    def assert_slots_available(self):
        acquired = 0
        try:
            for _ in range(2):
                self.assertTrue(self.application._slots.acquire(blocking=False))
                acquired += 1
            self.assertFalse(self.application._slots.acquire(blocking=False))
        finally:
            for _ in range(acquired):
                self.application._slots.release()

    def test_health_identifies_production_and_only_document_route_is_exposed(self):
        handler = LocalHandler(self.application, path="/health", authorization="")
        handler.do_GET()
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.result(), {"status": "ok", "profile": "production", "version": "0.6j", "pipeline_version": "0.6i"})
        forbidden = ("/analyze", "/commit", "/COMMIT", "/reconcile", "/prepare", "/approvals", "/index", "/index-folder",
                     "/files", "/write", "/execute", "/run", "/analyze-document/", "/analyze-document?commit=true", "/health?debug=true")
        for path in forbidden:
            for method in ("do_GET", "do_POST"):
                handler = LocalHandler(self.application, path=path)
                getattr(handler, method)()
                self.assertEqual(handler.status, 404)
                handler.rfile.read.assert_not_called()
        for path, method in (("/health", "do_POST"), ("/analyze-document", "do_GET")):
            handler = LocalHandler(self.application, path=path)
            getattr(handler, method)()
            self.assertEqual(handler.status, 404)
        self.assertFalse(hasattr(self.application, "analyze"))
        self.analyze.assert_not_called()

    def test_analysis_authentication_precedes_body_reads_and_pipeline_access(self):
        for authorization in (None, "", "Bearer wrong", "Basic " + TOKEN, "Bearer non-ascii-é", 42):
            handler = LocalHandler(self.application, authorization=authorization)
            handler.do_POST()
            self.assertEqual(handler.status, 401)
            self.assertEqual(handler.result(), {"error": "Unauthorized"})
            handler.rfile.read.assert_not_called()
        self.assertFalse(service.authorize_service_request("Bearer ", ""))
        self.analyze.assert_not_called()
        self.assert_slots_available()

    def test_full_request_and_complete_pipeline_results_are_preserved(self):
        for status in ("success", "missing", "ambiguous", "unsupported_task_type", "document_ambiguous", "invalid_evidence"):
            expected = pipeline_result(status)
            self.analyze.return_value = expected
            handler = LocalHandler(self.application)
            handler.do_POST()
            self.assertEqual(handler.status, 200)
            self.assertEqual(handler.result(), expected)
            self.analyze.assert_called_with(REQUEST, adapter=self.adapter, client=self.model)
            self.assertEqual(int(handler.response_headers["Content-Length"]), len(handler.wfile.getvalue()))
            self.assertEqual(handler.response_headers["Content-Type"], "application/json")
            self.assert_slots_available()

    def test_unavailable_service_is_sanitized_without_pipeline_access(self):
        handler = LocalHandler(None)
        handler.do_POST()
        self.assertEqual(handler.status, 400)
        self.assertEqual(handler.result(), {"valid": False, "error": "document service unavailable"})
        self.analyze.assert_not_called()

    def test_strict_json_malformed_request_types_and_extra_keys_are_rejected(self):
        bodies = [b"not-json", b'{"request":"\xff"}', b'{"request":"bad\x00value"}',
                  b'{"request":"a","request":"b"}', b'{"request":"a","reque\\u0073t":"b"}',
                  b'{"request":NaN}', b'{"request":Infinity}', b'{"request":"\\ud800"}',
                  b'{"request":"a","extra":{"x":1,"x":2}}']
        bodies += [json.dumps(payload).encode() for payload in (
            None, [], 1, "request", {}, {"request": None}, {"request": True}, {"request": []}, {"request": {}},
            {"request": ""}, {"request": "  "}, {"request": "x" * 4001},
            {"request": REQUEST, "endpoint": knowledge.ISOLATED_ENDPOINT}, {"request": REQUEST, "commit": True},
        )]
        for body in bodies:
            with self.subTest(body=body[:60]):
                handler = LocalHandler(self.application, body=body)
                handler.do_POST()
                self.assertEqual(handler.status, 400)
                self.assertFalse(handler.result()["valid"])
        self.analyze.assert_not_called()

    def test_input_byte_length_and_character_bounds_are_not_truncated(self):
        for length in ("0", "-1", "bad", str(service.MAX_REQUEST_BYTES + 1)):
            handler = LocalHandler(self.application)
            handler.headers["Content-Length"] = length
            handler.do_POST()
            self.assertEqual(handler.status, 400)
            handler.rfile.read.assert_not_called()
        handler = LocalHandler(self.application)
        handler.headers["Content-Length"] = str(len(handler.rfile._mock_wraps.getvalue()) + 1)
        handler.do_POST()
        self.assertEqual(handler.status, 400)
        overhead = len(json.dumps({"request": ""}, ensure_ascii=False).encode())
        request = "€" * 2200
        request += "x" * (service.MAX_REQUEST_BYTES - overhead - len(request.encode()))
        raw = json.dumps({"request": request}, ensure_ascii=False).encode()
        self.assertEqual(len(raw), 8 * 1024)
        handler = LocalHandler(self.application, body=raw)
        handler.do_POST()
        self.assertEqual(handler.status, 200)
        self.analyze.assert_called_once_with(request, adapter=self.adapter, client=self.model)

    def test_exceptions_and_response_serialization_failures_are_sanitized_and_release_slots(self):
        for error in (RuntimeError("private token and path"), service.ServiceError("private token and path")):
            self.analyze.side_effect = error
            handler = LocalHandler(self.application)
            handler.do_POST()
            self.assertEqual(handler.status, 500)
            self.assertEqual(handler.result(), {"valid": False, "error": "document service failure"})
            self.assertNotIn("private", handler.wfile.getvalue().decode())
            self.assert_slots_available()
        self.analyze.side_effect = None
        for payload in (None, [], {"value": float("nan")}, {"debug": "\ud800"}):
            self.analyze.return_value = payload
            handler = LocalHandler(self.application)
            handler.do_POST()
            self.assertEqual(handler.status, 500)
            self.assert_slots_available()

    def test_response_byte_cap_accepts_boundary_and_rejects_overflow(self):
        overhead = len(json.dumps({"value": ""}, ensure_ascii=False, sort_keys=True).encode())
        for extra, expected in ((0, 200), (1, 500)):
            self.analyze.return_value = {"value": "x" * (service.MAX_RESPONSE_BYTES - overhead + extra)}
            handler = LocalHandler(self.application)
            handler.do_POST()
            self.assertEqual(handler.status, expected)
            self.assertLessEqual(len(handler.wfile.getvalue()), 96 * 1024)
            if extra:
                self.assertEqual(handler.result(), {"valid": False, "error": "response too large"})
            self.assert_slots_available()

    def test_two_shared_slots_block_a_third_request_and_release_after_workers_finish(self):
        entered, release = threading.Event(), threading.Event()
        lock, results, errors = threading.Lock(), [], []
        count = 0
        def analyze(*args, **kwargs):
            nonlocal count
            with lock:
                count += 1
                if count == 2:
                    entered.set()
            if not release.wait(3):
                raise RuntimeError("test worker was not released")
            return pipeline_result()
        def worker():
            try:
                results.append(self.application.analyze_document(REQUEST))
            except Exception as exc:
                errors.append(exc)
        self.analyze.side_effect = analyze
        workers = [threading.Thread(target=worker) for _ in range(2)]
        real_acquire = self.application._slots.acquire
        with patch.object(self.application._slots, "acquire", side_effect=lambda **kwargs: real_acquire(blocking=False)):
            try:
                for thread in workers:
                    thread.start()
                self.assertTrue(entered.wait(3))
                handler = LocalHandler(self.application)
                handler.do_POST()
                self.assertEqual(handler.status, 400)
                self.assertEqual(handler.result()["error"], "document service is busy")
                self.assertEqual(self.analyze.call_count, 2)
            finally:
                release.set()
                for thread in workers:
                    thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assert_slots_available()

    def test_production_factory_and_credentials_are_fixed_and_isolated_adapter_is_refused(self):
        with patch.dict(os.environ, {"KNOWLEDGE_TOKEN_FILE": "/forbidden", "PLANNER_API_TOKEN": "wrong", "PLANNER_BIND_PORT": "9000"}), patch.object(
            service.knowledge_adapter, "create_production_adapter", return_value=self.adapter,
        ) as factory, patch.object(service, "IntentLMStudioClient", return_value=self.model) as client, patch.object(
            knowledge, "create_isolated_adapter", side_effect=AssertionError("no isolated factory"),
        ):
            application = service.build_service()
        self.assertIs(application.adapter, self.adapter)
        factory.assert_called_once_with(Path("/run/secrets/agent_knowledge_token"), timeout_seconds=10.0)
        client.assert_called_once_with(timeout=30.0)
        isolated = knowledge.KnowledgeServiceAdapter(Path("unused"), opener=Mock())
        with self.assertRaisesRegex(service.ServiceError, "production knowledge adapter"):
            service.build_service(model_client=self.model, adapter=isolated)
        with self.assertRaisesRegex(service.ServiceError, "production knowledge adapter"):
            service.DocumentService(self.model, isolated)

    def test_only_fixed_production_auth_file_is_read_with_a_bound(self):
        reads = []
        credential = mock_open(read_data=TOKEN + "\n").return_value
        def open_credential(path, *args, **kwargs):
            reads.append((path, args, kwargs))
            return credential
        with patch.dict(os.environ, {"PLANNER_API_TOKEN": "wrong", "PLANNER_API_TOKEN_FILE": "/forbidden"}), patch.object(
            Path, "open", new=open_credential,
        ):
            self.assertEqual(service._service_token(), TOKEN)
        self.assertEqual(reads, [(Path("/run/secrets/document_planner_prod_token"), ("r",), {"encoding": "utf-8"})])
        credential.read.assert_called_once_with(1025)
        for value in ("", " ", "x" * 513, " " * 1024 + "x", "bad\nvalue"):
            with patch.object(Path, "open", new=lambda *args, **kwargs: mock_open(read_data=value).return_value):
                with self.assertRaisesRegex(service.ServiceError, "credential is unavailable"):
                    service._service_token()
        with patch.object(Path, "open", side_effect=OSError("private credential path")):
            with self.assertRaises(service.ServiceError) as caught:
                service._service_token()
            self.assertNotIn("private", str(caught.exception))

    def test_entrypoint_binds_only_container_port_and_closes_mock_server(self):
        previous = service.Handler.service, service.Handler.expected_token
        self.addCleanup(setattr, service.Handler, "service", previous[0])
        self.addCleanup(setattr, service.Handler, "expected_token", previous[1])
        with patch.object(service, "_service_token", return_value=TOKEN), patch.object(
            service, "build_service", return_value=self.application,
        ), patch.object(service, "ThreadingHTTPServer") as server:
            service.serve()
        server.assert_called_once_with(("0.0.0.0", 8105), service.Handler)
        server.return_value.serve_forever.assert_called_once_with()
        server.return_value.server_close.assert_called_once_with()
        self.assertIs(service.Handler.service, self.application)
        self.assertEqual(service.Handler.expected_token, TOKEN)

    def test_real_pipeline_runs_through_tool_http_and_only_production_knowledge_with_mocked_models(self):
        self.analyze.side_effect = REAL_ANALYZE
        planning = {"presentation": {key: True for key in ("quotes", "sources", "locations", "units")},
                    "task_type": "field_extraction", "fields": [{"name": "sample_mass", "value_type": "numeric"}]}
        extraction = {"task_type": "field_extraction", "fields": [{"name": "sample_mass", "candidates": [
            {"value": "16.00", "chunk_ref": "c1"},
        ]}]}
        self.model.generate_structured.side_effect = [json.dumps(planning), json.dumps(extraction)]
        knowledge_responses = [catalog(), evidence()]
        self.knowledge_open.side_effect = lambda request, timeout: Response(knowledge_responses.pop(0), request.full_url)
        def local_open(request, timeout):
            self.assertEqual(request.full_url, "http://document-planner-prod:8105/analyze-document")
            handler = LocalHandler(self.application, path=urlsplit(request.full_url).path, body=request.data,
                                   authorization=request.get_header("Authorization"))
            handler.do_POST()
            return Response(handler.wfile.getvalue(), request.full_url, handler.status)
        transport = Mock(side_effect=local_open)
        with patch.object(tool, "open", mock_open(read_data=TOKEN), create=True), patch.object(
            tool.urllib.request, "build_opener", return_value=types.SimpleNamespace(open=transport),
        ):
            result = json.loads(tool.Tools().analyze_document(REQUEST))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["pipeline_version"], "0.6i")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["fields"][0]["value"], "16.00")
        self.assertEqual(result["fields"][0]["quote"], chunk()["text"])
        self.assertEqual(result["fields"][0]["location"], "Page 2")
        self.assertNotIn("extraction_request", result)
        self.assertNotIn("presentation", result)
        self.assertEqual([call.args[0].full_url for call in self.knowledge_open.call_args_list], [
            "http://knowledge-service:8002/documents?limit=50&offset=0", "http://knowledge-service:8002/search",
        ])
        self.assertEqual([call.kwargs["max_output_tokens"] for call in self.model.generate_structured.call_args_list], [768, 1536])
        planning_input = json.loads(self.model.generate_structured.call_args_list[0].args[0][1]["content"])
        self.assertEqual(planning_input["user_request"], REQUEST)
        self.assertEqual(transport.call_count, 1)
        self.assert_slots_available()


class ProductionToolTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.credential = self.start_patch(patch.object(tool, "open", mock_open(read_data=TOKEN + "\n"), create=True))
        self.transport = Mock(side_effect=AssertionError("unexpected tool HTTP"))
        self.builder = self.start_patch(patch.object(tool.urllib.request, "build_opener",
                                                     return_value=types.SimpleNamespace(open=self.transport)))

    def call_tool(self, payload, *, url=tool.DOCUMENT_ENDPOINT, status=200, request=REQUEST):
        self.response = Response(payload, url, status)
        self.transport.side_effect = None
        self.transport.return_value = self.response
        return json.loads(tool.Tools().analyze_document(request))

    def test_fixed_endpoint_credential_and_complete_request_are_used_without_legacy_tools(self):
        request = REQUEST + " Also summarize and calculate a total."
        expected = pipeline_result("unsupported_task_type")
        self.assertEqual(self.call_tool(expected, request=request), public_result(expected))
        sent = self.transport.call_args.args[0]
        self.assertEqual(sent.full_url, "http://document-planner-prod:8105/analyze-document")
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(json.loads(sent.data), {"request": request})
        self.assertEqual(sent.get_header("Authorization"), "Bearer " + TOKEN)
        self.credential.assert_called_once_with("/run/secrets/document_planner_prod_token", "r", encoding="utf-8")
        self.credential.return_value.read.assert_called_once_with(1025)
        self.assertEqual(self.transport.call_args.kwargs["timeout"], 75)
        self.assertEqual(self.response.read_limit, 96 * 1024 + 1)
        self.builder.assert_called_once_with(tool._NoRedirect)
        public_methods = [name for name, member in inspect.getmembers(tool.Tools, inspect.isfunction) if not name.startswith("_")]
        self.assertEqual(public_methods, ["analyze_document"])
        self.assertEqual(list(inspect.signature(tool.Tools.analyze_document).parameters), ["self", "user_request"])

    def test_all_public_results_and_evidence_statuses_are_preserved(self):
        for status in ("success", "missing", "ambiguous", "unsupported_task_type", "document_ambiguous", "invalid_evidence"):
            with self.subTest(status=status):
                expected = pipeline_result(status)
                actual = self.call_tool(expected)
                self.assertEqual(actual, public_result(expected))
                self.assertEqual(actual["pipeline_version"], "0.6i")
                self.assertFalse(actual["execution_performed"])
                self.assertNotIn("extraction_request", actual)

    def test_projection_excludes_planning_debug_presentation_and_unverified_answers(self):
        for status in ("missing", "ambiguous"):
            plain = pipeline_result(status)
            plain.update(choices=[document()], choices_truncated=False)
            poisoned = copy.deepcopy(plain)
            poisoned["extraction_request"] = {"answer": "private planning assertion"}
            poisoned["planning"] = {"raw": "private planning assertion"}
            poisoned["debug"] = {"raw": "private planning assertion"}
            poisoned["presentation"] = {"quotes": True}
            poisoned["selected_document"]["debug"] = "private planning assertion"
            poisoned["choices"][0]["debug"] = "private planning assertion"
            poisoned["errors"][0]["debug"] = "private planning assertion"
            for field in poisoned["fields"]:
                field["question"] = "private planning assertion"
                if field["status"] != "found":
                    field["value"] = "unverified answer"
                for candidate in field.get("candidates", []):
                    candidate["debug"] = "private planning assertion"
            actual = self.call_tool(poisoned)
            self.assertEqual(actual, public_result(plain))
            self.assertNotIn("private planning assertion", json.dumps(actual))
            self.assertNotIn("unverified answer", json.dumps(actual))
        legitimate = pipeline_result()
        legitimate["fields"][0].update(name="record_identifier", value="7302", quote="Record identifier: 7302")
        legitimate["debug"] = "7302"
        self.assertEqual(self.call_tool(legitimate)["fields"][0], legitimate["fields"][0])

    def test_invalid_requests_never_read_credentials_or_contact_any_endpoint(self):
        for request in (None, True, 1, [], {}, "", " ", "x" * 4001, "€" * 4000, "\ud800"):
            actual = json.loads(tool.Tools().analyze_document(request))
            self.assertEqual(actual, {"valid": False, "error": "request is invalid or too large"})
        with self.assertRaises(TypeError):
            tool.Tools().analyze_document(REQUEST, endpoint=knowledge.ISOLATED_ENDPOINT)
        self.credential.assert_not_called()
        self.builder.assert_not_called()
        self.transport.assert_not_called()

    def test_utf8_request_boundary_is_enforced_without_stripping_or_truncation(self):
        overhead = len(json.dumps({"request": ""}, ensure_ascii=False).encode())
        request = "€" * 2200
        request += "x" * (tool.MAX_REQUEST_BYTES - overhead - len(request.encode()))
        self.assertTrue(self.call_tool(pipeline_result(), request=request)["success"])
        sent = self.transport.call_args.args[0]
        self.assertEqual(len(sent.data), 8 * 1024)
        self.assertEqual(json.loads(sent.data)["request"], request)
        self.transport.reset_mock()
        self.credential.reset_mock()
        self.assertEqual(json.loads(tool.Tools().analyze_document(request + "x")),
                         {"valid": False, "error": "request is invalid or too large"})
        self.transport.assert_not_called()
        self.credential.assert_not_called()

    def test_response_limit_accepts_exact_boundary_and_rejects_overflow_or_non_bytes(self):
        base = b'{"success":true,"execution_performed":false}'
        for extra in (0, 1):
            actual = self.call_tool(base + b" " * (tool.MAX_RESPONSE_BYTES - len(base) + extra))
            self.assertEqual(actual, {"valid": False, "error": "document API unavailable"} if extra else
                             {"success": True, "execution_performed": False})
            self.assertEqual(self.response.read_limit, tool.MAX_RESPONSE_BYTES + 1)
        self.response.raw = "not response bytes"
        self.assertEqual(json.loads(tool.Tools().analyze_document(REQUEST)), {"valid": False, "error": "document API unavailable"})

    def test_strict_response_json_and_metadata_are_checked_before_projection(self):
        for raw in (b"\xff", b"not-json", b"[]", b"null", b'"text"', b'{"success":true,"success":false}',
                    b'{"debug":NaN}', b'{"debug":"\\ud800"}', b'{"fields":{}}',
                    b'{"fields":[null]}', b'{"fields":[{"status":"ambiguous","candidates":{}}]}'):
            self.assertEqual(self.call_tool(raw), {"valid": False, "error": "document API unavailable"})

    def test_redirects_do_not_forward_production_credentials_to_any_target(self):
        for code in (301, 302, 303, 307, 308):
            requests, forwarded = [], []
            def build(handler_class):
                def redirect(request, timeout):
                    requests.append(request)
                    target = handler_class().redirect_request(request, None, code, "private detail", {},
                                                              "http://planner-service:8105/analyze-document")
                    forwarded.append(target)
                    raise AssertionError("redirect was allowed")
                return types.SimpleNamespace(open=redirect)
            with patch.object(tool.urllib.request, "build_opener", side_effect=build):
                actual = json.loads(tool.Tools().analyze_document(REQUEST))
            self.assertEqual(actual, {"valid": False, "error": "document API unavailable"})
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].full_url, tool.DOCUMENT_ENDPOINT)
            self.assertEqual(forwarded, [])

    def test_changed_response_urls_and_error_statuses_fail_without_fallback(self):
        for options in ({"url": "http://planner-service:8105/analyze-document"},
                        {"url": "http://knowledge-service-test:8002/search"}, {"status": 302}, {"status": 401}, {"status": 500}):
            self.transport.reset_mock()
            actual = self.call_tool({"error": "private detail"}, **options)
            self.assertEqual(actual, {"valid": False, "error": "document API unavailable"})
            self.transport.assert_called_once()
            self.assertEqual(self.transport.call_args.args[0].full_url, tool.DOCUMENT_ENDPOINT)

    def test_credentials_and_transport_errors_are_bounded_and_sanitized(self):
        for value in ("", " ", "x" * 513, " " * 1024 + "x", "bad\nvalue"):
            self.credential.return_value.read.return_value = value
            self.assertEqual(json.loads(tool.Tools().analyze_document(REQUEST)),
                             {"valid": False, "error": "document API unavailable"})
        self.transport.assert_not_called()
        self.credential.return_value.read.return_value = TOKEN
        for error in (urllib.error.URLError("private detail"), TimeoutError("private detail"),
                      OSError("private detail"), RuntimeError("private detail"),
                      urllib.error.HTTPError(tool.DOCUMENT_ENDPOINT, 403, "private detail", {}, io.BytesIO(b"private token"))):
            self.transport.side_effect = error
            actual = json.loads(tool.Tools().analyze_document(REQUEST))
            self.assertEqual(actual, {"valid": False, "error": "document API unavailable"})
            self.assertNotIn("private", json.dumps(actual))
            if isinstance(error, urllib.error.HTTPError):
                self.assertTrue(error.closed)
        self.credential.side_effect = OSError("private credential path")
        self.transport.reset_mock()
        self.assertEqual(json.loads(tool.Tools().analyze_document(REQUEST)), {"valid": False, "error": "document API unavailable"})
        self.transport.assert_not_called()

    def test_tool_executes_as_pasted_source_without_project_imports(self):
        with io.open(tool.__file__, encoding="utf-8") as source:
            code = source.read()
        original_import = builtins.__import__
        def import_without_project(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"scripts", "tools"}:
                raise AssertionError("standalone tool attempted a project-local import")
            return original_import(name, *args, **kwargs)
        credential = mock_open(read_data=TOKEN)
        namespace = {"__name__": "pasted_production_document_tool", "__builtins__": {
            **vars(builtins), "__import__": import_without_project, "open": credential,
        }}
        exec(compile(code, "<pasted production tool>", "exec"), namespace)
        self.transport.side_effect = None
        self.transport.return_value = Response(pipeline_result(), tool.DOCUMENT_ENDPOINT)
        result = json.loads(namespace["Tools"]().analyze_document(REQUEST))
        self.assertEqual(result, public_result(pipeline_result()))
        credential.assert_called_once_with("/run/secrets/document_planner_prod_token", "r", encoding="utf-8")
        self.assertEqual(self.transport.call_args.args[0].full_url, tool.DOCUMENT_ENDPOINT)


if __name__ == "__main__":
    unittest.main()
