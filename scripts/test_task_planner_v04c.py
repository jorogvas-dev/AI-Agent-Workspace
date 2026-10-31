import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

from scripts.test_task_planner_v04b import plan as base_plan

from scripts import knowledge_adapter_v04c as adapter_module
from scripts import task_planner_v04c as planner


FULL_PLAN_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "full_job_offer_plan_v04c.json"


class FakeResponse:
    def __init__(self, payload=b"", error=None):
        self.payload = payload
        self.error = error

    def __enter__(self):
        if self.error:
            raise self.error
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit):
        return self.payload


def valid_plan():
    plan = base_plan()
    plan["version"] = "0.4c"
    return plan


def knowledge_payload(text="Pay is 16 $ per hour", source="sample.pdf", location="Page 1"):
    return {
        "status": "success",
        "result_count": 1,
        "results": [{
            "text": text,
            "source": source,
            "folder": "knowledge",
            "location": location,
            "chunk_number": 1,
            "distance": 0.1,
            "chunk_id": "chunk-1",
        }],
    }


def full_job_offer_payload():
    return {
        "status": "success",
        "result_count": 3,
        "results": [
            {"text": "The offered wage is 16 $ per hour.", "source": "sample.pdf", "location": "Page 1", "chunk_id": "wage-1"},
            {"text": "Expected schedule is 32 hours per week.", "source": "sample.pdf", "location": "Page 2", "chunk_id": "hours-1"},
            {"text": "Housing cost 15 per day.", "source": "sample.pdf", "location": "Page 3", "chunk_id": "housing-1"},
        ],
    }


def full_payload_without_housing():
    payload = full_job_offer_payload()
    payload["results"] = payload["results"][:2]
    payload["result_count"] = 2
    return payload


def housing_refinement_payload():
    return {
        "status": "success",
        "result_count": 1,
        "results": [{
            "text": "Housing cost: $15.0\nHow often is rent due? Per day.",
            "source": "sample.pdf",
            "location": "Page 4",
            "chunk_id": "housing-refined-1",
        }],
    }


