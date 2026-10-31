import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


SCRIPT = Path(__file__).with_name("job_offer_workflow_v03.py")
SPEC = importlib.util.spec_from_file_location("job_offer_workflow_v03", SCRIPT)
WORKFLOW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WORKFLOW
SPEC.loader.exec_module(WORKFLOW)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit):
        return json.dumps(self.payload).encode("utf-8")


class JobOfferWorkflowTests(unittest.TestCase):
    def test_fetch_knowledge_sends_authenticated_request_and_validates_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "token"
            secret.write_text("test-secret", encoding="utf-8")
            request_capture = {}

            def open_request(request, timeout):
                request_capture["url"] = request.full_url
                request_capture["auth"] = request.get_header("Authorization")
                request_capture["timeout"] = timeout
                return FakeResponse({"status": "success", "results": []})

            with patch.object(WORKFLOW, "SECRET_FILE", secret), patch.object(
                WORKFLOW.urllib.request, "urlopen", side_effect=open_request
            ):
                result = WORKFLOW.fetch_knowledge("wage", 5)

        self.assertEqual(result["status"], "success")
        self.assertEqual(request_capture["url"], WORKFLOW.KNOWLEDGE_SEARCH)
        self.assertEqual(request_capture["auth"], "Bearer test-secret")
        self.assertEqual(request_capture["timeout"], 15)
        self.assertNotIn("test-secret", json.dumps(result))

    def test_fetch_knowledge_hides_http_error_details(self):
        error = HTTPError(
            WORKFLOW.KNOWLEDGE_SEARCH,
            500,
            "internal",
            {},
            None,
        )

        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "token"
            secret.write_text("test-secret", encoding="utf-8")
            with patch.object(WORKFLOW, "SECRET_FILE", secret), patch.object(
                WORKFLOW.urllib.request, "urlopen", side_effect=error
            ):
                with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                    WORKFLOW.fetch_knowledge("wage", 5)

    def test_structured_result_verifies_output_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            report = output / "report.md"
            report.write_text("weekly gross earnings: $512.00\n", encoding="utf-8")
            plan = {"steps": [{"type": "verify_file", "path": "output/report.md"}]}
            state = {
                "status": "completed",
                "steps": [{"number": 1, "type": "verify_file", "status": "completed"}],
            }

            with patch.object(WORKFLOW, "controller_safe_target", return_value=report):
                result = WORKFLOW._structured_result(plan, state, root, "secret")
            expected_hash = hashlib.sha256(report.read_bytes()).hexdigest()

        self.assertTrue(result["success"])
        self.assertEqual(result["verified_output_path"], "/workspace/output/report.md")
        self.assertEqual(
            result["file_sha256"],
            expected_hash,
        )
        self.assertEqual(result["error"], None)

    def test_controller_path_validation_rejects_escape(self):
        with patch.object(
            WORKFLOW,
            "CONTROLLER",
            SCRIPT.with_name("task_controller_v02.py"),
        ):
            controller = WORKFLOW.load_controller()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            with self.assertRaises(ValueError):
                controller.safe_target(root, "output/../secret.txt")

    def test_fetch_knowledge_requires_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(WORKFLOW, "SECRET_FILE", Path(directory) / "missing-token"):
                with self.assertRaisesRegex(RuntimeError, "credential mount missing"):
                    WORKFLOW.fetch_knowledge("facts", 3)

    def test_controller_calculates_weekly_values(self):
        with patch.object(
            WORKFLOW,
            "CONTROLLER",
            SCRIPT.with_name("task_controller_v02.py"),
        ):
            controller = WORKFLOW.load_controller()
        values = {"hourly_wage": 16, "weekly_hours": 32}
        controller.calculate(
            {
                "name": "weekly_gross",
                "operation": "multiply",
                "operands": ["hourly_wage", "weekly_hours"],
                "save_as": "weekly_gross",
            },
            values,
        )
        self.assertEqual(values["weekly_gross"], "512.00")


if __name__ == "__main__":
    unittest.main()
