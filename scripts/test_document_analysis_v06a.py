import json
import unittest
from decimal import Decimal

from scripts import document_analysis_v06a as analysis


class FakeCallbacks:
    def __init__(self, request_output, extraction_output, chunks):
        self.request_output = request_output
        self.extraction_output = extraction_output
        self.chunks = chunks
        self.llm_calls = []
        self.retrieval_calls = []

    def retrieve(self, source, query, limit):
        self.retrieval_calls.append((source, query, limit))
        return {"status": "success", "result_count": len(self.chunks), "results": self.chunks}

    def llm(self, stage, payload):
        self.llm_calls.append((stage, payload))
        if stage == "request":
            return self.request_output
        if stage == "extract":
            return self.extraction_output
        raise AssertionError(f"unexpected stage: {stage}")


def request_for(*fields):
    return {"task_type": "field_extraction", "fields": list(fields)}


def field(name, question, value_type):
    return {"name": name, "question": question, "value_type": value_type}


def candidate(value, source, chunk_id, location, quote):
    return {"value": value, "source": source, "chunk_id": chunk_id, "location": location, "quote": quote}


def response_for(*fields):
    return {"task_type": "field_extraction", "fields": list(fields)}


def found(name, value, source, chunk_id, location, quote):
    return {"name": name, "status": "found", "candidates": [candidate(value, source, chunk_id, location, quote)]}