class AdapterTests(unittest.TestCase):
    def make_adapter(self, opener, token="isolated-test-token"):
        directory = tempfile.TemporaryDirectory()
        token_file = Path(directory.name) / "token"
        token_file.write_text(token, encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return adapter_module.create_isolated_adapter(token_file, opener=opener), token_file

    def test_authenticated_isolated_search_preserves_schema(self):
        payload = json.dumps(knowledge_payload()).encode("utf-8")
        opener = Mock(return_value=FakeResponse(payload))
        adapter, _ = self.make_adapter(opener)
        result = adapter.search("wage", 5)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, adapter_module.ISOLATED_ENDPOINT)
        self.assertEqual(request.get_header("Authorization"), "Bearer isolated-test-token")
        self.assertEqual(result["results"][0]["chunk_id"], "chunk-1")
        self.assertEqual(result["results"][0]["location"], "Page 1")

    def test_invalid_credentials_are_generic_and_secret_safe(self):
        error = urllib.error.HTTPError(adapter_module.ISOLATED_ENDPOINT, 401, "no", {}, None)
        opener = Mock(return_value=FakeResponse(error=error))
        adapter, _ = self.make_adapter(opener, token="do-not-disclose")
        with self.assertRaisesRegex(adapter_module.KnowledgeAdapterError, "authentication failed") as caught:
            adapter.search("wage", 5)
        self.assertNotIn("do-not-disclose", str(caught.exception))

    def test_http_failure_timeout_malformed_and_oversized_responses(self):
        cases = [
            urllib.error.HTTPError(adapter_module.ISOLATED_ENDPOINT, 500, "error", {}, None),
            socket.timeout(),
            FakeResponse(b"not-json"),
            FakeResponse(b"x" * (adapter_module.MAX_RESPONSE_BYTES + 1)),
        ]
        for failure in cases:
            with self.subTest(type=type(failure).__name__):
                opener = Mock(return_value=FakeResponse(error=failure) if not isinstance(failure, FakeResponse) else failure)
                adapter, _ = self.make_adapter(opener)
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.search("wage", 5)

    def test_response_schema_rejects_bad_source_evidence_and_count(self):
        bad_payloads = [
            knowledge_payload(text=""),
            knowledge_payload(source=""),
            {"status": "success", "result_count": 2, "results": []},
            {"status": "empty", "results": []},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                adapter, _ = self.make_adapter(Mock(return_value=FakeResponse(json.dumps(payload).encode())))
                with self.assertRaises(adapter_module.KnowledgeAdapterError):
                    adapter.search("wage", 5)

    def test_production_endpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                adapter_module.KnowledgeServiceAdapter(
                    Path(directory) / "token",
                    endpoint="http://knowledge-service:8002/search",
                )


class PlannerV04cTests(unittest.TestCase):
    def test_full_job_offer_plan_extracts_calculates_renders_and_verifies_in_memory(self):
        candidate = json.loads(FULL_PLAN_PATH.read_text(encoding="utf-8"))
        fake_adapter = Mock()
        fake_adapter.search.return_value = full_job_offer_payload()

        result = planner.run_mode("test", candidate, adapter=fake_adapter)

        self.assertTrue(result["success"])
        self.assertEqual(len(candidate["steps"]), 9)
        self.assertEqual(fake_adapter.search.call_count, 1)
        records = {record["id"]: record for record in result["records"]}
        self.assertEqual(records["extract_hourly_wage"]["fact"]["value"], "16.00")
        self.assertEqual(records["extract_weekly_hours"]["fact"]["value"], "32.00")
        self.assertEqual(records["extract_housing_per_day"]["fact"]["value"], "15.00")
        self.assertIn("evidence_excerpt", records["extract_hourly_wage"]["fact"])
        self.assertNotIn("evidence", records["extract_hourly_wage"]["fact"])
        self.assertEqual(records["calculate_weekly_gross"]["value"], "512.00")
        self.assertEqual(records["calculate_weekly_housing"]["value"], "105.00")
        self.assertEqual(records["calculate_difference"]["value"], "407.00")
        report = records["write_job_offer_report"]
        self.assertEqual(report["status"], "would_write")
        self.assertIn("source: sample.pdf", report["content"])
        self.assertIn("location: Page 1", report["content"])
        self.assertIn("chunk: wage-1", report["content"])
        self.assertIn("Weekly gross: $512.00", report["content"])
        self.assertEqual(records["verify_job_offer_report"]["sha256"], report["sha256"])

        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse((Path(directory) / "output").exists())

    def test_housing_refinement_handles_pdf_formatting_once(self):
        candidate = json.loads(FULL_PLAN_PATH.read_text(encoding="utf-8"))
        fake_adapter = Mock()
        fake_adapter.search.side_effect = [full_payload_without_housing(), housing_refinement_payload()]

        result = planner.run_mode("test", candidate, adapter=fake_adapter)

        self.assertTrue(result["success"])
        records = {record["id"]: record for record in result["records"]}
        self.assertEqual(records["extract_housing_per_day"]["fact"]["value"], "15.00")
        self.assertEqual(records["extract_housing_per_day"]["fact"]["location"], "Page 4")
        self.assertEqual(records["extract_housing_per_day"]["fact"]["chunk_id"], "housing-refined-1")
        self.assertEqual(fake_adapter.search.call_count, 2)
        self.assertEqual(fake_adapter.search.call_args_list[1].args[0], "sample.pdf housing cost 15.0 per day")

    def test_failed_housing_branch_does_not_block_independent_gross_but_blocks_report(self):
        candidate = json.loads(FULL_PLAN_PATH.read_text(encoding="utf-8"))
        fake_adapter = Mock()
        fake_adapter.search.side_effect = [full_payload_without_housing(), {"status": "success", "result_count": 0, "results": []}]

        result = planner.run_mode("test", candidate, adapter=fake_adapter)

        records = {record["id"]: record for record in result["records"]}
        self.assertFalse(result["success"])
        self.assertEqual(records["calculate_weekly_gross"]["status"], "simulated_success")
        self.assertEqual(records["calculate_weekly_housing"]["status"], "blocked")
        self.assertEqual(records["calculate_difference"]["status"], "blocked")
        self.assertEqual(records["write_job_offer_report"]["status"], "blocked")

    def test_validate_and_dry_run_modes_do_not_use_adapter_or_write(self):
        candidate = valid_plan()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validate = planner.run_mode("validate", candidate, workspace_root=root)
            dry_run = planner.run_mode("dry_run", candidate, workspace_root=root)
            self.assertTrue(validate["valid"])
            self.assertFalse(dry_run["execution_performed"])
            self.assertFalse((root / "output").exists())
            self.assertEqual(dry_run["records"][0]["status"], "not_executed")

            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(candidate), encoding="utf-8")
            self.assertEqual(planner.main(["validate", str(plan_path)]), 0)
            self.assertEqual(planner.main(["dry_run", str(plan_path)]), 1)

    def test_test_mode_connects_only_through_injected_adapter_and_does_not_write(self):
        candidate = valid_plan()
        fake_adapter = Mock()
        fake_adapter.search.return_value = knowledge_payload()
        result = planner.run_mode("test", candidate, adapter=fake_adapter)
        self.assertTrue(result["success"])
        self.assertEqual(result["records"][0]["status"], "simulated_success")
        self.assertEqual(result["records"][1]["fact"]["chunk_id"], "chunk-1")
        self.assertEqual(result["records"][1]["fact"]["location"], "Page 1")
        fake_adapter.search.assert_called_once()

    def test_dependency_ordering_and_source_mismatch_fail_explicitly(self):
        candidate = valid_plan()
        candidate["steps"][1]["depends_on"] = []
        with self.assertRaises(planner.PlanValidationError):
            planner.validate_plan(candidate)

        candidate = valid_plan()
        fake_adapter = Mock()
        fake_adapter.search.return_value = knowledge_payload(source="other.pdf")
        result = planner.run_mode("test", candidate, adapter=fake_adapter)
        self.assertFalse(result["success"])
        self.assertIn("no matching numeric evidence", result["records"][1]["error"])

    def test_ambiguous_and_conflicting_values_are_rejected(self):
        for payload in (
            knowledge_payload("Pay is 16 $ per hour and 18 $ per hour"),
            {
                "status": "success",
                "result_count": 2,
                "results": knowledge_payload()["results"] + knowledge_payload("Pay is 18 $ per hour")["results"],
            },
        ):
            with self.subTest(payload=payload):
                fake_adapter = Mock()
                fake_adapter.search.return_value = payload
                result = planner.run_mode("test", valid_plan(), adapter=fake_adapter)
                self.assertFalse(result["success"])
                self.assertEqual(result["records"][1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
