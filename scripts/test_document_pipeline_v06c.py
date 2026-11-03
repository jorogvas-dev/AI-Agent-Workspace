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


def evidence_response(*, name="visitor", value="Mira", chunk_ref="c1"):
    return {"task_type": "field_extraction", "fields": [{
        "name": name, "candidates": [{
            "value": value, "chunk_ref": chunk_ref,
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


def view_text(chunk_view):
    return "".join(span[1] for span in chunk_view["spans"])


def schema_accepts(schema, value):
    """Interpret only the basic JSON Schema keywords used in provider tests."""
    known = {"anyOf", "type", "const", "enum", "properties", "required", "additionalProperties",
             "items", "minItems", "maxItems", "minLength", "maxLength", "pattern", "minimum", "maximum"}
    if set(schema) - known:
        raise AssertionError("Test schema interpreter needs an explicit keyword update")
    if "anyOf" in schema:
        if set(schema) != {"anyOf"}:
            raise AssertionError("Provider alternatives must not have sibling constraints")
        return any(schema_accepts(branch, value) for branch in schema["anyOf"])
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict) or not set(schema.get("required", [])).issubset(value):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(schema_accepts(properties[key], item) for key, item in value.items() if key in properties)
    if kind == "array":
        return (isinstance(value, list) and schema.get("minItems", 0) <= len(value)
                <= schema.get("maxItems", float("inf"))
                and all(schema_accepts(schema["items"], item) for item in value))
    if kind == "string":
        return (isinstance(value, str) and schema.get("minLength", 0) <= len(value)
                <= schema.get("maxLength", float("inf"))
                and ("pattern" not in schema or re.search(schema["pattern"], value) is not None))
    if kind == "integer":
        return (type(value) is int and schema.get("minimum", float("-inf")) <= value
                <= schema.get("maximum", float("inf")))
    return True


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
        client = client or StructuredClient(field_request(), evidence_response())
        return pipeline.analyze_request(request, adapter=adapter, client=client), adapter, client

    def assert_public_source_evidence(self, result, originals):
        private_keys = {"chunk_ref", "start_span", "end_span", "spans", "bounds", "lookup"}

        def inspect(value):
            if isinstance(value, dict):
                self.assertTrue(private_keys.isdisjoint(value))
                for nested in value.values():
                    inspect(nested)
            elif isinstance(value, list):
                for nested in value:
                    inspect(nested)

        inspect(result)
        originals = {item["chunk_id"]: item for item in originals}
        for field in result["fields"]:
            facts = [field] if field["status"] == "found" else field.get("candidates", [])
            for fact in facts:
                original = originals[fact["chunk_id"]]
                self.assertIn(fact["quote"], original["text"])
                self.assertEqual(fact["source"], original["source"])
                self.assertEqual(fact["location"], original["location"])

    def test_currency_after_number_is_quoted_in_original_order_without_model_quote(self):
        selected = document("transcripts/table-epsilon.txt", "knowledge")
        original = "Published rate\n16.00\t$\tper\thour\nEnd of table"
        adapter = CatalogAdapter([selected], [chunk(selected, original)])
        response = evidence_response(name="published_rate", value="16.00")
        with patch.object(pipeline, "_evidence_spans", wraps=pipeline._evidence_spans) as build:
            result, _, client = self.run_request(
                'Extract published_rate from "knowledge/transcripts/table-epsilon.txt".',
                adapter=adapter, client=StructuredClient(field_request("published_rate", "numeric"), response),
            )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["fields"][0]["quote"], original)
        self.assertNotIn("$16.00", result["fields"][0]["quote"])
        self.assertEqual(result["fields"][0]["value"], "16.00")
        self.assertEqual(build.call_args.args[0][0]["text"], original)
        self.assertEqual(build.call_args.args[0][0]["chunk_id"], "evidence-1")
        self.assertEqual(adapter.chunks[0]["text"], original)
        provider = json.loads(client.calls[1]["messages"][1]["content"])
        self.assertEqual(provider["chunks"][0]["chunk_ref"], "c1")
        self.assertEqual(provider["chunks"][0]["spans"][1], [2, "16.00\t$\tper\thour\n"])
        self.assert_public_source_evidence(result, adapter.chunks)

    def test_unicode_tabs_and_lf_crlf_multiline_ranges_are_exact(self):
        for separator in ("\n", "\r\n"):
            with self.subTest(separator=repr(separator)):
                title = "案内:\tCafé Ω" + separator + " \t" + separator
                measurement = "\tvaleur: 45.50 kg" + separator
                original = "Préface" + separator + title + measurement + "fin"
                adapter = CatalogAdapter(chunks=[chunk(document(), original)])
                planning = {"task_type": "field_extraction", "fields": [
                    {"name": "label", "value_type": "text"}, {"name": "mass", "value_type": "numeric"},
                ]}
                response = evidence_response(name="label", value="Café Ω")
                response["fields"] += evidence_response(name="mass", value="45.50")["fields"]
                result, _, _ = self.run_request(adapter=adapter, client=StructuredClient(planning, response))
                self.assertTrue(result["success"], result)
                self.assertEqual(result["fields"][0]["quote"], original)
                self.assertEqual(result["fields"][1]["quote"], title + measurement + "fin")
                self.assertEqual(adapter.chunks[0]["text"], original)
                self.assert_public_source_evidence(result, adapter.chunks)

    def test_span_selectors_unknown_foreign_reversed_boolean_and_extra_keys_fail_closed(self):
        bad_selectors = [
            {"chunk_ref": "c99"}, {"chunk_ref": "evidence-1"}, {"chunk_ref": "foreign-chunk"},
            {"chunk_ref": True}, {"chunk_ref": {}},
            {"start_span": 0}, {"start_span": -1}, {"start_span": True},
            {"end_span": False}, {"end_span": 1.0}, {"end_span": "2"},
            {"start_span": 2, "end_span": 1}, {"end_span": 3}, {"end_span": 65},
            {"start_span": "c2:1"}, {"end_span": "c2:2"},
        ]
        bad_selectors += [{key: "model-authored"} for key in (
            "quote", "source", "location", "chunk_id", "start_offset", "end_offset", "end_chunk_ref", "status",
        )]
        for mutation in bad_selectors:
            with self.subTest(mutation=mutation):
                adapter = CatalogAdapter(chunks=[chunk(document(), "Visitor: Mira.\nAlternate: Rin.")])
                response = evidence_response()
                response["fields"][0]["candidates"][0].update(mutation)
                result, _, client = self.run_request(adapter=adapter, client=StructuredClient(field_request(), response))
                self.assertEqual(result["status"], "malformed_model_output")
                self.assertFalse(result["success"])
                self.assertEqual(len(client.calls), 2)
                self.assert_public_source_evidence(result, adapter.chunks)

    def test_provider_malformed_structures_and_candidate_cardinality_still_fail_closed(self):
        cases = [
            {"task_type": "field_extraction", "fields": None},
            {"task_type": "field_extraction", "fields": ["not a field"]},
        ]
        for mutation in (
            {"status": "missing"}, {"status": "ambiguous"}, {"status": []},
            {"status": "found"}, {"candidates": ["not a candidate"]}, {"candidates": {}},
            {"quote": "authored"}, {"name": "unknown_field"},
        ):
            response = evidence_response()
            response["fields"][0].update(mutation)
            cases.append(response)
        response = evidence_response()
        del response["fields"][0]["candidates"][0]["chunk_ref"]
        cases.append(response)
        response = evidence_response()
        response["fields"] *= 2
        cases.append(response)
        response = evidence_response()
        response["fields"][0]["candidates"] *= engine.MAX_CANDIDATES + 1
        cases.append(response)
        for response in cases:
            result, _, client = self.run_request(client=StructuredClient(field_request(), response))
            self.assertEqual(result["status"], "malformed_model_output")
            self.assertEqual(len(client.calls), 2)

    def test_valid_reference_to_wrong_chunk_and_wrong_numeric_literal_remain_invalid_evidence(self):
        originals = [ranked_chunk("original-a", "Measurement: 16.00\t$\tper\thour"),
                     ranked_chunk("original-b", "Measurement: 20.00 units")]
        for value, reference in (("1", "c1"), ("16", "c1"), ("999", "c1"), ("16.00", "c2")):
            with self.subTest(value=value, reference=reference):
                adapter = CatalogAdapter(chunks=originals)
                response = evidence_response(name="measurement", value=value, chunk_ref=reference)
                result, _, _ = self.run_request(adapter=adapter, client=StructuredClient(
                    field_request("measurement", "numeric"), response,
                ))
                self.assertEqual(result["status"], "invalid_evidence")
                self.assertFalse(result["success"])
                self.assertNotIn("value", result["fields"][0])

    def test_coalescing_is_bounded_contiguous_and_preserves_every_source_character(self):
        original = "\r\n\t\n" + "".join(f"Liné {number}\tΩ\r\n\r\n" for number in range(130)) + " \t\r\n"
        originals = [chunk(document(), original)]
        view, lookup = pipeline._evidence_spans(originals)
        self.assertEqual(len(view[0]["spans"]), 64)
        self.assertEqual(view_text(view[0]), original)
        self.assertEqual([item[0] for item in view[0]["spans"]], list(range(1, 65)))
        bounds = lookup["c1"]["bounds"]
        self.assertEqual(bounds[0][0], 0)
        self.assertEqual(bounds[-1][1], len(original))
        for index, (start, end) in enumerate(bounds):
            self.assertEqual(original[start:end], view[0]["spans"][index][1])
            self.assertTrue(view[0]["spans"][index][1].strip())
            if index:
                self.assertEqual(start, bounds[index - 1][1])
        response = evidence_response(name="line_label", value="Liné 70")
        result, _, client = self.run_request(adapter=CatalogAdapter(chunks=originals), client=StructuredClient(
            field_request("line_label", "text"), response,
        ))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["fields"][0]["quote"], original[bounds[33][0]:bounds[36][1]])
        self.assertEqual(client.calls[1]["schema_name"], "document_extract_v06g")
        self.assert_public_source_evidence(result, originals)

    def test_every_agreeing_candidate_is_verified_before_deduplication(self):
        originals = [ranked_chunk("support", "Measurement: 16.00 units"),
                     ranked_chunk("wrong", "Measurement: 20.00 units"),
                     ranked_chunk("oversized", "Measurement: 16.00 units " + "x" * engine.MAX_QUOTE_BYTES),
                     ranked_chunk("engine-invalid", "9" * 129 + "\nMeasurement: 16.00 units")]
        for bad, expected in (
            ({"value": "16.00", "chunk_ref": "c99"}, "malformed_model_output"),
            ({"value": "16.00", "chunk_ref": "c2"}, "invalid_evidence"),
            ({"value": "16", "chunk_ref": "c1"}, "invalid_evidence"),
            ({"value": "16.00", "chunk_ref": "c1", "start_span": 1}, "malformed_model_output"),
            ({"value": "16.00", "chunk_ref": "c1", "status": "found"}, "malformed_model_output"),
            ({"value": 16, "chunk_ref": "c1"}, "malformed_model_output"),
            ({"value": "16.00", "chunk_ref": "c3"}, "malformed_model_output"),
            ({"value": "16.00", "chunk_ref": "c4"}, "invalid_evidence"),
        ):
            for reverse in (False, True):
                with self.subTest(bad=bad, reverse=reverse):
                    response = evidence_response(name="measurement", value="16.00")
                    response["fields"][0]["candidates"].append(bad)
                    if reverse:
                        response["fields"][0]["candidates"].reverse()
                    result, _, client = self.run_request(adapter=CatalogAdapter(chunks=originals), client=StructuredClient(
                        field_request("measurement", "numeric"), response,
                    ))
                    self.assertEqual(result["status"], expected)
                    self.assertFalse(result["success"])
                    self.assertEqual(result["model_calls"], 2)
                    self.assertEqual(len(client.calls), 2)
        response = evidence_response()
        response["fields"][0]["candidates"].append({"value": "Mira", "chunk_ref": "c2"})
        result, _, _ = self.run_request(adapter=CatalogAdapter(chunks=[ranked_chunk("a", "Visitor: Mira"),
                                                                     ranked_chunk("b", "Visitor: Rin")]),
                                        client=StructuredClient(field_request(), response))
        self.assertEqual(result["status"], "invalid_evidence")

    def test_agreeing_numeric_decimals_merge_with_an_actual_supporting_citation(self):
        literals = ["16.00", "+16", "1.600e1", "16.0"]
        originals = [ranked_chunk(f"support-{index}", f"Measurement: {literal} units")
                     for index, literal in enumerate(literals)]
        response = evidence_response(name="measurement", value=literals[0])
        response["fields"][0]["candidates"] += [
            {"value": literal, "chunk_ref": f"c{index}"} for index, literal in enumerate(literals[1:], start=2)
        ]
        for _ in range(2):
            with patch.object(engine, "_validate_candidate", wraps=engine._validate_candidate) as validate:
                result, _, _ = self.run_request(adapter=CatalogAdapter(chunks=originals), client=StructuredClient(
                    field_request("measurement", "numeric"), response,
                ))
            self.assertGreaterEqual(validate.call_count, len(literals))
            self.assertTrue(result["success"], result)
            field = result["fields"][0]
            self.assertEqual(field["status"], "found")
            self.assertEqual(field["value"], "16.00")
            self.assertEqual(field["chunk_id"], originals[0]["chunk_id"])
            self.assertEqual(field["quote"], originals[0]["text"])
            self.assert_public_source_evidence(result, originals)

    def test_server_status_uses_distinct_verified_values_for_text_and_numeric_fields(self):
        for value_type, first, second in (("numeric", "16.00", "17.00"), ("text", "Mira", "mira"),
                                          ("text", "Mira", "Mira ")):
            originals = [ranked_chunk("a", f"Label: {first}\nUnits."), ranked_chunk("b", f"Label: {second}\nUnits.")]
            for values, expected in (([], "missing"), ([first], "found"), ([first, first], "found"),
                                     ([first, second, first], "ambiguous")):
                response = evidence_response(name="measurement", value=first)
                response["fields"][0]["candidates"] = [
                    {"value": value, "chunk_ref": "c2" if value == second else "c1"} for value in values
                ]
                result, _, _ = self.run_request(adapter=CatalogAdapter(chunks=originals), client=StructuredClient(
                    field_request("measurement", value_type), response,
                ))
                field = result["fields"][0]
                self.assertEqual(field["status"], expected)
                self.assertEqual(result["status"], "success" if expected == "found" else expected)
                self.assertEqual(result["success"], expected == "found")
                self.assertTrue(result["valid"])
                if expected == "ambiguous":
                    self.assertEqual([item["value"] for item in field["candidates"]], [first, second])
                self.assert_public_source_evidence(result, originals)

    def test_quotes_cover_all_literal_matches_with_one_previous_and_two_following_spans(self):
        for separator in ("\n", "\r\n"):
            lines = ["Outside start", " \t", "Earlier context", "Published rate", "\t16.00\t$", "per", "hour",
                     "middle context", "Repeated rate", "16.00\t$", "per", "hour", "Outside end"]
            original = separator.join(lines)
            expected = separator.join(lines[3:12]) + separator
            for value_type in ("numeric", "text"):
                result, adapter, _ = self.run_request(adapter=CatalogAdapter(chunks=[chunk(document(), original)]),
                                                      client=StructuredClient(field_request("measurement", value_type),
                                                                              evidence_response(name="measurement", value="16.00")))
                self.assertTrue(result["success"], result)
                self.assertEqual(result["fields"][0]["quote"], expected)
                self.assertEqual(result["fields"][0]["quote"].count("16.00"), 2)
                self.assert_public_source_evidence(result, adapter.chunks)

    def test_text_matching_is_literal_across_spans_without_regex_or_whitespace_repair(self):
        literal = "A[1].*\t\nnext line"
        original = "Outside\nLabel\n" + literal + "\nUnit\nTail\nOutside end"
        result, adapter, _ = self.run_request(adapter=CatalogAdapter(chunks=[chunk(document(), original)]),
                                              client=StructuredClient(field_request(), evidence_response(value=literal)))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["fields"][0]["quote"], "Label\n" + literal + "\nUnit\nTail\n")
        self.assert_public_source_evidence(result, adapter.chunks)
        for value_type, source, unsupported in (
            ("text", "Visitor: Mira", "M.*"), ("text", "Label: A[1]", "A1"),
            ("text", "Label: A\tB", "A B"), ("text", "Label: A\nB", "A B"),
            ("text", "Rate: 16.00\t$", "$16.00"), ("text", "Visitor: Mira", ""),
            ("numeric", "Rate: 16.00", " 16.00"), ("numeric", "Rate: 16.00", "16.00 "),
            ("numeric", "Rate: 1e2", "100"), ("numeric", "Rate: -16.00", "16.00"),
            ("numeric", "Rate: 116.00", "16.00"), ("numeric", "Rate: 16.00e2", "16.00"),
        ):
            with self.subTest(source=source, unsupported=unsupported):
                result, _, _ = self.run_request(adapter=CatalogAdapter(chunks=[chunk(document(), source)]),
                                                client=StructuredClient(field_request("measurement", value_type),
                                                                        evidence_response(name="measurement", value=unsupported)))
                self.assertEqual(result["status"], "invalid_evidence")
                self.assertFalse(result["success"])

    def test_all_matches_and_context_must_fit_quote_limit_without_truncation(self):
        for original, value in (
            ("Visitor: Mira\n" + "x" * engine.MAX_QUOTE_BYTES + "\nVisitor: Mira", "Mira"),
            ("Label: " + "x" * engine.MAX_QUOTE_BYTES + "\nVisitor: Mira\nUnit\nTail", "Mira"),
            ("Visitor: Mira\nUnit\n" + "x" * engine.MAX_QUOTE_BYTES, "Mira"),
            ("Measurement: 16.00\n" + "x" * engine.MAX_QUOTE_BYTES + "\nMeasurement: 16.00", "16.00"),
        ):
            response = evidence_response(value=value)
            value_type = "numeric" if value == "16.00" else "text"
            result, adapter, client = self.run_request(adapter=CatalogAdapter(chunks=[chunk(document(), original)]),
                                                       client=StructuredClient(field_request(value_type=value_type), response))
            self.assertEqual(result["status"], "malformed_model_output")
            self.assertFalse(result["success"])
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(adapter.chunks[0]["text"], original)
        for extra, expected in ((0, "success"), (1, "malformed_model_output")):
            original = "Mira" + "é" * ((engine.MAX_QUOTE_BYTES - 4) // 2) + "x" * extra
            result, _, _ = self.run_request(adapter=CatalogAdapter(chunks=[chunk(document(), original)]))
            self.assertEqual(result["status"], expected)

    def test_early_source_failure_preserves_requested_field_order_and_partial_results(self):
        planning = {"task_type": "field_extraction", "fields": [
            {"name": "optional_label", "value_type": "text"}, {"name": "visitor", "value_type": "text"},
            {"name": "measurement", "value_type": "numeric"},
        ]}
        response = evidence_response(name="measurement", value="16")
        response["fields"] += evidence_response()["fields"]
        result, _, _ = self.run_request(adapter=CatalogAdapter(chunks=[chunk(document(), "Visitor: Mira\nMeasurement: 16.00")]),
                                        client=StructuredClient(planning, response))
        self.assertEqual(result["status"], "invalid_evidence")
        self.assertFalse(result["success"])
        self.assertEqual([field["name"] for field in result["fields"]], [field["name"] for field in planning["fields"]])
        self.assertEqual([field["status"] for field in result["fields"]], ["missing", "found", "invalid_evidence"])
        self.assertEqual([error["code"] for error in result["errors"]], ["missing_field", "invalid_evidence"])

    def test_span_view_overhead_is_included_in_actual_message_limit(self):
        text = "evidence detailed record\n" * 64
        rankings = [[ranked_chunk(f"query-{query}-rank-{rank}", text) for rank in range(4)] for query in range(4)]
        adapter = RankedAdapter(rankings)
        result, _, client = self.run_request(adapter=adapter, client=StructuredClient(requested_fields(4)))
        self.assertLess(8 * len(text.encode()), 16 * 1024)
        self.assertEqual(result["status"], "model_input_too_large")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(adapter.search_calls), 4)
        self.assertTrue(all(item["text"] == text for row in rankings for item in row))

    def test_seven_chunks_244_spans_fit_compact_view_with_identical_source_and_reconstruction(self):
        originals = [ranked_chunk(
            f"compact-chunk-{index}",
            "".join(f"Évidence {index}:{line:02d}\tmarker record.\r\n" for line in range(count)),
        ) for index, count in enumerate((35, 35, 35, 35, 35, 35, 34))]
        rankings = [originals[::2], originals[1::2]]
        planning = {"task_type": "field_extraction", "fields": [
            {"name": "marker_label", "value_type": "text"},
            {"name": "line_label", "value_type": "text"},
        ]}
        response = evidence_response(name="marker_label", value="marker record.", chunk_ref="c7")
        response["fields"] += evidence_response(name="line_label", value="Évidence 0:01")["fields"]
        real_builder = pipeline._evidence_spans
        lookups = {}

        def render(chunks, expanded=False):
            view, lookup = real_builder(chunks)
            lookups["expanded" if expanded else "compact"] = copy.deepcopy(lookup)
            if expanded:
                view = [{"chunk_ref": item["chunk_ref"], "spans": [
                    {"span": number, "text": text} for number, text in item["spans"]
                ]} for item in view]
            return view, lookup

        request = "Extract marker_label and line_label from arrival-card.txt."
        with patch.object(pipeline, "_evidence_spans", side_effect=render):
            result, _, client = self.run_request(request, adapter=RankedAdapter(rankings), client=StructuredClient(planning, response))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["pipeline_version"], "0.6g")
        messages = client.calls[1]["messages"]
        compact = json.loads(messages[1]["content"])
        self.assertEqual(len(compact["chunks"]), 7)
        self.assertEqual(sum(len(item["spans"]) for item in compact["chunks"]), 244)
        for view, original in zip(compact["chunks"], originals):
            self.assertTrue(all(isinstance(pair, list) and len(pair) == 2 for pair in view["spans"]))
            self.assertEqual(view_text(view), original["text"])
            self.assertEqual([pair[0] for pair in view["spans"]], list(range(1, len(view["spans"]) + 1)))
        expanded = copy.deepcopy(compact)
        for item in expanded["chunks"]:
            item["spans"] = [{"span": number, "text": text} for number, text in item["spans"]]
        expanded_messages = copy.deepcopy(messages)
        expanded_messages[1]["content"] = json.dumps(expanded, ensure_ascii=False, separators=(",", ":"))
        compact_bytes = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        expanded_bytes = len(json.dumps(expanded_messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(pipeline.MAX_MESSAGE_BYTES, 16384)
        self.assertLessEqual(compact_bytes, 16384, f"compact={compact_bytes}, expanded={expanded_bytes}")
        self.assertGreater(expanded_bytes, 16384, f"compact={compact_bytes}, expanded={expanded_bytes}")
        with patch.object(pipeline, "_evidence_spans", side_effect=lambda chunks: render(chunks, expanded=True)):
            rejected, _, stopped_client = self.run_request(request, adapter=RankedAdapter(rankings), client=StructuredClient(planning))
        self.assertEqual(rejected["status"], "model_input_too_large")
        self.assertEqual(len(stopped_client.calls), 1)
        self.assertEqual(lookups["compact"], lookups["expanded"])
        fields = result["extraction_request"]["fields"]
        self.assertEqual(pipeline._engine_response(response, fields, lookups["compact"]),
                         pipeline._engine_response(response, fields, lookups["expanded"]))
        self.assertEqual(result["fields"][0]["quote"], originals[6]["text"])
        self.assertEqual(result["fields"][1]["quote"], "".join(originals[0]["text"].splitlines(keepends=True)[:4]))
        self.assert_public_source_evidence(result, originals)

    def test_reconstructed_quote_size_bound_is_not_relaxed_or_truncated(self):
        original = "Measurement: 16.00 " + "x" * engine.MAX_QUOTE_BYTES
        adapter = CatalogAdapter(chunks=[chunk(document(), original)])
        result, _, client = self.run_request(adapter=adapter, client=StructuredClient(
            field_request("measurement", "numeric"), evidence_response(name="measurement", value="16.00"),
        ))
        self.assertEqual(result["status"], "malformed_model_output")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(adapter.chunks[0]["text"], original)

    def test_span_backed_partial_and_ambiguous_results_keep_distinct_source_evidence(self):
        originals = [ranked_chunk("original-a", "Visitor: Mira.\r\nTeam: Blue."), ranked_chunk("original-b", "Visitor: Rin.")]
        for ambiguous in (False, True):
            response = evidence_response()
            if ambiguous:
                response["fields"][0]["candidates"] += evidence_response(value="Rin", chunk_ref="c2")["fields"][0]["candidates"]
            response["fields"].append({"name": "optional_label", "candidates": []})
            planning = {"task_type": "field_extraction", "fields": [
                {"name": "visitor", "value_type": "text"}, {"name": "optional_label", "value_type": "text"},
            ]}
            result, _, _ = self.run_request(adapter=CatalogAdapter(chunks=originals), client=StructuredClient(planning, response))
            self.assertEqual(result["status"], "ambiguous" if ambiguous else "missing")
            self.assertTrue(result["valid"])
            self.assertFalse(result["success"])
            self.assertEqual(result["fields"][1]["status"], "missing")
            if ambiguous:
                self.assertEqual([item["quote"] for item in result["fields"][0]["candidates"]], [originals[0]["text"], "Visitor: Rin."])
            else:
                self.assertEqual(result["fields"][0]["quote"], originals[0]["text"])
            self.assert_public_source_evidence(result, originals)

    def test_two_unrelated_documents_generic_text_numeric_and_exact_scope(self):
        first = document()
        second = document("lab notes/readout-472.csv", "knowledge")
        for selected, name, kind, value, quote, request in (
            (first, "visitor", "text", "Mira", "Visitor: Mira.", 'Extract visitor from "input/registrations/arrival-card.txt".'),
            (second, "sample_mass", "numeric", "-2.50e1", "Sample mass: -2.50e1 grams.", 'Extract sample_mass from “knowledge/lab notes/readout-472.csv”.'),
        ):
            with self.subTest(selected=selected):
                adapter = CatalogAdapter([first, second], [chunk(selected, quote)])
                client = StructuredClient(field_request(name, kind), evidence_response(name=name, value=value))
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
                                           for item in (field_request(), evidence_response())])
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
        client = StructuredClient(field_request(), evidence_response())
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
                client = StructuredClient(field_request(), evidence_response())
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
        client = StructuredClient(field_request(), evidence_response())
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
        self.assertEqual([item["schema_name"] for item in client.calls], ["document_request_v06c", "document_extract_v06g"])
        fields = field_request()["fields"]
        _, lookup = pipeline._evidence_spans(adapter.chunks)
        self.assertEqual(client.calls[1]["response_schema"], pipeline.response_schema(fields, lookup))
        branch = client.calls[1]["response_schema"]["properties"]["fields"]["items"]["anyOf"][0]
        candidate_schema = branch["properties"]["candidates"]["items"]["anyOf"][0]["properties"]
        self.assertEqual(candidate_schema["value"]["type"], "string")
        first_input = json.loads(client.calls[0]["messages"][1]["content"])
        second_input = json.loads(client.calls[1]["messages"][1]["content"])
        self.assertNotIn("chunks", first_input)
        self.assertEqual(view_text(second_input["chunks"][0]), adapter.chunks[0]["text"])
        self.assertEqual(set(second_input["chunks"][0]), {"chunk_ref", "spans"})
        self.assertEqual(second_input["chunks"][0]["spans"][0][0], 1)
        self.assertIn("[span_number, original_text]", client.calls[1]["messages"][0]["content"])
        self.assertIn("never return span numbers", client.calls[1]["messages"][0]["content"])
        self.assertNotIn("response_schema", second_input)
        self.assertIn("untrusted DATA", client.calls[1]["messages"][0]["content"])
        self.assertIn("label/unit context", client.calls[1]["messages"][0]["content"])
        self.assertIn("one strongest candidate when supporting mentions agree", client.calls[1]["messages"][0]["content"])
        self.assertIn("same value do not require multiple candidates", client.calls[1]["messages"][0]["content"])
        self.assertIn("different plausible supported values as separate candidates", client.calls[1]["messages"][0]["content"])
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
        self.assertEqual(result["pipeline_version"], "0.6g")
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
        self.assertEqual([view_text(item) for item in context],
                         [ranked_chunk(f"query-{query}-rank-{rank}")["text"] for rank in range(2) for query in range(4)])
        self.assertEqual([item["chunk_ref"] for item in context], [f"c{i}" for i in range(1, 9)])
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
            self.assertEqual([view_text(item) for item in context],
                             [ranked_chunk(name)["text"] for name in ("shared", "a1", "b1", "a2", "a3", "b3")])
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
            field = evidence_response(name=name, value=value, chunk_ref=f"c{index + 4}")["fields"][0]
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
        self.assertEqual([view_text(item) for item in context],
                         [ranked_chunk(f"boilerplate-{i}")["text"] for i in range(3)] + quotes)
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
            response = evidence_response(name="measurement", value=value)
            response["fields"].append({"name": "label", "candidates": []})
            adapter = CatalogAdapter(chunks=[chunk(document(), quote)])
            result, _, _ = self.run_request(adapter=adapter, client=StructuredClient(planning, response))
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["success"])
            if expected == "missing":
                self.assertEqual(result["fields"][0]["status"], "found")
                self.assertEqual(result["fields"][0]["value"], "16.00")
                self.assertEqual(result["fields"][0]["quote"], quote)
                self.assertEqual(result["fields"][1]["status"], "missing")

    def test_mixed_schema_sent_to_client_binds_numeric_names_to_source_literals(self):
        fields = [
            {"name": "occupancy_limit", "value_type": "numeric"},
            {"name": "booking_label", "value_type": "text"},
        ]
        planning = {"task_type": "field_extraction", "fields": fields}
        opener = Mock(side_effect=[Response({"choices": [{"message": {"content": json.dumps(output)}}]})
                                   for output in (planning, {"task_type": "field_extraction", "fields": []})])
        client = pipeline.IntentLMStudioClient(opener=opener)
        originals = [chunk(document(), "Capacity: 120\nBooking: 120 people.")]
        result, _, _ = self.run_request(
            "Extract occupancy_limit and booking_label from arrival-card.txt.",
            adapter=CatalogAdapter(chunks=originals), client=client,
        )
        self.assertEqual(result["status"], "missing")
        self.assertEqual(opener.call_count, 2)
        body = json.loads(opener.call_args_list[1].args[0].data)
        self.assertEqual(body["max_tokens"], 1536)
        schema = body["response_format"]["json_schema"]["schema"]
        items = schema["properties"]["fields"]["items"]
        self.assertEqual(set(items), {"anyOf"})
        self.assertEqual(len(items["anyOf"]), 2)
        for branch in items["anyOf"]:
            self.assertEqual(branch["type"], "object")
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(branch["required"], ["name", "candidates"])
            self.assertEqual(list(branch["properties"]), ["name", "candidates"])
            candidates = branch["properties"]["candidates"]
            self.assertEqual((candidates["minItems"], candidates["maxItems"]), (0, engine.MAX_CANDIDATES))
            self.assertEqual(set(candidates["items"]), {"anyOf"})
            candidate = candidates["items"]["anyOf"][0]
            self.assertEqual(candidate["type"], "object")
            self.assertFalse(candidate["additionalProperties"])
            self.assertEqual(set(candidate["required"]), {"value", "chunk_ref"})
            self.assertEqual(set(candidate["properties"]), set(candidate["required"]))
            self.assertEqual(candidate["properties"]["chunk_ref"], {"type": "string", "const": "c1"})
            value = candidate["properties"]["value"]
            self.assertEqual(value["type"], "string")
            if branch["properties"]["name"]["enum"] == ["occupancy_limit"]:
                self.assertEqual(value["enum"], ["120"])
            else:
                self.assertEqual(branch["properties"]["name"]["enum"], ["booking_label"])
                self.assertNotIn("enum", value)
                self.assertEqual(value["maxLength"], engine.MAX_QUOTE_BYTES)
        for name, value, accepted in (("occupancy_limit", "120 people", False),
                                      ("occupancy_limit", "120", True), ("booking_label", "120 people", True)):
            self.assertEqual(schema_accepts(items, evidence_response(name=name, value=value)["fields"][0]), accepted)

    def test_schema_groups_names_into_one_complete_object_per_requested_type(self):
        for types in (("text",), ("numeric",), ("numeric", "text")):
            fields = [{"name": f"field_{index}", "value_type": types[index % len(types)]}
                      for index in range(engine.MAX_FIELDS)]
            _, lookup = pipeline._evidence_spans([chunk(document(), "Measurement: 16.00 units")])
            schema = pipeline.response_schema(fields, lookup)
            self.assertEqual(schema["properties"]["fields"]["maxItems"], engine.MAX_FIELDS)
            items = schema["properties"]["fields"]["items"]
            self.assertEqual(set(items), {"anyOf"})
            self.assertEqual(len(items["anyOf"]), len(types))
            for branch in items["anyOf"]:
                self.assertFalse(branch["additionalProperties"])
                self.assertEqual(list(branch["properties"]), ["name", "candidates"])
                value = branch["properties"]["candidates"]["items"]["anyOf"][0]["properties"]["value"]
                value_type = "numeric" if "enum" in value else "text"
                self.assertEqual(branch["properties"]["name"]["enum"],
                                 [field["name"] for field in fields if field["value_type"] == value_type])

    def test_numeric_enum_preserves_complete_literals_and_engine_bounds(self):
        literals = ["120", "45.50", "-2.50e1", "+.5", "-0.00", "1.", "1e+2", "2E-3", "1e255", "1e-254"]
        invalid = ["NaN", "Infinity", "1e", "1e+", "1e-", "1e.2", "--1", "+", ".", "1.2.3",
                   "9" * (engine.MAX_RAW_NUMERIC_BYTES + 1), "1e10000000", "1e-10000000", "-1e255", "1e-255"]
        original = "\n".join(literals + invalid + ["45.50"])
        _, lookup = pipeline._evidence_spans([chunk(document(), original)])
        schema = pipeline.response_schema(field_request("measurement", "numeric")["fields"], lookup)
        items = schema["properties"]["fields"]["items"]
        values = items["anyOf"][0]["properties"]["candidates"]["items"]["anyOf"][0]["properties"]["value"]
        self.assertEqual(values, {"type": "string", "enum": literals})
        for literal in literals:
            self.assertTrue(schema_accepts(items, evidence_response(name="measurement", value=literal)["fields"][0]))
        for literal in invalid + ["120 people", "18 hours", "number 120", "120 ", " 120", "45.5", "2e-3"]:
            self.assertFalse(schema_accepts(items, evidence_response(name="measurement", value=literal)["fields"][0]))

    def test_empty_numeric_alternatives_permit_only_empty_candidate_lists(self):
        fields = field_request("measurement", "numeric")["fields"]
        for original in ("No numeric evidence.", "1e1000000\n" + "9" * 129):
            _, lookup = pipeline._evidence_spans([chunk(document(), original)])
            schema = pipeline.response_schema(fields, lookup)
            response = evidence_response(name="measurement", value="1")
            self.assertFalse(schema_accepts(schema, response))
            result, _, _ = self.run_request(adapter=CatalogAdapter(chunks=[chunk(document(), original)]),
                                            client=StructuredClient(field_request("measurement", "numeric"), response))
            self.assertEqual(result["status"], "invalid_evidence")
            response["fields"][0]["candidates"] = []
            self.assertTrue(schema_accepts(schema, response))
            result, _, _ = self.run_request(adapter=CatalogAdapter(chunks=[chunk(document(), original)]),
                                            client=StructuredClient(field_request("measurement", "numeric"), response))
            self.assertEqual(result["status"], "missing")
            self.assertEqual(result["fields"][0]["status"], "missing")

    def test_minimal_field_schema_limits_candidate_count_and_rejects_status(self):
        fields = [{"name": "label", "value_type": "text"}, {"name": "capacity", "value_type": "numeric"}]
        _, lookup = pipeline._evidence_spans([chunk(document(), "Label: 18 hours\nCapacity: +.5")])
        items = pipeline.response_schema(fields, lookup)["properties"]["fields"]["items"]
        for name, value in (("label", "18 hours"), ("capacity", "+.5")):
            for count in range(engine.MAX_CANDIDATES + 2):
                field = evidence_response(name=name, value=value)["fields"][0]
                field["candidates"] *= count
                self.assertEqual(schema_accepts(items, field), count <= engine.MAX_CANDIDATES)
                for status in ("found", "missing", "ambiguous", "invalid_status"):
                    self.assertFalse(schema_accepts(items, {**field, "status": status}))
        self.assertFalse(schema_accepts(items, evidence_response(name="unknown")["fields"][0]))

    def test_numeric_enum_binds_exact_spelling_to_correct_chunk_without_fallback(self):
        fields = [{"name": "label", "value_type": "text"}, {"name": "measurement", "value_type": "numeric"}]
        originals = [ranked_chunk("a", "Measurement: 16.00"), ranked_chunk("b", "Measurement: -2.50e1\nOther: +.5"),
                     ranked_chunk("c", "No numeric evidence.")]
        _, lookup = pipeline._evidence_spans(originals)
        schema = pipeline.response_schema(fields, lookup)
        branches = schema["properties"]["fields"]["items"]["anyOf"]
        for branch in branches:
            numeric = branch["properties"]["name"]["enum"] == ["measurement"]
            alternatives = branch["properties"]["candidates"]["items"]["anyOf"]
            self.assertEqual([item["properties"]["chunk_ref"]["const"] for item in alternatives],
                             ["c1", "c2"] if numeric else ["c1", "c2", "c3"])
            for candidate in alternatives:
                self.assertEqual(set(candidate), {"type", "additionalProperties", "required", "properties"})
                self.assertEqual(set(candidate["required"]), {"value", "chunk_ref"})
                self.assertEqual(set(candidate["properties"]), {"value", "chunk_ref"})
                self.assertFalse(candidate["additionalProperties"])
        for reference, value, accepted in (("c1", "16.00", True), ("c1", "16", False), ("c1", "-2.50e1", False),
                                            ("c2", "16.00", False), ("c2", "-2.50e1", True), ("c2", "+.5", True),
                                            ("c3", "16.00", False), ("c99", "16.00", False)):
            response = evidence_response(name="measurement", value=value, chunk_ref=reference)
            self.assertEqual(schema_accepts(schema, response), accepted)
            for key in ("start_span", "end_span", "start_offset", "end_offset", "quote", "source", "chunk_id", "location", "status"):
                bad = copy.deepcopy(response)
                bad["fields"][0]["candidates"][0][key] = 1
                self.assertFalse(schema_accepts(schema, bad))

    def test_correct_values_with_previously_wrong_ranges_need_no_coordinates(self):
        originals = [ranked_chunk(f"source-{index}", "Visitor: Mira.\n" * count)
                     for index, count in enumerate((53, 1, 1, 1, 1, 14))]
        planning = {"task_type": "field_extraction", "fields": [
            {"name": "visitor", "value_type": "text"}, {"name": "optional_label", "value_type": "text"},
        ]}
        response = evidence_response(chunk_ref="c6")
        result, _, client = self.run_request(adapter=RankedAdapter([originals[::2], originals[1::2]]),
                                            client=StructuredClient(planning, response))
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["fields"][0]["status"], "found")
        self.assertEqual(result["fields"][0]["quote"], originals[5]["text"])
        self.assertEqual(len(client.calls), 2)
        _, lookup = pipeline._evidence_spans(originals)
        schema = client.calls[1]["response_schema"]
        self.assertEqual(schema, pipeline.response_schema(planning["fields"], lookup))
        self.assertTrue(schema_accepts(schema, response))
        self.assert_public_source_evidence(result, originals)

    def test_maximum_generated_schema_fits_existing_client_size_bound(self):
        fields = engine.validate_extraction_request({"task_type": "field_extraction", "fields": [
            {"name": f"field_{index}_" + "x" * (engine.MAX_FIELD_NAME_BYTES - 8),
             "question": "What is the requested field?", "value_type": "numeric" if index % 2 else "text"}
            for index in range(engine.MAX_FIELDS)
        ]})["fields"]
        originals = [ranked_chunk(f"chunk-{index}", "\n".join(f"Metric: {index}{number:03d}.00" for number in range(64)))
                     for index in range(engine.MAX_CHUNKS)]
        _, lookup = pipeline._evidence_spans(originals)
        schema = pipeline.response_schema(fields, lookup)
        self.assertTrue(all(len(field["name"]) == engine.MAX_FIELD_NAME_BYTES for field in fields))
        schema_bytes = json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.assertEqual(lm_module.MAX_INTENT_BYTES, 64 * 1024)
        self.assertLessEqual(len(schema_bytes), lm_module.MAX_INTENT_BYTES)

        def check_alternatives(node):
            if isinstance(node, dict):
                self.assertTrue({"if", "then", "dependencies"}.isdisjoint(node))
                if "anyOf" in node:
                    self.assertEqual(set(node), {"anyOf"})
                    for branch in node["anyOf"]:
                        self.assertEqual(branch["type"], "object")
                        self.assertFalse(branch["additionalProperties"])
                        self.assertEqual(set(branch["properties"]), set(branch["required"]))
                for nested in node.values():
                    check_alternatives(nested)
            elif isinstance(node, list):
                for nested in node:
                    check_alternatives(nested)

        check_alternatives(schema)
        output = {"task_type": "field_extraction", "fields": []}
        opener = Mock(return_value=Response({"choices": [{"message": {"content": json.dumps(output)}}]}))
        client = pipeline.IntentLMStudioClient(opener=opener)
        self.assertEqual(json.loads(client.generate_structured(
            [{"role": "user", "content": "Offline schema size check"}], response_schema=schema,
            schema_name="document_extract_v06g", max_output_tokens=1536,
        )), output)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(json.loads(opener.call_args.args[0].data)["response_format"]["json_schema"]["schema"], schema)

    def test_oversized_numeric_enum_schema_is_rejected_without_dropping_literals(self):
        originals = [ranked_chunk(f"chunk-{index}", " ".join(str(100000 + index * 2000 + number) for number in range(900)))
                     for index in range(engine.MAX_CHUNKS)]
        self.assertLessEqual(sum(len(item["text"].encode()) for item in originals), engine.MAX_TOTAL_CHUNK_BYTES)
        _, lookup = pipeline._evidence_spans(originals)
        with self.assertRaises(pipeline.PipelineError) as caught:
            pipeline.response_schema(field_request("measurement", "numeric")["fields"], lookup)
        self.assertEqual(caught.exception.code, "model_input_too_large")
        self.assertTrue(all(len(pipeline._numeric_literals(item["text"])) == 900 for item in originals))

    def test_exact_source_literals_still_obey_numeric_engine_bounds_without_schema_enforcement(self):
        for literal, supported in (
            ("1e255", True), ("1e-254", True), ("9" * engine.MAX_RAW_NUMERIC_BYTES, True),
            ("1e256", False), ("1e-255", False), ("9" * (engine.MAX_RAW_NUMERIC_BYTES + 1), False),
            ("1e1000000", False), ("NaN", False), ("Infinity", False), ("1_000", False),
        ):
            with self.subTest(literal=literal):
                original = "Measurement: " + literal + " units"
                result, _, client = self.run_request(adapter=CatalogAdapter(chunks=[chunk(document(), original)]),
                                                      client=StructuredClient(field_request("measurement", "numeric"),
                                                                              evidence_response(name="measurement", value=literal)))
                self.assertEqual(result["status"], "success" if supported else "invalid_evidence")
                self.assertEqual(result["success"], supported)
                self.assertEqual(len(client.calls), 2)

    def test_model_output_and_public_result_size_limits_remain_enforced(self):
        for stage in (0, 1):
            for extra in (0, 1):
                raw = json.dumps(field_request() if stage == 0 else evidence_response())
                raw += " " * (engine.MAX_MODEL_RESPONSE_BYTES - len(raw.encode()) + extra)
                outputs = [raw, evidence_response()] if stage == 0 else [field_request(), raw]
                result, _, client = self.run_request(client=StructuredClient(*outputs))
                self.assertEqual(result["status"], "malformed_model_output" if extra else "success")
                self.assertEqual(len(client.calls), stage + 1 if extra else 2)
        normal, _, _ = self.run_request()
        result_bytes = len(json.dumps(normal, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertEqual(engine.MAX_RESULT_BYTES, 96 * 1024)
        for extra, status in ((0, "success"), (1, "result_too_large")):
            with patch.object(engine, "MAX_RESULT_BYTES", result_bytes - extra):
                result, _, _ = self.run_request()
            self.assertEqual(result["status"], status)

    def test_mixed_extraction_preserves_numeric_literal_and_text_with_units(self):
        fields = [
            {"name": "measurement", "value_type": "numeric"},
            {"name": "duration_label", "value_type": "text"},
        ]
        for literal, normalized in (("120", "120"), ("45.50", "45.50"), ("-2.50e1", "-25.0"), ("+.5", "0.5")):
            with self.subTest(literal=literal):
                quote = f"Measurement: {literal} units."
                text_quote = "Duration label: 18 hours."
                response = evidence_response(name="measurement", value=literal)
                response["fields"].extend(evidence_response(
                    name="duration_label", value="18 hours",
                )["fields"])
                adapter = CatalogAdapter(chunks=[chunk(document(), quote + "\n" + text_quote)])
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
                self.assertEqual(result["fields"][0]["quote"], quote + "\n" + text_quote)
                self.assertEqual(result["fields"][1]["value"], "18 hours")
                self.assertEqual(result["fields"][1]["quote"], quote + "\n" + text_quote)
                self.assertFalse(result["execution_performed"])
                self.assertEqual(result["evidence_validation"], pipeline.EVIDENCE_DISCLAIMER)
                self.assertEqual(result["model_calls"], 2)
                self.assertEqual([call["max_output_tokens"] for call in client.calls], [768, 1536])

    def test_numeric_candidate_with_units_still_fails_closed_without_repair_or_retry(self):
        for raw in ("120 people", "18 hours"):
            with self.subTest(raw=raw):
                quote = f"Capacity: {raw}."
                response = evidence_response(name="capacity", value=raw)
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
            (field_request(), evidence_response(value=16), "malformed_model_output"),
            (field_request(), evidence_response(value="Invented visitor"), "invalid_evidence"),
            ({"task_type": "field_extraction", "fields": []}, None, "malformed_model_output"),
        ):
            with self.subTest(expected=expected):
                outputs = [first] if second is None else [first, second]
                result, _, client = self.run_request(client=StructuredClient(*outputs))
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["success"])
                self.assertLessEqual(len(client.calls), 2)

    def test_missing_and_ambiguous_engine_results_are_preserved(self):
        missing = {"task_type": "field_extraction", "fields": [{"name": "visitor", "candidates": []}]}
        ambiguous = evidence_response()
        second = copy.deepcopy(ambiguous["fields"][0]["candidates"][0])
        second.update({"value": "Rin"})
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