class DocumentAnalysisV06aTests(unittest.TestCase):
    def test_partial_numeric_literals_and_lost_signs_are_rejected(self):
        request_output = request_for(field("measurement", "What is the measurement?", "numeric"))
        for value, quote in (("1", "Measurement: 16.00 units."), ("1", "Measurement: -1 units."), ("1", "Measurement: 1e10 units.")):
            with self.subTest(value=value, quote=quote):
                response_output = response_for(found("measurement", value, "measurements.txt", "measurement-1", "line 1", quote))
                callbacks = FakeCallbacks(
                    request_output,
                    response_output,
                    [{"chunk_id": "measurement-1", "source": "measurements.txt", "location": "line 1", "text": quote}],
                )
                result = analysis.analyze_document("measurements.txt", "Extract the measurement.", callbacks.retrieve, callbacks.llm)
                self.assertEqual(result["status"], "invalid_evidence")
                self.assertFalse(result["success"])

    def test_numeric_token_and_normalized_value_limits_are_fail_closed(self):
        request_output = request_for(field("measurement", "What is the measurement?", "numeric"))
        raw_too_long = "1" * (analysis.MAX_RAW_NUMERIC_BYTES + 1)
        response_output = response_for(found("measurement", raw_too_long, "measurements.txt", "measurement-1", "line 1", raw_too_long))
        callbacks = FakeCallbacks(
            request_output,
            response_output,
            [{"chunk_id": "measurement-1", "source": "measurements.txt", "location": "line 1", "text": raw_too_long}],
        )
        result = analysis.analyze_document("measurements.txt", "Extract the measurement.", callbacks.retrieve, callbacks.llm)
        self.assertEqual(result["status"], "invalid_evidence")

        huge_exponent = "1e100000"
        response_output = response_for(found("measurement", huge_exponent, "measurements.txt", "measurement-1", "line 1", huge_exponent))
        callbacks = FakeCallbacks(
            request_output,
            response_output,
            [{"chunk_id": "measurement-1", "source": "measurements.txt", "location": "line 1", "text": huge_exponent}],
        )
        result = analysis.analyze_document("measurements.txt", "Extract the measurement.", callbacks.retrieve, callbacks.llm)
        self.assertEqual(result["status"], "invalid_evidence")
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), analysis.MAX_RESULT_BYTES)

    def test_signed_decimal_and_scientific_numeric_literals_are_accepted(self):
        request_output = request_for(
            field("signed_value", "What is the signed value?", "numeric"),
            field("scientific_value", "What is the scientific value?", "numeric"),
        )
        response_output = response_for(
            found("signed_value", "-16.00", "numbers.txt", "numbers-1", "line 2", "Signed value: -16.00"),
            found("scientific_value", "1e3", "numbers.txt", "numbers-2", "line 3", "Scientific value: 1e3"),
        )
        callbacks = FakeCallbacks(
            request_output,
            response_output,
            [
                {"chunk_id": "numbers-1", "source": "numbers.txt", "location": "line 2", "text": "Signed value: -16.00"},
                {"chunk_id": "numbers-2", "source": "numbers.txt", "location": "line 3", "text": "Scientific value: 1e3"},
            ],
        )
        result = analysis.analyze_document("numbers.txt", "Extract both values.", callbacks.retrieve, callbacks.llm)
        self.assertTrue(result["success"])
        self.assertEqual(result["fields"][0]["value"], "-16.00")
        self.assertEqual(result["fields"][1]["value"], "1000")

    def test_result_serialization_overflow_returns_small_structured_error(self):
        request_output = request_for(field("title", "What is the title?", "text"))
        response_output = response_for(found("title", "Ready", "doc.txt", "doc-1", "line 1", "Title: Ready"))
        callbacks = FakeCallbacks(
            request_output,
            response_output,
            [{"chunk_id": "doc-1", "source": "doc.txt", "location": "line 1", "text": "Title: Ready"}],
        )
        original_limit = analysis.MAX_RESULT_BYTES
        analysis.MAX_RESULT_BYTES = 256
        try:
            result = analysis.analyze_document("doc.txt", "Extract the title.", callbacks.retrieve, callbacks.llm)
        finally:
            analysis.MAX_RESULT_BYTES = original_limit
        self.assertFalse(result["valid"])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "result_too_large")
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 1024)

    def test_unrelated_documents_support_arbitrary_sources_and_text_numeric_fields(self):
        alpha = analysis.DocumentIdentity("alpha-record.txt")
        alpha_fields = request_for(
            field("contact_name", "What is the contact name?", "text"),
            field("balance_due", "What is the balance due?", "numeric"),
        )
        alpha_chunks = [{
            "chunk_id": "alpha-1",
            "source": "alpha-record.txt",
            "location": "line 4",
            "text": "Contact name: Ada Lovelace. Balance due: 123.450 USD.",
        }]
        alpha_response = response_for(
            found("contact_name", "Ada Lovelace", "alpha-record.txt", "alpha-1", "line 4", "Contact name: Ada Lovelace"),
            found("balance_due", "123.450", "alpha-record.txt", "alpha-1", "line 4", "Balance due: 123.450 USD"),
        )
        alpha_callbacks = FakeCallbacks(alpha_fields, alpha_response, alpha_chunks)
        result = analysis.analyze_document(alpha, "Extract the contact and balance.", alpha_callbacks.retrieve, alpha_callbacks.llm)

        self.assertTrue(result["success"])
        self.assertEqual(result["source"], "alpha-record.txt")
        self.assertEqual(result["fields"][0]["value"], "Ada Lovelace")
        self.assertEqual(result["fields"][1]["value"], "123.450")
        self.assertEqual(Decimal(result["fields"][1]["value"]), Decimal("123.450"))
        self.assertEqual(alpha_callbacks.retrieval_calls[0][0], alpha)
        self.assertEqual(alpha_callbacks.retrieval_calls[0][2], analysis.MAX_CHUNKS)
        self.assertEqual([call[0] for call in alpha_callbacks.llm_calls], ["request", "extract"])
        self.assertEqual(alpha_callbacks.llm_calls[1][1]["chunks"], alpha_chunks)

        beta = analysis.DocumentIdentity("beta-notes.md")
        beta_fields = request_for(field("status_label", "What status label is shown?", "text"))
        beta_chunks = [{"chunk_id": "beta-1", "source": "beta-notes.md", "location": "section A", "text": "Status label: Ready"}]
        beta_response = response_for(found("status_label", "Ready", "beta-notes.md", "beta-1", "section A", "Status label: Ready"))
        beta_callbacks = FakeCallbacks(beta_fields, beta_response, beta_chunks)
        beta_result = analysis.analyze_document(beta, "Extract the status label.", beta_callbacks.retrieve, beta_callbacks.llm)
        self.assertTrue(beta_result["success"])
        self.assertEqual(beta_result["fields"][0]["value"], "Ready")
        self.assertEqual(beta_result["source"], "beta-notes.md")

    def test_request_schema_rejects_regex_formula_and_unknown_fields(self):
        valid = request_for(field("total", "What is the total?", "numeric"))
        self.assertEqual(analysis.validate_extraction_request(valid)["fields"][0]["name"], "total")
        for invalid in (
            {"task_type": "field_extraction", "fields": [{"name": "total", "question": "x", "value_type": "numeric", "regex": "(.*)"}]},
            {"task_type": "field_extraction", "fields": [{"name": "total", "question": "x", "value_type": "numeric", "formula": "a+b"}]},
            {"task_type": "field_extraction", "fields": [{"name": "total", "question": "x", "value_type": "numeric", "command": "python"}]},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(analysis.InvalidExtractionRequestError):
                    analysis.validate_extraction_request(invalid)

    def test_unsupported_task_type_is_explicit_and_no_retrieval_occurs(self):
        callbacks = FakeCallbacks({"task_type": "calculation", "fields": []}, {}, [])
        result = analysis.analyze_document(
            "unrelated-source.bin",
            "Calculate a result from the document.",
            callbacks.retrieve,
            callbacks.llm,
        )
        self.assertFalse(result["valid"])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "unsupported_task_type")
        self.assertEqual(callbacks.retrieval_calls, [])
        self.assertEqual(result["model_calls"], 1)

    def test_wrong_source_contamination_is_rejected_before_extraction_model(self):
        request_output = request_for(field("reference", "What is the reference?", "text"))
        callbacks = FakeCallbacks(
            request_output,
            {},
            [{"chunk_id": "wrong-1", "source": "other-document.txt", "location": "page 1", "text": "Reference: forged"}],
        )
        result = analysis.analyze_document("target-document.txt", "Extract the reference.", callbacks.retrieve, callbacks.llm)
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "source_mismatch")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual([stage for stage, _payload in callbacks.llm_calls], ["request"])

    def test_fabricated_quote_is_invalid_evidence(self):
        request_output = request_for(field("amount", "What is the amount?", "numeric"))
        response_output = response_for(found("amount", "999", "ledger-a.txt", "ledger-1", "page 1", "Amount: 999"))
        callbacks = FakeCallbacks(
            request_output,
            response_output,
            [{"chunk_id": "ledger-1", "source": "ledger-a.txt", "location": "page 1", "text": "Amount: 12.50"}],
        )
        result = analysis.analyze_document("ledger-a.txt", "Extract the amount.", callbacks.retrieve, callbacks.llm)
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "invalid_evidence")
        self.assertEqual(result["fields"], [{"name": "amount", "value_type": "numeric", "status": "invalid_evidence"}])

    def test_value_missing_from_quote_is_invalid_evidence(self):
        request_output = request_for(field("code", "What is the code?", "text"))
        response_output = response_for(found("code", "ABC", "codes.txt", "codes-1", "line 2", "Code: XYZ"))
        callbacks = FakeCallbacks(
            request_output,
            response_output,
            [{"chunk_id": "codes-1", "source": "codes.txt", "location": "line 2", "text": "Code: XYZ"}],
        )
        result = analysis.analyze_document("codes.txt", "Extract the code.", callbacks.retrieve, callbacks.llm)
        self.assertEqual(result["status"], "invalid_evidence")
        self.assertFalse(result["success"])

    def test_missing_field_is_explicit_and_does_not_fabricate(self):
        request_output = request_for(
            field("title", "What is the title?", "text"),
            field("amount", "What is the amount?", "numeric"),
        )
        response_output = response_for({"name": "title", "status": "missing", "candidates": []})
        callbacks = FakeCallbacks(
            request_output,
            response_output,
            [{"chunk_id": "memo-1", "source": "memo.txt", "location": "page 1", "text": "No requested facts here."}],
        )
        result = analysis.analyze_document("memo.txt", "Extract title and amount.", callbacks.retrieve, callbacks.llm)
        self.assertTrue(result["valid"])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "missing")
        self.assertEqual([item["status"] for item in result["fields"]], ["missing", "missing"])
        self.assertEqual({item["code"] for item in result["errors"]}, {"missing_field"})

    def test_conflicting_candidates_are_explicitly_ambiguous(self):
        request_output = request_for(field("amount", "What is the amount?", "numeric"))
        response_output = response_for({
            "name": "amount",
            "status": "ambiguous",
            "candidates": [
                candidate("12.50", "statement.csv", "statement-1", "row 2", "Amount: 12.50"),
                candidate("13.50", "statement.csv", "statement-2", "row 8", "Amount: 13.50"),
            ],
        })
        callbacks = FakeCallbacks(
            request_output,
            response_output,
            [
                {"chunk_id": "statement-1", "source": "statement.csv", "location": "row 2", "text": "Amount: 12.50"},
                {"chunk_id": "statement-2", "source": "statement.csv", "location": "row 8", "text": "Amount: 13.50"},
            ],
        )
        result = analysis.analyze_document("statement.csv", "Extract the amount.", callbacks.retrieve, callbacks.llm)
        self.assertTrue(result["valid"])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["fields"][0]["candidates"]), 2)
        self.assertEqual({item["code"] for item in result["errors"]}, {"ambiguous_field"})

    def test_malformed_model_output_is_structured_and_bounded(self):
        callbacks = FakeCallbacks("{not-json", {}, [])
        result = analysis.analyze_document("notes.txt", "Extract a title.", callbacks.retrieve, callbacks.llm)
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "malformed_model_output")
        self.assertEqual(result["model_calls"], 1)

        request_output = request_for(field("title", "What is the title?", "text"))
        callbacks = FakeCallbacks(request_output, {"task_type": "field_extraction", "fields": []}, [])
        result = analysis.analyze_document("notes.txt", "Extract a title.", callbacks.retrieve, callbacks.llm)
        self.assertEqual(result["status"], "missing")
        self.assertTrue(result["valid"])

    def test_limits_reject_oversized_requests_and_retrieval(self):
        callbacks = FakeCallbacks({}, {}, [])
        oversized_request = "x" * (analysis.MAX_REQUEST_BYTES + 1)
        result = analysis.analyze_document("doc.txt", oversized_request, callbacks.retrieve, callbacks.llm)
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(callbacks.llm_calls, [])

        request_output = request_for(field("title", "What is the title?", "text"))
        callbacks = FakeCallbacks(
            request_output,
            {},
            [{"chunk_id": str(index), "source": "doc.txt", "location": "page 1", "text": "Title: X"} for index in range(analysis.MAX_CHUNKS + 1)],
        )
        result = analysis.analyze_document("doc.txt", "Extract a title.", callbacks.retrieve, callbacks.llm)
        self.assertEqual(result["status"], "retrieval_error")


if __name__ == "__main__":
    unittest.main()
