"""Offline HTTP/tool boundary checks; no sockets, credentials, LM or DB calls."""
import copy
import io
import json
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, mock_open, patch
from urllib.parse import urlsplit

from scripts import planner_service_v05b as service
from tools import analyze_with_planner as tool


TOKEN = "synthetic-service-token"
REQUEST = '  Extract capacity and session_label from "input/events/program.txt".\nDo not omit any part.  '


def document_result(status="success"):
    selected = {"document_id": "synthetic-document-id", "folder": "input", "source": "events/program.txt"}
    numeric = {
        "name": "capacity", "status": "found", "value_type": "numeric", "value": "120",
        "source": selected["source"], "chunk_id": "capacity-1", "location": "Section 2",
        "quote": "Capacity: 120 people.",
    }
    text = {
        "name": "session_label", "status": "found", "value_type": "text", "value": "18 hours",
        "source": selected["source"], "chunk_id": "label-1", "location": "Section 3",
        "quote": "Session label: 18 hours — début.",
    }
    result = {
        "version": "0.6a", "pipeline_version": "0.6f", "status": status,
        "valid": status != "unsupported_task_type", "success": status == "success",
        "source": selected["source"], "selected_document": selected,
        "extraction_request": {"task_type": "field_extraction", "fields": [
            {"name": "capacity", "question": "What is the capacity?", "value_type": "numeric"},
            {"name": "session_label", "question": "What is the session label?", "value_type": "text"},
        ]},
        "fields": [numeric, text], "errors": [], "model_calls": 2,
        "execution_performed": False,
        "evidence_validation": "structural_only; semantic_correctness_not_verified",
        "future_metadata": {"preserve_this": True},
    }
    if status == "missing":
        result["fields"] = [{"name": "capacity", "value_type": "numeric", "status": "missing"}, text]
        result["errors"] = [{"code": "missing_field", "message": "No verified candidate was returned.", "field": "capacity"}]
    elif status == "ambiguous":
        candidate = {key: value for key, value in numeric.items() if key not in {"name", "status"}}
        other = {**candidate, "value": "150", "quote": "Capacity: 150 people.", "chunk_id": "capacity-2"}
        result["fields"] = [{"name": "capacity", "value_type": "numeric", "status": "ambiguous",
                             "candidates": [candidate, other]}, text]
        result["errors"] = [{"code": "ambiguous_field", "message": "No candidate was selected.", "field": "capacity"}]
    elif status == "unsupported_task_type":
        result.update({"fields": [], "extraction_request": None, "model_calls": 1,
                       "errors": [{"code": status, "message": "Only field extraction is supported."}]})
    return result


def model_result(result):
    """Expected public fixture: HTTP retains these two internal-only payloads."""
    return {key: copy.deepcopy(value) for key, value in result.items()
            if key not in {"extraction_request", "future_metadata"}}


def legacy_result():
    return {
        "valid": False, "version": "0.5b", "attempts": [],
        "errors": [{"code": "invalid_intent", "message": "Unsupported legacy request."}],
        "report": None, "evidence": [], "calculations": [], "execution_performed": False,
    }


