"""Offline pipeline tests: all retrieval, models, credentials, and CLI I/O mocked."""
import copy
import hashlib
import io
import json
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import document_pipeline_v06c as pipeline
from scripts import document_analysis_v06a as engine
from scripts import task_planner_v05a2 as lm_module


def document(source="registrations/arrival-card.txt", folder="input"):
    return {
        "document_id": hashlib.sha256(f"{folder}:{source}".encode()).hexdigest(),
        "folder": folder, "source": source,
    }


def field_request(name="visitor", value_type="text"):
    return {"task_type": "field_extraction", "fields": [
        {"name": name, "value_type": value_type},
    ]}


def evidence_response(selected, *, name="visitor", value="Mira", quote="Visitor: Mira."):
    return {"task_type": "field_extraction", "fields": [{
        "name": name, "status": "found", "candidates": [{
            "value": value, "source": selected["source"], "chunk_id": "evidence-1",
            "location": "Section 3", "quote": quote,
        }],
    }]}


def chunk(selected, text="Visitor: Mira."):
    return {
        "folder": selected["folder"], "source": selected["source"],
        "chunk_id": "evidence-1", "location": "Section 3", "text": text,
    }


def ranked_chunk(chunk_id, text=None):
    return {**chunk(document(), text or f"Evidence for {chunk_id}."), "chunk_id": chunk_id}


def requested_fields(count):
    return {"task_type": "field_extraction", "fields": [
        {"name": f"metric_{index}", "value_type": "text"} for index in range(count)
    ]}


class CatalogAdapter:
    def __init__(self, documents=None, chunks=None):
        self.documents = documents if documents is not None else [document()]
        self.chunks = chunks if chunks is not None else [chunk(self.documents[0])] if self.documents else []
        self.list_calls = []
        self.search_calls = []
        self.page_override = None

    def list_documents(self, *, limit, offset):
        self.list_calls.append({"limit": limit, "offset": offset})
        items = copy.deepcopy(self.documents[offset:offset + limit])
        end = offset + len(items)
        total = len(self.documents)
        page = {
            "status": "success", "documents": items, "result_count": len(items),
            "pagination": {
                "limit": limit, "offset": offset, "total": total,
                "has_more": end < total, "next_offset": end if end < total else None,
            },
        }
        return self.page_override(page, len(self.list_calls)) if self.page_override else page

    def search_document(self, query, *, folder, source, limit):
        self.search_calls.append({"query": query, "folder": folder, "source": source, "limit": limit})
        return {"status": "success", "result_count": len(self.chunks), "results": copy.deepcopy(self.chunks)}


class StructuredClient:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def generate_structured(self, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), **copy.deepcopy(kwargs)})
        if not self.outputs:
            raise AssertionError("unexpected model call")
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, str) else json.dumps(result)


class RankedAdapter(CatalogAdapter):
    def __init__(self, rankings):
        super().__init__()
        self.rankings = rankings

    def search_document(self, query, *, folder, source, limit):
        self.search_calls.append({"query": query, "folder": folder, "source": source, "limit": limit})
        records = copy.deepcopy(self.rankings[len(self.search_calls) - 1])
        return {"status": "success", "result_count": len(records), "results": records}


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.payload[:limit]


