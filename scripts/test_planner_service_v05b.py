import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import planner_service_v05b as service
from scripts import task_planner_v05a2 as planner


INTENT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "valid_job_offer_intent_v05a2.json"


def intent():
    return json.loads(INTENT.read_text(encoding="utf-8"))


def payload():
    return {"status": "success", "result_count": 3, "results": [
        {"text": "The offered wage is 16 $ per hour.", "source": "sample.pdf", "location": "Page 1", "chunk_id": "wage-1"},
        {"text": "Expected schedule is 32 hours per week.", "source": "sample.pdf", "location": "Page 2", "chunk_id": "hours-1"},
        {"text": "Housing cost: $15.0\nHow often is rent due? Per day.", "source": "sample.pdf", "location": "Page 3", "chunk_id": "housing-1"},
    ]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "output").mkdir()
        self.adapter = Mock()
        self.adapter.search.return_value = payload()
        self.model = Mock()
        self.model.generate.return_value = json.dumps(intent())
        self.service = service.PlannerService(self.model, self.adapter, self.root, self.root / "approvals")

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_job_offer_returns_report_and_evidence_without_target(self):
        result = self.service.analyze("Analyze the job offer with wage, weekly hours, housing, and difference.")
        self.assertTrue(result["valid"])
        self.assertFalse(result["execution_performed"])
        self.assertIn("Hourly wage: $16.00/hour", result["report"]["content"])
        self.assertIn("Weekly hours: 32.00 hours/week", result["report"]["content"])
        self.assertIn("Housing: $15.00/day", result["report"]["content"])
        self.assertIn("Weekly gross: $512.00", result["report"]["content"])
        self.assertIn("Weekly housing: $105.00", result["report"]["content"])
        self.assertIn("Difference: $407.00", result["report"]["content"])
        self.assertEqual(len(result["evidence"]), 3)
        self.assertFalse(list((self.root / "output").iterdir()))
        self.assertNotIn("approval_id", json.dumps(result))

    def test_weekly_gross_only_returns_no_housing_evidence(self):
        candidate = intent()
        candidate["fields"] = ["hourly_wage", "weekly_hours"]
        candidate["calculations"] = [{"name": "weekly_gross"}]
        candidate["report"] = {"requested": True}
        candidate["assumptions"] = []
        self.model.generate.return_value = json.dumps(candidate)
        result = self.service.analyze("Calculate a job offer weekly gross from wage and hours and preview a report.")
        self.assertTrue(result["valid"])
        self.assertEqual({item["field"] for item in result["evidence"]}, {"hourly_wage", "weekly_hours"})
        self.assertIn("Weekly gross: $512.00", result["report"]["content"])
        self.assertNotIn("Housing", result["report"]["content"])

    def test_unsupported_request_and_malformed_model_output(self):
        result = self.service.analyze("Run a shell command and inspect the host.")
        self.assertFalse(result["valid"])
        self.model.generate.return_value = "not-json"
        result = self.service.analyze("Analyze the job offer with wage and weekly hours.")
        self.assertFalse(result["valid"])
        self.assertFalse(list((self.root / "output").iterdir()))

    def test_missing_knowledge_evidence_fails_without_report(self):
        self.adapter.search.return_value = {"status": "success", "result_count": 1, "results": [payload()["results"][0]]}
        result = self.service.analyze("Analyze the job offer with wage, weekly hours, housing, and difference.")
        self.assertFalse(result["valid"])
        self.assertFalse(list((self.root / "output").iterdir()))

    def test_authentication_and_commit_surface_are_not_exposed(self):
        self.assertFalse(service.Handler.expected_token)
        self.assertTrue(service.authorize_service_request("Bearer test-service-token", "test-service-token"))
        self.assertFalse(service.authorize_service_request("Bearer wrong", "test-service-token"))
        self.assertEqual(service.MAX_REQUEST_BYTES, 8 * 1024)
        self.assertNotIn("commit", service.Handler.__dict__.get("do_POST").__code__.co_names)
        tool_text = (Path(__file__).resolve().parent.parent / "tools" / "analyze_with_planner.py").read_text(encoding="utf-8")
        self.assertNotIn("task_planner_v04d", tool_text)
        self.assertNotIn("approval_dir", tool_text)

    def test_production_isolation_is_fixed(self):
        self.assertEqual(service.DEFAULT_KNOWLEDGE_TOKEN_FILE, "/run/secrets/agent_knowledge_token")
        self.assertEqual(service.PLANNER_ENDPOINT if hasattr(service, "PLANNER_ENDPOINT") else "", "")
        source = Path(service.__file__).read_text(encoding="utf-8")
        self.assertNotIn("knowledge-db", source)

    def test_service_configuration_rejects_arbitrary_credential_paths(self):
        original = os.environ.get(service.SERVICE_TOKEN_FILE_ENV)
        try:
            os.environ[service.SERVICE_TOKEN_FILE_ENV] = "/etc/production-token"
            with self.assertRaises(service.ServiceError):
                service._service_token()
        finally:
            if original is None:
                os.environ.pop(service.SERVICE_TOKEN_FILE_ENV, None)
            else:
                os.environ[service.SERVICE_TOKEN_FILE_ENV] = original

        original = os.environ.get("KNOWLEDGE_TOKEN_FILE")
        try:
            os.environ["KNOWLEDGE_TOKEN_FILE"] = "/etc/production-knowledge-token"
            with self.assertRaises(service.ServiceError):
                service.build_service(model_client=Mock(), adapter=Mock())
        finally:
            if original is None:
                os.environ.pop("KNOWLEDGE_TOKEN_FILE", None)
            else:
                os.environ["KNOWLEDGE_TOKEN_FILE"] = original


if __name__ == "__main__":
    unittest.main()