class LocalHandler(service.Handler):
    """Exercise real routing and JSON serialization without an HTTP listener."""
    def __init__(self, planner_service, path="/analyze-document", body=None, authorization="Bearer " + TOKEN):
        if body is None:
            body = json.dumps({"request": REQUEST}, ensure_ascii=False).encode()
        self.path = path
        self.service = planner_service
        self.expected_token = TOKEN
        self.headers = {"Authorization": authorization, "Content-Length": str(len(body))}
        self.rfile = Mock(wraps=io.BytesIO(body))
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass

    def result(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class Response:
    def __init__(self, payload, *, url=None, status=200):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
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
        # Simulate a faulty transport too: callers must check returned length.
        return self.raw


class DocumentAPIV06dTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Mock(name="isolated_adapter")
        self.model = Mock(name="structured_model")
        self.planner_service = service.PlannerService(
            self.model, self.adapter, Path("unused-isolated-root"), Path("unused-isolated-root/approvals")
        )
        self.generic = self.start_patch(patch.object(
            service.document_pipeline_v06c, "analyze_request", return_value=document_result()
        ))
        self.legacy = self.start_patch(patch.object(service.bridge, "prepare_from_request", return_value={
            "valid": False, "planning": {"attempts": [], "errors": legacy_result()["errors"]},
        }))
        self.credential = self.start_patch(patch.object(tool, "open", mock_open(read_data=TOKEN + "\n"), create=True))
        self.transport = Mock(side_effect=AssertionError("unmocked transport"))
        self.builder = self.start_patch(patch.object(
            tool.urllib.request, "build_opener", return_value=types.SimpleNamespace(open=self.transport)
        ))
        self.start_patch(patch.object(service, "_read_secret", side_effect=AssertionError("no credential reads")))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def respond(self, payload, *, url=None, status=200):
        self.response = Response(payload, url=url, status=status)
        self.transport.side_effect = None
        self.transport.return_value = self.response

    def tool_call(self, method, payload, **response_args):
        endpoint = tool.PLANNER_ENDPOINT if method == "analyze_with_planner" else tool.DOCUMENT_ENDPOINT
        self.respond(payload, url=response_args.pop("url", endpoint), **response_args)
        return json.loads(getattr(tool.Tools(), method)(REQUEST))

    def test_generic_method_returns_complete_pipeline_result_with_injected_dependencies(self):
        expected = document_result()
        self.generic.return_value = expected
        with patch.object(service, "_public_result", side_effect=AssertionError("no legacy filtering")) as public:
            actual = self.planner_service.analyze_document(REQUEST)
        self.assertIs(actual, expected)
        self.generic.assert_called_once_with(REQUEST, adapter=self.adapter, client=self.model)
        self.legacy.assert_not_called()
        public.assert_not_called()

    def test_generic_http_outcomes_are_200_and_unmodified(self):
        for status in ("success", "missing", "ambiguous", "unsupported_task_type"):
            with self.subTest(status=status):
                expected = document_result(status)
                self.generic.return_value = copy.deepcopy(expected)
                handler = LocalHandler(self.planner_service)
                handler.do_POST()
                self.assertEqual(handler.status, 200)
                self.assertEqual(handler.result(), expected)
                self.assertEqual(int(handler.response_headers["Content-Length"]), len(handler.wfile.getvalue()))
        self.legacy.assert_not_called()

    def test_legacy_route_preserves_response_contract_and_bridge(self):
        handler = LocalHandler(self.planner_service, path="/analyze")
        handler.do_POST()
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.result(), legacy_result())
        self.legacy.assert_called_once_with(
            REQUEST, model_client=self.model, adapter=self.adapter,
            isolated_root=self.planner_service.isolated_root, approval_dir=self.planner_service.approval_dir,
        )
        self.generic.assert_not_called()

    def test_authentication_fails_before_body_reads_or_analysis(self):
        for route in ("/analyze", "/analyze-document"):
            for authorization in ("", "Bearer wrong", "Basic anything", "Bearer non-ascii-é", None):
                with self.subTest(route=route, authorization=authorization):
                    handler = LocalHandler(self.planner_service, path=route, authorization=authorization)
                    handler.do_POST()
                    self.assertEqual(handler.status, 401)
                    self.assertEqual(handler.result(), {"error": "Unauthorized"})
                    handler.rfile.read.assert_not_called()
        self.generic.assert_not_called()
        self.legacy.assert_not_called()
        self.assertFalse(service.authorize_service_request("Bearer ", ""))

    def test_invalid_utf8_json_duplicates_body_types_and_extra_keys_are_400(self):
        bodies = [
            b'{"request":"\xff"}', b'{not-json', b'{"request":"bad\x00value"}',
            b'{"request":"first","request":"second"}',
            b'{"request":"first","reque\\u0073t":"second"}',
            b'{"request":NaN}', b'{"request":Infinity}', b'{"request":"\\ud800"}',
            b'{"request":"request","extra":{"x":1,"x":2}}',
        ]
        bodies += [json.dumps(item).encode() for item in (
            None, [], 1, "request", {}, {"request": None}, {"request": 1}, {"request": True},
            {"request": []}, {"request": {}}, {"request": ""}, {"request": "  "},
            {"request": "x" * (service.MAX_REQUEST_CHARS + 1)},
            {"request": "request", "endpoint": "untrusted"}, {"request": "request", "commit": True},
        )]
        for route in ("/analyze", "/analyze-document"):
            for body in bodies:
                with self.subTest(route=route, body=body[:50]):
                    analysis = Mock(spec=service.PlannerService)
                    handler = LocalHandler(analysis, path=route, body=body)
                    handler.do_POST()
                    self.assertEqual(handler.status, 400)
                    analysis.analyze.assert_not_called()
                    analysis.analyze_document.assert_not_called()

    def test_body_length_limits_are_preserved(self):
        for length in ("0", "-1", "not-integer", str(service.MAX_REQUEST_BYTES + 1)):
            handler = LocalHandler(self.planner_service)
            handler.headers["Content-Length"] = length
            handler.do_POST()
            self.assertEqual(handler.status, 400)
            handler.rfile.read.assert_not_called()
        handler = LocalHandler(self.planner_service)
        handler.headers["Content-Length"] = str(len(handler.rfile._mock_wraps.getvalue()) + 1)
        handler.do_POST()
        self.assertEqual(handler.status, 400)
        self.generic.assert_not_called()

    def test_unknown_and_execution_routes_have_no_analysis_access(self):
        for path in ("/commit", "/reconcile", "/prepare", "/run", "/execute", "/unknown", "/analyze-document/", "/analyze-document?commit=true"):
            for method in ("do_POST", "do_GET"):
                with self.subTest(path=path, method=method):
                    handler = LocalHandler(self.planner_service, path=path)
                    getattr(handler, method)()
                    self.assertEqual(handler.status, 404)
                    handler.rfile.read.assert_not_called()
        handler = LocalHandler(self.planner_service, path="/health", authorization="")
        handler.do_GET()
        self.assertEqual(handler.result(), {"status": "ok", "version": "0.5b"})
        self.generic.assert_not_called()
        self.legacy.assert_not_called()

    def test_unexpected_errors_are_sanitized_and_slots_released(self):
        for error in (RuntimeError("private token and host path"), service.ServiceError("private token and host path")):
            self.generic.side_effect = error
            handler = LocalHandler(self.planner_service)
            handler.do_POST()
            self.assertEqual(handler.status, 500)
            self.assertEqual(handler.result(), {"valid": False, "error": "planner service failure"})
            self.assertNotIn("private", handler.wfile.getvalue().decode())
            self.assert_slots_available()
        self.generic.side_effect = None
        self.legacy.side_effect = RuntimeError("private legacy failure")
        result = self.planner_service.analyze(REQUEST)
        self.assertFalse(result["valid"])
        self.assertNotIn("private", json.dumps(result))
        self.assert_slots_available()

    def assert_slots_available(self):
        acquired = 0
        try:
            for _ in range(service.MAX_CONCURRENT_ANALYSES):
                self.assertTrue(self.planner_service._slots.acquire(blocking=False))
                acquired += 1
            self.assertFalse(self.planner_service._slots.acquire(blocking=False))
        finally:
            for _ in range(acquired):
                self.planner_service._slots.release()

    def test_both_methods_share_the_same_concurrency_limit(self):
        entered_legacy, entered_generic, release = threading.Event(), threading.Event(), threading.Event()
        errors, results = [], []

        def legacy(*args, **kwargs):
            entered_legacy.set()
            if not release.wait(2):
                raise RuntimeError("test worker was not released")
            return {"valid": False, "planning": {"errors": legacy_result()["errors"]}}

        def generic(*args, **kwargs):
            entered_generic.set()
            if not release.wait(2):
                raise RuntimeError("test worker was not released")
            return document_result()

        def worker(method):
            try:
                results.append(getattr(self.planner_service, method)(REQUEST))
            except Exception as exc:
                errors.append(exc)

        self.legacy.side_effect = legacy
        self.generic.side_effect = generic
        threads = [threading.Thread(target=worker, args=(method,)) for method in ("analyze", "analyze_document")]
        real_acquire = self.planner_service._slots.acquire
        # Preserve real semaphore accounting while making saturation immediate,
        # avoiding sleeps or waiting for the normal 1-second admission timeout.
        with patch.object(self.planner_service._slots, "acquire", side_effect=lambda **kwargs: real_acquire(blocking=False)):
            try:
                for thread in threads:
                    thread.start()
                self.assertTrue(entered_legacy.wait(2))
                self.assertTrue(entered_generic.wait(2))
                for method in ("analyze", "analyze_document"):
                    with self.assertRaisesRegex(service.ServiceError, "busy"):
                        getattr(self.planner_service, method)(REQUEST)
                self.assertEqual(self.legacy.call_count, 1)
                self.assertEqual(self.generic.call_count, 1)
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assert_slots_available()

    def test_response_size_limit_accepts_boundary_and_rejects_overflow(self):
        overhead = len(json.dumps({"value": ""}, ensure_ascii=False, sort_keys=True).encode())
        for extra, status in ((0, 200), (1, 500)):
            self.generic.return_value = {"value": "x" * (service.MAX_RESPONSE_BYTES - overhead + extra)}
            handler = LocalHandler(self.planner_service)
            handler.do_POST()
            self.assertEqual(handler.status, status)
            self.assertLessEqual(len(handler.wfile.getvalue()), service.MAX_RESPONSE_BYTES)
            if extra:
                self.assertEqual(handler.result(), {"valid": False, "error": "response too large"})
            else:
                self.assertEqual(handler.result(), self.generic.return_value)

    def test_service_unavailable_is_safe_and_does_not_invoke_pipeline(self):
        handler = LocalHandler(None)
        handler.do_POST()
        self.assertEqual(handler.status, 400)
        self.assertEqual(handler.result(), {"valid": False, "error": "planner service unavailable"})
        self.generic.assert_not_called()

    def test_both_tool_methods_use_fixed_urls_and_forward_the_entire_request(self):
        for method, endpoint, expected in (
            ("analyze_with_planner", tool.PLANNER_ENDPOINT, legacy_result()),
            ("analyze_document", tool.DOCUMENT_ENDPOINT, document_result()),
        ):
            with self.subTest(method=method):
                actual = self.tool_call(method, expected)
                self.assertEqual(actual, expected if method == "analyze_with_planner" else model_result(expected))
                request = self.transport.call_args.args[0]
                self.assertEqual(request.full_url, endpoint)
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(json.loads(request.data), {"request": REQUEST})
                self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
                self.assertEqual(self.transport.call_args.kwargs["timeout"], 75)
                self.assertEqual(self.response.read_limit, tool.MAX_RESPONSE_BYTES + 1)
                self.credential.assert_called_with(tool.PLANNER_TOKEN_FILE, "r", encoding="utf-8")
        self.assertEqual(tool.PLANNER_ENDPOINT, "http://planner-service:8105/analyze")
        self.assertEqual(tool.DOCUMENT_ENDPOINT, "http://planner-service:8105/analyze-document")
        self.assertIn("job-offer calculation", tool.Tools.analyze_with_planner.__doc__)
        self.assertIn("text/numeric fields", tool.Tools.analyze_document.__doc__)

    def test_tool_preserves_generic_results_but_omits_planning_metadata(self):
        for status in ("success", "missing", "ambiguous", "unsupported_task_type"):
            with self.subTest(status=status):
                expected = document_result(status)
                self.assertEqual(self.tool_call("analyze_document", expected), model_result(expected))

    def test_http_retains_full_result_but_tool_never_exposes_poisoned_planning(self):
        payload = document_result("missing")
        payload["fields"] = [{"name": "capacity", "value_type": "numeric", "status": "missing"}]
        expected = model_result(payload)
        poisoned = copy.deepcopy(payload)
        poisoned["extraction_request"]["fields"][0]["question"] = "Wage is $15.25"
        poisoned["extraction_request"]["fields"][1]["question"] = "Schedule is 37.5 hours; fee is $40"
        poisoned["planning"] = {"description": "Wage $15.25, 37.5 hours, fee $40"}
        poisoned["debug"] = {"raw_model_output": poisoned["planning"]}
        poisoned["fields"][0].update({"value": "15.25", "question": "Wage $15.25", "debug": "37.5 hours"})
        self.generic.return_value = copy.deepcopy(poisoned)
        handler = LocalHandler(self.planner_service)
        handler.do_POST()
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.result(), poisoned)  # The service contract is deliberately unchanged.
        actual = self.tool_call("analyze_document", handler.result())
        self.assertEqual(actual, expected)
        for text in ("$15.25", "37.5 hours", "$40", "extraction_request", "planning", "debug"):
            self.assertNotIn(text, json.dumps(actual))
        self.assertNotIn("value", actual["fields"][0])

    def test_model_projection_preserves_real_verified_values_even_when_they_match_poisoned_numbers(self):
        payload = document_result()
        payload["fields"] = []
        for name, value, quote in (
            ("hourly_pay", "15.25", "Rate:\t$15.25\nper hour."),
            ("scheduled_hours", "37.5", "Schedule:\n37.5\thours."),
            ("daily_fee", "40", "Fee:\t$40\nper day."),
        ):
            payload["fields"].append({
                "name": name, "status": "found", "value_type": "numeric", "value": value,
                "source": payload["source"], "chunk_id": name, "location": "Section 2", "quote": quote,
            })
        payload["extraction_request"] = {"question": "Invented: $15.25, 37.5 hours, $40"}
        actual = self.tool_call("analyze_document", payload)
        self.assertEqual(actual["fields"], payload["fields"])
        self.assertNotIn("extraction_request", actual)
        self.assertNotIn("Invented", json.dumps(actual))
        self.assertEqual(actual["pipeline_version"], "0.6f")
        self.assertEqual(actual["evidence_validation"], payload["evidence_validation"])

    def test_model_projection_preserves_partial_and_ambiguous_statuses_and_bounded_choices(self):
        for status in ("missing", "ambiguous"):
            with self.subTest(status=status):
                payload = document_result(status)
                payload["choices"] = [copy.deepcopy(payload["selected_document"])]
                payload["choices_truncated"] = False
                expected = model_result(payload)
                poisoned = copy.deepcopy(payload)
                poisoned["selected_document"]["debug"] = "internal planning"
                poisoned["choices"][0]["question"] = "internal planning"
                poisoned["errors"][0]["debug"] = "internal planning"
                for field in poisoned["fields"]:
                    field["question"] = "internal planning"
                    if field["status"] == "ambiguous":
                        field["value"] = "120"  # A candidate is not a settled answer.
                        for candidate in field["candidates"]:
                            candidate["debug"] = "internal planning"
                actual = self.tool_call("analyze_document", poisoned)
                self.assertEqual(actual, expected)
                self.assertFalse(actual["success"])
                self.assertEqual(actual["fields"][1]["status"], "found")
                self.assertNotIn("internal planning", json.dumps(actual))
                if status == "ambiguous":
                    self.assertNotIn("value", actual["fields"][0])
                    self.assertEqual(len(actual["fields"][0]["candidates"]), 2)
        docstring = tool.Tools.analyze_document.__doc__
        self.assertIn('status "found"', docstring)
        self.assertIn("does not establish absence", docstring)
        self.assertIn("not settled facts", docstring)

    def test_legacy_tool_response_is_not_subject_to_document_projection(self):
        expected = {**legacy_result(), "legacy_metadata": {"kept": True}, "extraction_request": {"legacy_fixture": "unchanged"}}
        self.assertEqual(self.tool_call("analyze_with_planner", expected), expected)

    def test_generic_results_survive_combined_local_http_and_tool_boundary(self):
        def local_open(request, timeout):
            handler = LocalHandler(
                self.planner_service, path=urlsplit(request.full_url).path,
                body=request.data, authorization=request.get_header("Authorization"),
            )
            handler.do_POST()
            return Response(handler.wfile.getvalue(), url=request.full_url, status=handler.status)

        self.transport.side_effect = local_open
        for status in ("success", "missing", "ambiguous", "unsupported_task_type"):
            expected = document_result(status)
            self.generic.return_value = copy.deepcopy(expected)
            self.assertEqual(json.loads(tool.Tools().analyze_document(REQUEST)), model_result(expected))
        self.assertEqual(self.generic.call_count, 4)
        self.legacy.assert_not_called()

    def test_tool_rejects_invalid_requests_before_secret_reads_or_transport(self):
        for method in ("analyze_with_planner", "analyze_document"):
            for request in (None, True, 4, {}, [], "", " ", "x" * 4001, "€" * 4000, "\ud800"):
                with self.subTest(method=method, request_type=type(request).__name__):
                    result = json.loads(getattr(tool.Tools(), method)(request))
                    self.assertEqual(result, {"valid": False, "error": "request is invalid or too large"})
        self.credential.assert_not_called()
        self.builder.assert_not_called()
        self.transport.assert_not_called()

    def test_tool_request_utf8_byte_boundary_is_enforced_without_truncation(self):
        overhead = len(json.dumps({"request": ""}, ensure_ascii=False).encode())
        request = "€" * 2200
        request += "x" * (tool.MAX_REQUEST_BYTES - overhead - len(request.encode()))
        self.assertLessEqual(len(request), tool.MAX_REQUEST_CHARS)
        self.respond(document_result(), url=tool.DOCUMENT_ENDPOINT)
        result = json.loads(tool.Tools().analyze_document(request))
        self.assertTrue(result["success"])
        sent = self.transport.call_args.args[0]
        self.assertEqual(len(sent.data), tool.MAX_REQUEST_BYTES)
        self.assertEqual(json.loads(sent.data)["request"], request)
        self.credential.reset_mock()
        self.transport.reset_mock()
        result = json.loads(tool.Tools().analyze_document(request + "x"))
        self.assertEqual(result, {"valid": False, "error": "request is invalid or too large"})
        self.credential.assert_not_called()
        self.transport.assert_not_called()

    def test_tool_rejects_response_overflow_and_accepts_exact_boundary(self):
        base = b'{"success":true,"execution_performed":false}'
        for method in ("analyze_with_planner", "analyze_document"):
            for extra in (0, 1):
                raw = base + b" " * (tool.MAX_RESPONSE_BYTES - len(base) + extra)
                result = self.tool_call(method, raw)
                if extra:
                    self.assertEqual(result, {"valid": False, "error": "planner API unavailable"})
                else:
                    self.assertEqual(result, {"success": True, "execution_performed": False})
                self.assertEqual(self.response.read_limit, 96 * 1024 + 1)

    def test_tool_rejects_invalid_utf8_json_and_non_object_responses(self):
        for raw in (
            b"\xff", b"not-json", b"[]", b"null", b'"text"',
            b'{"success":true,"success":false}', b'{"value":NaN}', b'{"value":"\\ud800"}',
        ):
            with self.subTest(raw=raw):
                result = self.tool_call("analyze_document", raw)
                self.assertEqual(result, {"valid": False, "error": "planner API unavailable"})

    def test_tool_blocks_redirects_before_forwarding_credentials(self):
        for code in (301, 302, 303, 307, 308):
            forwarded = []
            requests = []

            def build(handler_class):
                def open_redirect(request, timeout):
                    requests.append(request)
                    try:
                        target = handler_class().redirect_request(
                            request, None, code, "private redirect response", {}, "http://forbidden.invalid/steal"
                        )
                    except urllib.error.HTTPError as exc:
                        self.addCleanup(exc.close)
                        raise
                    forwarded.append(target)
                    raise AssertionError("redirect was allowed")
                return types.SimpleNamespace(open=open_redirect)

            with self.subTest(code=code), patch.object(tool.urllib.request, "build_opener", side_effect=build):
                result = json.loads(tool.Tools().analyze_document(REQUEST))
                self.assertEqual(result, {"valid": False, "error": "planner API unavailable"})
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].full_url, tool.DOCUMENT_ENDPOINT)
                self.assertEqual(forwarded, [])

    def test_tool_rejects_changed_response_url_or_http_error_status(self):
        for kwargs in (
            {"url": "http://forbidden.invalid/analyze-document"},
            {"status": 302}, {"status": 401}, {"status": 500},
        ):
            with self.subTest(kwargs=kwargs):
                result = self.tool_call("analyze_document", {"error": "private detail"}, **kwargs)
                self.assertEqual(result, {"valid": False, "error": "planner API unavailable"})

    def test_tool_transport_and_credential_errors_are_compact_and_sanitized(self):
        errors = [urllib.error.URLError("private detail"), TimeoutError("private detail"),
                  OSError("private detail"), RuntimeError("private detail")]
        for code in (401, 403, 500):
            error = urllib.error.HTTPError(tool.DOCUMENT_ENDPOINT, code, "private detail", {}, None)
            self.addCleanup(error.close)
            errors.append(error)
        for method in ("analyze_with_planner", "analyze_document"):
            for error in errors:
                self.transport.side_effect = error
                result = getattr(tool.Tools(), method)(REQUEST)
                self.assertEqual(json.loads(result), {"valid": False, "error": "planner API unavailable"})
                self.assertNotIn("private", result)
                self.assertNotIn(TOKEN, result)
                self.assertLess(len(result), 128)
        self.transport.reset_mock()
        for value in ("", "x" * 513, "x" * 1025):
            with patch.object(tool, "open", mock_open(read_data=value), create=True):
                self.assertEqual(json.loads(tool.Tools().analyze_document(REQUEST)),
                                 {"valid": False, "error": "planner API unavailable"})
        for error in (OSError("private credential path"), UnicodeError("private credential detail")):
            with patch.object(tool, "open", side_effect=error, create=True):
                self.assertEqual(json.loads(tool.Tools().analyze_document(REQUEST)),
                                 {"valid": False, "error": "planner API unavailable"})
        self.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