class DocumentPipelineV06cTests(unittest.TestCase):
    def setUp(self):
        for target in (
            patch.object(Path, "read_text", side_effect=AssertionError("no real credential/file reads")),
            patch.object(Path, "write_text", side_effect=AssertionError("no writes")),
            patch.object(lm_module.urllib.request.OpenerDirector, "open", side_effect=AssertionError("no network")),
        ):
            target.start()
            self.addCleanup(target.stop)

    def run_request(self, request="Extract visitor from arrival-card.txt.", *, adapter=None, client=None):
        adapter = adapter or CatalogAdapter()
        client = client or StructuredClient(field_request(), evidence_response(adapter.documents[0]))
        return pipeline.analyze_request(request, adapter=adapter, client=client), adapter, client

    def test_two_unrelated_documents_generic_text_numeric_and_exact_scope(self):
        first = document()
        second = document("lab notes/readout-472.csv", "knowledge")
        for selected, name, kind, value, quote, request in (
            (first, "visitor", "text", "Mira", "Visitor: Mira.", 'Extract visitor from "input/registrations/arrival-card.txt".'),
            (second, "sample_mass", "numeric", "-2.50e1", "Sample mass: -2.50e1 grams.", 'Extract sample_mass from “knowledge/lab notes/readout-472.csv”.'),
        ):
            with self.subTest(selected=selected):
                adapter = CatalogAdapter([first, second], [chunk(selected, quote)])
                client = StructuredClient(field_request(name, kind), evidence_response(selected, name=name, value=value, quote=quote))
                result, _, _ = self.run_request(request, adapter=adapter, client=client)
                self.assertTrue(result["success"], result)
                self.assertEqual(result["selected_document"], selected)
                self.assertEqual(result["fields"][0]["value"], "-25.0" if kind == "numeric" else "Mira")
                self.assertFalse(result["execution_performed"])
                self.assertEqual(result["evidence_validation"], pipeline.EVIDENCE_DISCLAIMER)
                self.assertEqual(adapter.search_calls, [{
                    "query": name.replace("_", " "), "folder": selected["folder"],
                    "source": selected["source"], "limit": 4,
                }])
                self.assertEqual(result["model_calls"], 2)

    def test_passes_resolved_identity_to_existing_engine(self):
        selected = document()
        with patch.object(engine, "analyze_document", wraps=engine.analyze_document) as analyze:
            result, _, _ = self.run_request()
        self.assertTrue(result["success"])
        self.assertEqual(analyze.call_args.args[0], engine.DocumentIdentity(selected["source"], selected["document_id"]))

    def test_real_adapter_and_generic_client_work_together_with_mocked_http(self):
        fixture = CatalogAdapter()
        responses = [fixture.list_documents(limit=50, offset=0), {
            "status": "success", "result_count": 1, "results": fixture.chunks,
        }]
        knowledge_open = Mock(side_effect=[Response(item) for item in responses])
        adapter = pipeline.KnowledgeServiceAdapter(Path("never-read-unit-credential"), opener=knowledge_open)
        model_open = Mock(side_effect=[Response({"choices": [{"message": {"content": json.dumps(item)}}]})
                                           for item in (field_request(), evidence_response(document()))])
        client = pipeline.IntentLMStudioClient(opener=model_open)
        with patch.object(adapter, "_token", return_value="synthetic-unit-token"):
            result = pipeline.analyze_request("Extract visitor from arrival-card.txt.", adapter=adapter, client=client)
        self.assertTrue(result["success"], result)
        self.assertEqual(knowledge_open.call_count, 2)
        self.assertEqual(model_open.call_count, 2)
        requests = [call.args[0] for call in knowledge_open.call_args_list]
        self.assertEqual(requests[0].full_url, "http://knowledge-service-test:8002/documents?limit=50&offset=0")
        self.assertEqual(requests[1].full_url, "http://knowledge-service-test:8002/search")
        self.assertEqual(json.loads(requests[1].data)["document"], {"folder": "input", "source": document()["source"]})
        self.assertEqual([json.loads(call.args[0].data)["max_tokens"] for call in model_open.call_args_list], [768, 1536])

    def test_qualified_reference_beats_ambiguous_basename(self):
        first, second = document("a.txt", "input"), document("a.txt", "knowledge")
        adapter = CatalogAdapter([second, first], [chunk(first)])
        client = StructuredClient(field_request(), evidence_response(first))
        result, _, _ = self.run_request("Extract visitor from input/a.txt, not the other document.", adapter=adapter, client=client)
        self.assertTrue(result["success"])
        self.assertEqual(result["selected_document"], first)

    def test_source_path_and_quoted_space_name_resolution(self):
        for source, request in (
            ("records/a.txt", "Extract visitor from records/a.txt!"),
            ("records/arrival card.txt", "Extract visitor from 'records/arrival card.txt'."),
            ("records/a.txt", "Extract visitor from a.txt."),
        ):
            with self.subTest(request=request):
                selected = document(source)
                adapter = CatalogAdapter([selected], [chunk(selected)])
                client = StructuredClient(field_request(), evidence_response(selected))
                result, _, _ = self.run_request(request, adapter=adapter, client=client)
                self.assertTrue(result["success"], result)

    def test_basename_ambiguity_never_selects_first_and_returns_bounded_choices(self):
        documents = [document(f"group-{i}/same.txt") for i in range(12)]
        for ordered in (documents, list(reversed(documents))):
            adapter = CatalogAdapter(ordered)
            client = StructuredClient()
            result, _, _ = self.run_request("Extract visitor from same.txt.", adapter=adapter, client=client)
            self.assertEqual(result["status"], "document_ambiguous")
            self.assertIsNone(result["selected_document"])
            self.assertEqual(len(result["choices"]), pipeline.MAX_CHOICES)
            self.assertTrue(result["choices_truncated"])
            self.assertEqual(client.calls, [])
            self.assertEqual(adapter.search_calls, [])

    def test_root_source_does_not_hide_same_nested_basename(self):
        adapter = CatalogAdapter([document("same.txt"), document("nested/same.txt")])
        result, _, _ = self.run_request("Extract visitor from same.txt.", adapter=adapter, client=StructuredClient())
        self.assertEqual(result["status"], "document_ambiguous")

    def test_multiple_explicit_documents_are_ambiguous(self):
        adapter = CatalogAdapter([document("one.txt"), document("two.txt")])
        result, _, client = self.run_request("Extract visitor from input/one.txt and input/two.txt.", adapter=adapter, client=StructuredClient())
        self.assertEqual(result["status"], "document_ambiguous")
        self.assertEqual(client.calls, [])

    def test_punctuation_and_prefix_collisions(self):
        for reference in ('"arrival-card.txt".', "arrival-card.txt,", "(arrival-card.txt)", "arrival-card.txt...", "arrival-card.txt;", "`arrival-card.txt`"):
            with self.subTest(reference=reference):
                result, _, _ = self.run_request(f"Extract visitor from {reference}")
                self.assertTrue(result["success"], result)
        for reference in (
            "not-arrival-card.txt", "prefixarrival-card.txt", "arrival-card.txt.bak", "arrival-card.txtx",
            "prefix/arrival-card.txt", "https://other.invalid/arrival-card.txt", "input/other/arrival-card.txt",
            '"prefixed arrival-card.txt"', "'other arrival-card.txt'",
        ):
            with self.subTest(reference=reference):
                result, adapter, client = self.run_request(f"Extract visitor from {reference}.", client=StructuredClient())
                self.assertEqual(result["status"], "document_not_found")
                self.assertEqual(client.calls, [])
                self.assertEqual(adapter.search_calls, [])

    def test_missing_reference_or_empty_catalog_returns_choices_without_model_calls(self):
        for request, adapter in (
            ("Extract visitor from absent.txt.", CatalogAdapter([document(f"file-{i}.txt") for i in range(10)])),
            ("Extract visitor.", CatalogAdapter()),
            ("Extract visitor from absent.txt.", CatalogAdapter([])),
        ):
            result, _, client = self.run_request(request, adapter=adapter, client=StructuredClient())
            self.assertEqual(result["status"], "document_not_found")
            self.assertLessEqual(len(result["choices"]), 8)
            self.assertEqual(client.calls, [])

    def test_catalog_four_page_boundary_and_overflow(self):
        documents = [document(f"file-{i}.txt") for i in range(200)]
        selected = documents[-1]
        adapter = CatalogAdapter(documents, [chunk(selected)])
        client = StructuredClient(field_request(), evidence_response(selected))
        result, _, _ = self.run_request("Extract visitor from file-199.txt.", adapter=adapter, client=client)
        self.assertTrue(result["success"])
        self.assertEqual(adapter.list_calls, [{"limit": 50, "offset": i * 50} for i in range(4)])
        adapter = CatalogAdapter(documents + [document("file-200.txt")])
        result, _, client = self.run_request("Extract visitor from file-199.txt.", adapter=adapter, client=StructuredClient())
        self.assertEqual(result["status"], "catalog_incomplete")
        self.assertLessEqual(len(adapter.list_calls), 4)
        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.search_calls, [])

    def test_changing_catalog_total_and_duplicate_identity_are_rejected(self):
        for change in ("total", "duplicate"):
            with self.subTest(change=change):
                adapter = CatalogAdapter([document(f"file-{i}.txt") for i in range(51)])
                def override(page, number):
                    if number == 2:
                        if change == "total":
                            page["pagination"]["total"] += 1
                        else:
                            page["documents"] = [adapter.documents[0]]
                    return page
                adapter.page_override = override
                result, _, client = self.run_request("Extract visitor from file-0.txt.", adapter=adapter, client=StructuredClient())
                self.assertEqual(result["status"], "catalog_changed")
                self.assertEqual(client.calls, [])

    def test_catalog_gaps_and_bad_pagination_are_rejected(self):
        for mutate in (
            lambda page: page["pagination"].update({"next_offset": 0}),
            lambda page: page["pagination"].update({"has_more": True}),
            lambda page: page["pagination"].update({"offset": 50}),
            lambda page: page.update({"documents": [], "result_count": 0}),
            lambda page: page["documents"][0].update({"document_id": "invented"}),
            lambda page: page["documents"][0].update({"source": "../private.txt"}),
        ):
            adapter = CatalogAdapter()
            def override(page, number):
                mutate(page)
                return page
            adapter.page_override = override
            result, _, client = self.run_request(adapter=adapter, client=StructuredClient())
            self.assertIn(result["status"], {"catalog_invalid", "catalog_incomplete"})
            self.assertEqual(client.calls, [])

    def test_unsupported_requests_stop_before_retrieval_and_do_not_drop_parts(self):
        for request in (
            "Calculate the total from arrival-card.txt.", "Summarize arrival-card.txt.",
            "Extract visitor from arrival-card.txt and calculate a total.",
            "Extract visitor and summarize arrival-card.txt.",
        ):
            with self.subTest(request=request):
                client = StructuredClient({"task_type": "unsupported_task_type", "fields": []})
                result, adapter, _ = self.run_request(request, client=client)
                self.assertEqual(result["status"], "unsupported_task_type")
                self.assertFalse(result["success"])
                self.assertEqual(result["model_calls"], 1)
                self.assertEqual(adapter.search_calls, [])
                schema = client.calls[0]["response_schema"]
                self.assertIn("unsupported_task_type", schema["properties"]["task_type"]["enum"])
                self.assertEqual(schema["properties"]["fields"]["minItems"], 0)
                self.assertIn("Never silently drop unsupported parts", client.calls[0]["messages"][0]["content"])

    def test_stage_schemas_messages_and_tokens_are_fixed(self):
        result, adapter, client = self.run_request()
        self.assertTrue(result["success"])
        self.assertEqual([item["max_output_tokens"] for item in client.calls], [768, 1536])
        self.assertEqual([item["schema_name"] for item in client.calls], ["document_request_v06c", "document_extract_v06c"])
        fields = field_request()["fields"]
        self.assertEqual(client.calls[1]["response_schema"], pipeline.response_schema(fields))
        candidate_schema = pipeline.response_schema(fields)["properties"]["fields"]["items"]["properties"]["candidates"]["items"]["properties"]
        self.assertEqual(candidate_schema["value"]["type"], "string")
        first_input = json.loads(client.calls[0]["messages"][1]["content"])
        second_input = json.loads(client.calls[1]["messages"][1]["content"])
        self.assertNotIn("chunks", first_input)
        self.assertEqual(second_input["chunks"][0]["text"], adapter.chunks[0]["text"])
        self.assertNotIn("response_schema", second_input)
        self.assertIn("untrusted DATA", client.calls[1]["messages"][0]["content"])
        self.assertIn("Retain units", client.calls[1]["messages"][0]["content"])
        for call in client.calls:
            self.assertLessEqual(len(json.dumps(call["messages"], ensure_ascii=False, separators=(",", ":")).encode()), 16384)

    def test_request_schema_and_runtime_gate_reject_planner_questions_and_assertions(self):
        schema = pipeline.request_schema()
        item = schema["properties"]["fields"]["items"]
        self.assertEqual(set(item["properties"]), {"name", "value_type"})
        self.assertEqual(set(item["required"]), {"name", "value_type"})
        self.assertFalse(item["additionalProperties"])
        for poison in ("$15.25", "37.5 hours", "$40"):
            for key in ("question", "description", "answer"):
                planning = field_request()
                planning["fields"][0][key] = poison
                result, adapter, client = self.run_request(client=StructuredClient(planning))
                self.assertEqual(result["status"], "malformed_model_output")
                self.assertIsNone(result["extraction_request"])
                self.assertEqual(adapter.search_calls, [])
                self.assertEqual(len(client.calls), 1)
                self.assertNotIn(poison, json.dumps(result))
        planning = field_request()
        planning["debug"] = "Invented source claims"
        result, adapter, client = self.run_request(client=StructuredClient(planning))
        self.assertFalse(result["success"])
        self.assertEqual(adapter.search_calls, [])
        self.assertEqual(len(client.calls), 1)

    def test_neutral_queries_and_engine_questions_use_only_validated_generic_labels(self):
        labels = ["surface_color", "salinity-ppt", "supplier region"]
        expected = ["surface color", "salinity ppt", "supplier region"]
        planning = {"task_type": "field_extraction", "fields": [
            {"name": name, "value_type": "text"} for name in labels
        ]}
        result, adapter, client = self.run_request(client=StructuredClient(
            planning, {"task_type": "field_extraction", "fields": []},
        ))
        self.assertEqual(result["status"], "missing")
        self.assertEqual([call["query"] for call in adapter.search_calls], expected)
        questions = [f"What is the {label}?" for label in expected]
        self.assertEqual([field["question"] for field in result["extraction_request"]["fields"]], questions)
        extraction = json.loads(client.calls[1]["messages"][1]["content"])
        self.assertEqual([field["question"] for field in extraction["fields"]], questions)
        self.assertEqual(result["pipeline_version"], "0.6f")
        self.assertEqual(result["version"], engine.VERSION)

    def test_query_groups_are_deterministic_and_never_exceed_four_searches(self):
        for count, queries in (
            (1, ["metric 0"]),
            (4, ["metric 0", "metric 1", "metric 2", "metric 3"]),
            (5, ["metric 0 metric 4", "metric 1", "metric 2", "metric 3"]),
            (8, ["metric 0 metric 4", "metric 1 metric 5", "metric 2 metric 6", "metric 3 metric 7"]),
        ):
            with self.subTest(count=count):
                result, adapter, client = self.run_request(client=StructuredClient(
                    requested_fields(count), {"task_type": "field_extraction", "fields": []},
                ))
                self.assertTrue(result["valid"])
                self.assertEqual([call["query"] for call in adapter.search_calls], queries)
                self.assertLessEqual(len(adapter.search_calls), 4)
                self.assertTrue(all(call["limit"] == 4 for call in adapter.search_calls))
                self.assertEqual(len(client.calls), 2)
                self.assertEqual([call["max_output_tokens"] for call in client.calls], [768, 1536])

    def test_round_robin_merge_caps_sixteen_candidates_at_eight_context_chunks(self):
        rankings = [[ranked_chunk(f"query-{query}-rank-{rank}") for rank in range(4)] for query in range(4)]
        adapter = RankedAdapter(rankings)
        result, _, client = self.run_request(adapter=adapter, client=StructuredClient(
            requested_fields(8), {"task_type": "field_extraction", "fields": []},
        ))
        self.assertTrue(result["valid"])
        context = json.loads(client.calls[1]["messages"][1]["content"])["chunks"]
        self.assertEqual([item["chunk_id"] for item in context],
                         [f"query-{query}-rank-{rank}" for rank in range(2) for query in range(4)])
        self.assertEqual(len(context), 8)
        self.assertEqual(len(adapter.search_calls), 4)
        self.assertEqual(sum(len(row) for row in rankings), 16)
        self.assertTrue(all(call["limit"] == 4 for call in adapter.search_calls))

    def test_identical_chunks_are_stably_deduplicated_across_ranked_searches(self):
        rankings = [
            [ranked_chunk(name) for name in ("shared", "a1", "a2", "a3")],
            [ranked_chunk(name) for name in ("shared", "b1", "a2", "b3")],
        ]
        rankings[0][0]["distance"] = 0.1
        rankings[1][0]["distance"] = 0.3  # Scores are query-specific, not content conflicts.
        for _ in range(2):
            adapter = RankedAdapter(rankings)
            result, _, client = self.run_request(adapter=adapter, client=StructuredClient(
                requested_fields(2), {"task_type": "field_extraction", "fields": []},
            ))
            self.assertTrue(result["valid"])
            context = json.loads(client.calls[1]["messages"][1]["content"])["chunks"]
            self.assertEqual([item["chunk_id"] for item in context], ["shared", "a1", "b1", "a2", "a3", "b3"])
            self.assertEqual(len(context), 6)

    def test_discarded_candidates_are_still_checked_for_conflicts_and_document_scope(self):
        for failure in ("content", "location", "folder", "source"):
            with self.subTest(failure=failure):
                rankings = [[ranked_chunk(f"query-{query}-rank-{rank}") for rank in range(4)] for query in range(4)]
                if failure in {"content", "location"}:
                    rankings[3][3] = copy.deepcopy(rankings[0][3])
                    rankings[3][3]["text" if failure == "content" else "location"] = "Conflicting evidence"
                else:
                    rankings[3][3][failure] = "knowledge" if failure == "folder" else "unselected.txt"
                adapter = RankedAdapter(rankings)
                result, _, client = self.run_request(adapter=adapter, client=StructuredClient(requested_fields(4)))
                self.assertEqual(result["status"], "retrieval_error")
                self.assertFalse(result["success"])
                self.assertEqual(len(adapter.search_calls), 4)
                self.assertEqual(len(client.calls), 1)

    def test_every_response_requires_an_exact_success_count_and_per_search_bound(self):
        good = {"status": "success", "result_count": 4, "results": [ranked_chunk(str(i)) for i in range(4)]}
        invalid = [
            {**good, "result_count": True}, {**good, "result_count": 3}, {**good, "result_count": None},
            {"status": "success", "results": []}, {**good, "status": "empty"},
            {"status": "success", "result_count": 5, "results": [ranked_chunk(str(i)) for i in range(5)]},
        ]
        for bad in invalid:
            adapter = CatalogAdapter()
            adapter.search_document = Mock(side_effect=[copy.deepcopy(good) for _ in range(3)] + [bad])
            result, _, client = self.run_request(adapter=adapter, client=StructuredClient(requested_fields(4)))
            self.assertEqual(result["status"], "retrieval_error")
            self.assertEqual(adapter.search_document.call_count, 4)
            self.assertEqual(len(client.calls), 1)

    def test_rank_four_facts_with_tabs_newlines_and_previous_invented_values_are_genuine_evidence(self):
        names = ["hourly_pay", "scheduled_hours", "daily_fee"]
        values = ["15.25", "37.5", "40"]
        quotes = ["Rate:\t$15.25\nper hour.", "Schedule:\n37.5\thours per week.", "Fee:\t$40\nper day."]
        rankings, fields = [], []
        for index, (name, value, quote) in enumerate(zip(names, values, quotes)):
            fact = ranked_chunk(f"fact-{index}", quote)
            rankings.append([ranked_chunk(f"boilerplate-{rank}") for rank in range(3)] + [fact])
            field = evidence_response(document(), name=name, value=value, quote=quote)["fields"][0]
            field["candidates"][0]["chunk_id"] = fact["chunk_id"]
            fields.append(field)
        planning = {"task_type": "field_extraction", "fields": [{"name": name, "value_type": "numeric"} for name in names]}
        adapter = RankedAdapter(rankings)
        result, _, client = self.run_request(adapter=adapter, client=StructuredClient(
            planning, {"task_type": "field_extraction", "fields": fields},
        ))
        self.assertTrue(result["success"], result)
        self.assertEqual([item["value"] for item in result["fields"]], values)
        self.assertEqual([item["quote"] for item in result["fields"]], quotes)
        self.assertEqual([call["query"] for call in adapter.search_calls], ["hourly pay", "scheduled hours", "daily fee"])
        context = json.loads(client.calls[1]["messages"][1]["content"])["chunks"]
        self.assertEqual([item["chunk_id"] for item in context], ["boilerplate-0", "boilerplate-1", "boilerplate-2", "fact-0", "fact-1", "fact-2"])
        self.assertEqual(result["evidence_validation"], pipeline.EVIDENCE_DISCLAIMER)
        self.assertFalse(result["execution_performed"])
        for value in ("$15.25", "37.5", "$40"):
            self.assertNotIn(value, json.dumps(adapter.search_calls))

    def test_eight_selected_chunks_that_exceed_message_limit_are_not_truncated_or_generated(self):
        rankings = [[ranked_chunk(f"query-{query}-rank-{rank}", "Evidence: " + "x" * 2200)
                     for rank in range(4)] for query in range(4)]
        adapter = RankedAdapter(rankings)
        result, _, client = self.run_request(adapter=adapter, client=StructuredClient(requested_fields(4)))
        self.assertEqual(result["status"], "model_input_too_large")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(adapter.search_calls), 4)
        self.assertTrue(all(item["text"] == "Evidence: " + "x" * 2200 for row in adapter.rankings for item in row))

    def test_partial_results_and_exact_numeric_quote_validation_remain_authoritative(self):
        planning = {"task_type": "field_extraction", "fields": [
            {"name": "measurement", "value_type": "numeric"}, {"name": "label", "value_type": "text"},
        ]}
        quote = "Measurement:\t16.00\nunits."
        for value, expected in (("16.00", "missing"), ("1", "invalid_evidence")):
            response = evidence_response(document(), name="measurement", value=value, quote=quote)
            response["fields"].append({"name": "label", "status": "missing", "candidates": []})
            adapter = CatalogAdapter(chunks=[chunk(document(), quote)])
            result, _, _ = self.run_request(adapter=adapter, client=StructuredClient(planning, response))
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["success"])
            if expected == "missing":
                self.assertEqual(result["fields"][0]["status"], "found")
                self.assertEqual(result["fields"][0]["value"], "16.00")
                self.assertEqual(result["fields"][0]["quote"], quote)
                self.assertEqual(result["fields"][1]["status"], "missing")

    def test_mixed_schema_sent_to_client_binds_numeric_names_to_numeric_branch(self):
        fields = [
            {"name": "occupancy_limit", "value_type": "numeric"},
            {"name": "booking_label", "value_type": "text"},
        ]
        planning = {"task_type": "field_extraction", "fields": fields}
        opener = Mock(side_effect=[Response({"choices": [{"message": {"content": json.dumps(output)}}]})
                                   for output in (planning, {"task_type": "field_extraction", "fields": []})])
        client = pipeline.IntentLMStudioClient(opener=opener)
        result, _, _ = self.run_request(
            "Extract occupancy_limit and booking_label from arrival-card.txt.", client=client,
        )
        self.assertEqual(result["status"], "missing")
        self.assertEqual(opener.call_count, 2)
        body = json.loads(opener.call_args_list[1].args[0].data)
        self.assertEqual(body["max_tokens"], 1536)
        schema = body["response_format"]["json_schema"]["schema"]
        items = schema["properties"]["fields"]["items"]
        # No permissive sibling properties or unbound fallback branch.
        self.assertEqual(set(items), {"anyOf"})
        self.assertEqual(len(items["anyOf"]), 2)
        base = engine.extraction_response_schema()["properties"]["fields"]["items"]
        matched_names = set()
        for branch in items["anyOf"]:
            self.assertEqual(branch["type"], "object")
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(branch["required"], base["required"])
            self.assertEqual(branch["properties"]["status"], base["properties"]["status"])
            candidates = branch["properties"]["candidates"]
            self.assertEqual(candidates["maxItems"], base["properties"]["candidates"]["maxItems"])
            candidate = candidates["items"]
            self.assertFalse(candidate["additionalProperties"])
            self.assertEqual(candidate["required"], base["properties"]["candidates"]["items"]["required"])
            for key in ("source", "chunk_id", "location", "quote"):
                self.assertEqual(candidate["properties"][key], base["properties"]["candidates"]["items"]["properties"][key])
            value = candidate["properties"]["value"]
            self.assertEqual(value["type"], "string")
            self.assertEqual(value["maxLength"], engine.MAX_QUOTE_BYTES)
            names = set(branch["properties"]["name"]["enum"])
            self.assertTrue(matched_names.isdisjoint(names))
            matched_names.update(names)
            if "pattern" in value:
                self.assertEqual(names, {"occupancy_limit"})
                self.assertEqual(value["pattern"], r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$")
            else:
                self.assertEqual(names, {"booking_label"})
                self.assertNotIn("occupancy_limit", names)
        self.assertEqual(matched_names, {field["name"] for field in fields})

    def test_single_type_schema_uses_one_complete_grouped_object(self):
        for value_type in ("text", "numeric"):
            with self.subTest(value_type=value_type):
                requested = engine.validate_extraction_request({
                    "task_type": "field_extraction",
                    "fields": [{"name": f"field_{i}", "question": f"What is field {i}?", "value_type": value_type}
                               for i in range(3)],
                })
                schema = pipeline.response_schema(requested["fields"])
                self.assertEqual(schema["properties"]["fields"]["maxItems"], engine.MAX_FIELDS)
                items = schema["properties"]["fields"]["items"]
                self.assertNotIn("anyOf", items)
                self.assertEqual(items["type"], "object")
                self.assertFalse(items["additionalProperties"])
                self.assertEqual(items["properties"]["name"]["enum"], ["field_0", "field_1", "field_2"])
                value = items["properties"]["candidates"]["items"]["properties"]["value"]
                self.assertEqual("pattern" in value, value_type == "numeric")

    def test_mixed_schema_groups_maximum_fields_into_only_two_branches(self):
        fields = engine.validate_extraction_request({
            "task_type": "field_extraction",
            "fields": [{"name": f"field_{i}", "question": f"What is field {i}?",
                        "value_type": "text" if i % 2 else "numeric"}
                       for i in range(engine.MAX_FIELDS)],
        })["fields"]
        items = pipeline.response_schema(fields)["properties"]["fields"]["items"]
        self.assertEqual(set(items), {"anyOf"})
        self.assertEqual(len(items["anyOf"]), 2)
        for branch in items["anyOf"]:
            value = branch["properties"]["candidates"]["items"]["properties"]["value"]
            value_type = "numeric" if "pattern" in value else "text"
            self.assertEqual(branch["properties"]["name"]["enum"],
                             [field["name"] for field in fields if field["value_type"] == value_type])

    def test_numeric_schema_pattern_accepts_only_complete_literals(self):
        fields = engine.validate_extraction_request({"task_type": "field_extraction", "fields": [{
            "name": "measurement", "question": "What is the measurement?", "value_type": "numeric",
        }]})["fields"]
        items = pipeline.response_schema(fields)["properties"]["fields"]["items"]
        value_schema = items["properties"]["candidates"]["items"]["properties"]["value"]
        pattern = value_schema["pattern"]
        for literal in ("120", "45.50", "-2.50e1", "+.5", "-0.00", "1.", "1e+2", "2E-3"):
            with self.subTest(literal=literal):
                self.assertIsNotNone(re.search(pattern, literal))
        for literal in ("120 people", "18 hours", "NaN", "Infinity", "1e", "1e+", "1e-", "1e.2",
                        "number 120", "120 ", " 120", "--1", "+", ".", "1.2.3"):
            with self.subTest(literal=literal):
                self.assertIsNone(re.search(pattern, literal))

    def test_mixed_extraction_preserves_numeric_literal_and_text_with_units(self):
        fields = [
            {"name": "measurement", "value_type": "numeric"},
            {"name": "duration_label", "value_type": "text"},
        ]
        for literal, normalized in (("120", "120"), ("45.50", "45.50"), ("-2.50e1", "-25.0"), ("+.5", "0.5")):
            with self.subTest(literal=literal):
                quote = f"Measurement: {literal} units."
                text_quote = "Duration label: 18 hours."
                response = evidence_response(document(), name="measurement", value=literal, quote=quote)
                response["fields"].extend(evidence_response(
                    document(), name="duration_label", value="18 hours", quote=text_quote,
                )["fields"])
                adapter = CatalogAdapter(chunks=[chunk(document(), quote + " " + text_quote)])
                client = StructuredClient({"task_type": "field_extraction", "fields": fields}, response)
                with patch.object(engine, "validate_extraction_response", wraps=engine.validate_extraction_response) as validate:
                    result, _, _ = self.run_request(
                        "Extract measurement and duration_label from arrival-card.txt.", adapter=adapter, client=client,
                    )
                self.assertTrue(result["success"], result)
                raw = validate.call_args_list[0].args[0]["fields"][0]["candidates"][0]["value"]
                self.assertEqual(raw, literal)  # No stripping signs/zeros or JSON-number coercion.
                self.assertIsInstance(raw, str)
                self.assertEqual(result["fields"][0]["value"], normalized)
                self.assertEqual(result["fields"][0]["quote"], quote)
                self.assertEqual(result["fields"][1]["value"], "18 hours")
                self.assertEqual(result["fields"][1]["quote"], text_quote)
                self.assertFalse(result["execution_performed"])
                self.assertEqual(result["evidence_validation"], pipeline.EVIDENCE_DISCLAIMER)
                self.assertEqual(result["model_calls"], 2)
                self.assertEqual([call["max_output_tokens"] for call in client.calls], [768, 1536])

    def test_numeric_candidate_with_units_still_fails_closed_without_repair_or_retry(self):
        for raw in ("120 people", "18 hours"):
            with self.subTest(raw=raw):
                quote = f"Capacity: {raw}."
                response = evidence_response(document(), name="capacity", value=raw, quote=quote)
                client = StructuredClient(field_request("capacity", "numeric"), response)
                adapter = CatalogAdapter(chunks=[chunk(document(), quote)])
                result, _, _ = self.run_request("Extract capacity from arrival-card.txt.", adapter=adapter, client=client)
                self.assertEqual(result["status"], "invalid_evidence")
                self.assertFalse(result["success"])
                self.assertEqual(result["fields"][0]["status"], "invalid_evidence")
                self.assertEqual(response["fields"][0]["candidates"][0]["value"], raw)
                self.assertEqual(result["model_calls"], 2)
                self.assertEqual(len(client.calls), 2)
                self.assertEqual([call["max_output_tokens"] for call in client.calls], [768, 1536])

    def test_request_and_extraction_messages_exceeding_budget_are_not_sent_or_truncated(self):
        request = "Extract visitor from arrival-card.txt. " + "\n" * 6000
        result, adapter, client = self.run_request(request, client=StructuredClient())
        self.assertEqual(result["status"], "model_input_too_large")
        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.search_calls, [])
        text = "Visitor: Mira. "
        text += "x" * (engine.MAX_CHUNK_BYTES - len(text.encode("utf-8")))
        adapter = CatalogAdapter(chunks=[chunk(document(), text)])
        result, _, client = self.run_request(adapter=adapter, client=StructuredClient(field_request()))
        self.assertEqual(result["status"], "model_input_too_large")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.chunks[0]["text"], text)

    def test_malformed_outputs_candidate_strings_and_engine_evidence_validation(self):
        for first, second, expected in (
            ("{malformed", None, "malformed_model_output"),
            (field_request(), "{malformed", "malformed_model_output"),
            (field_request(), evidence_response(document(), value=16), "malformed_model_output"),
            (field_request(), evidence_response(document(), quote="Invented quote: Mira"), "invalid_evidence"),
            ({"task_type": "field_extraction", "fields": []}, None, "malformed_model_output"),
        ):
            with self.subTest(expected=expected):
                outputs = [first] if second is None else [first, second]
                result, _, client = self.run_request(client=StructuredClient(*outputs))
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["success"])
                self.assertLessEqual(len(client.calls), 2)

    def test_missing_and_ambiguous_engine_results_are_preserved(self):
        missing = {"task_type": "field_extraction", "fields": []}
        ambiguous = evidence_response(document())
        ambiguous["fields"][0]["status"] = "ambiguous"
        second = copy.deepcopy(ambiguous["fields"][0]["candidates"][0])
        second.update({"value": "Rin", "quote": "Visitor: Rin."})
        ambiguous["fields"][0]["candidates"].append(second)
        for response, status in ((missing, "missing"), (ambiguous, "ambiguous")):
            adapter = CatalogAdapter(chunks=[chunk(document(), "Visitor: Mira. Visitor: Rin.")])
            result, _, _ = self.run_request(adapter=adapter, client=StructuredClient(field_request(), response))
            self.assertTrue(result["valid"])
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], status)

    def test_wrong_folder_or_source_and_excess_chunks_are_rejected(self):
        for chunks in (
            [chunk(document(folder="knowledge"))], [chunk(document("other.txt"))],
            [chunk(document())] * 5,
        ):
            adapter = CatalogAdapter(chunks=chunks)
            result, _, client = self.run_request(adapter=adapter, client=StructuredClient(field_request()))
            self.assertEqual(result["status"], "retrieval_error")
            self.assertEqual(len(client.calls), 1)

    def test_transport_and_internal_failures_are_sanitized_no_retries(self):
        for error, expected in (
            (lm_module.LMUnavailable("private credential detail"), "lm_studio_unavailable"),
            (lm_module.StructuredOutputUnsupported("private provider detail"), "structured_output_unsupported"),
            (lm_module.ModelOutputError("private provider detail"), "model_output_invalid"),
            (RuntimeError("private internal path"), "model_failed"),
        ):
            for stage in (0, 1):
                outputs = [error] if stage == 0 else [field_request(), error]
                result, _, client = self.run_request(client=StructuredClient(*outputs))
                self.assertEqual(result["status"], expected)
                self.assertEqual(len(client.calls), stage + 1)
                self.assertEqual(result["model_calls"], stage + 1)
                self.assertNotIn("private", json.dumps(result))
        adapter = CatalogAdapter()
        adapter.list_documents = Mock(side_effect=RuntimeError("private credential detail"))
        result, _, client = self.run_request(adapter=adapter, client=StructuredClient())
        self.assertEqual(result["status"], "catalog_unavailable")
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(client.calls, [])
        adapter = CatalogAdapter()
        adapter.search_document = Mock(side_effect=RuntimeError("private credential detail"))
        result, _, client = self.run_request(adapter=adapter, client=StructuredClient(field_request()))
        self.assertEqual(result["status"], "retrieval_error")
        self.assertNotIn("private", json.dumps(result))

    def test_empty_and_oversized_requests_fail_before_catalog(self):
        for request in ("", "  ", "x" * 8193, "é" * 4097, None):
            result, adapter, client = self.run_request(request, client=StructuredClient())
            self.assertEqual(result["status"], "invalid_request")
            self.assertEqual(adapter.list_calls, [])
            self.assertEqual(client.calls, [])

    def test_default_adapter_uses_only_existing_fixed_test_credential(self):
        for platform, expected in (("nt", pipeline.WINDOWS_TEST_TOKEN), ("posix", pipeline.LINUX_TEST_TOKEN)):
            with self.subTest(platform=platform):
                # Mock only the platform value, leaving Path's concrete implementation intact.
                with patch.object(pipeline, "os", Mock(name=platform)) as platform_module:
                    platform_module.name = platform
                    with patch.object(pipeline, "create_isolated_adapter") as create:
                        pipeline.create_test_adapter()
                self.assertEqual(str(create.call_args.args[0]), str(Path(expected)))

    def test_cli_prints_json_and_exit_code_requires_success(self):
        for success in (True, False):
            result = {"success": success, "execution_performed": False}
            with patch.object(pipeline, "create_test_adapter") as create_adapter, patch.object(
                pipeline, "IntentLMStudioClient"
            ) as create_client, patch.object(pipeline, "analyze_request", return_value=result) as analyze, patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                status = pipeline.main(["Extract visitor from arrival-card.txt."])
            self.assertEqual(status, 0 if success else 1)
            self.assertEqual(json.loads(output.getvalue()), result)
            analyze.assert_called_once_with(
                "Extract visitor from arrival-card.txt.", adapter=create_adapter.return_value, client=create_client.return_value
            )
        for option in ("--endpoint", "--token-file", "--commit"):
            with patch.object(pipeline, "create_test_adapter") as create, patch("sys.stdout", new_callable=io.StringIO) as output:
                status = pipeline.main(["request", option, "untrusted-value"])
            self.assertNotEqual(status, 0)
            self.assertFalse(json.loads(output.getvalue())["success"])
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
