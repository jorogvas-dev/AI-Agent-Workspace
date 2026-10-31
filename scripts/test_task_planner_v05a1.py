import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock

from scripts import task_planner_v05a1 as pipeline


def valid_plan():
    return {
        "version": "0.5a",
        "variables": {"amount": {"type": "number", "unit": "USD"}, "total": {"type": "number", "unit": "USD"}},
        "steps": [
            {"id": "calculate_total", "operation": "calculate", "args": {"operation": "add", "operands": [10, 5], "save_as": "total"}},
            {"id": "write_report", "operation": "write_report", "depends_on": ["calculate_total"], "args": {"path": "output/total.md", "content": "Total: ${total}"}},
            {"id": "verify_report", "operation": "verify_file", "depends_on": ["write_report"], "args": {"path": "output/total.md", "reference": "write_report"}},
        ],
    }


class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def generate(self, messages):
        self.calls.append(messages)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return output


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit):
        return self.body


class PipelineV05a1Tests(unittest.TestCase):
    def test_valid_first_generation_plan(self):
        client = FakeClient([json.dumps(valid_plan())])
        result = pipeline.generate_and_validate("Calculate a budget and preview a report.", client)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertFalse(result["execution_performed"])
        self.assertEqual(len(client.calls), 1)

    def test_valid_after_one_structured_correction(self):
        invalid = valid_plan()
        invalid["steps"][1]["depends_on"] = []
        client = FakeClient([json.dumps(invalid), json.dumps(valid_plan())])
        result = pipeline.generate_and_validate("Calculate a budget and preview a report.", client)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["attempts"][0]["errors"][0]["code"], "invalid_dependencies")

    def test_identical_malformed_plan_is_not_revalidated_or_retried(self):
        malformed = '{"version":"0.5a","steps": [1,].repeat(5)}'
        client = FakeClient([malformed, malformed])
        result = pipeline.generate_and_validate("Calculate a budget and preview a report.", client)
        self.assertFalse(result["valid"])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["attempts"][1]["errors"][0]["code"], "identical_plan")

    def test_invalid_escaping_and_javascript_are_rejected(self):
        for content in ('{"version":"0.5a", "x":"bad\\q"}', '{"version":"0.5a", "x":"a".repeat(5)}'):
            client = FakeClient([content])
            result = pipeline.generate_and_validate("Calculate a budget and preview a report.", client)
            self.assertFalse(result["valid"])
            self.assertEqual(len(client.calls), 2)

    def test_structurally_valid_but_incomplete_plan_is_not_success(self):
        incomplete = valid_plan()
        incomplete["steps"][1]["args"]["content"] = "Budget total: ${total}"
        client = FakeClient([json.dumps(valid_plan()), json.dumps(incomplete)])
        result = pipeline.generate_and_validate("Analyze this job offer with wage, weekly hours, and housing.", client)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "incomplete_task")

    def test_unsupported_operation_is_rejected(self):
        plan = valid_plan()
        plan["steps"][0]["operation"] = "run_command"
        client = FakeClient([json.dumps(plan)])
        result = pipeline.generate_and_validate("Calculate a budget and preview a report.", client)
        self.assertFalse(result["valid"])
        self.assertEqual(result["attempts"][0]["errors"][0]["code"], "unsupported_operation")

    def test_lm_studio_unavailable_refuses_without_retrying_transport_failure(self):
        client = FakeClient([pipeline.LMStudioUnavailable("LM Studio is unavailable")])
        result = pipeline.generate_and_validate("Calculate a budget and preview a report.", client)
        self.assertFalse(result["valid"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["errors"][0]["code"], "lm_studio_unavailable")

    def test_lm_studio_client_requests_structured_schema(self):
        response = Response(json.dumps({"choices": [{"message": {"content": json.dumps(valid_plan())}}]}).encode())
        opener = Mock(return_value=response)
        client = pipeline.LMStudioClient(opener=opener)
        content = client.generate([{"role": "user", "content": "make a budget plan"}])
        self.assertIn('"version": "0.5a"', content)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, pipeline.LM_STUDIO_ENDPOINT)
        sent = json.loads(request.data.decode())
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertTrue(sent["response_format"]["json_schema"]["strict"])

    def test_lm_studio_structured_output_unsupported_is_explicit(self):
        error = urllib.error.HTTPError(pipeline.LM_STUDIO_ENDPOINT, 400, "unsupported", {}, None)
        client = pipeline.LMStudioClient(opener=Mock(side_effect=error))
        with self.assertRaises(pipeline.StructuredOutputUnsupported):
            client.generate([{"role": "user", "content": "make a budget plan"}])

    def test_lm_studio_truncation_is_rejected(self):
        response = Response(json.dumps({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}).encode())
        client = pipeline.LMStudioClient(opener=Mock(return_value=response))
        with self.assertRaises(pipeline.ModelOutputError):
            client.generate([{"role": "user", "content": "make a budget plan"}])

    def test_refusal_and_truncated_output_use_at_most_two_generations(self):
        client = FakeClient([pipeline.ModelOutputError("model returned empty plan content"), pipeline.ModelOutputError("model output is incomplete")])
        result = pipeline.generate_and_validate("Calculate a budget and preview a report.", client)
        self.assertFalse(result["valid"])
        self.assertEqual(len(client.calls), 2)

    def test_two_attempt_limit_is_enforced(self):
        invalid = valid_plan()
        invalid["steps"][1]["depends_on"] = []
        client = FakeClient([json.dumps(invalid), json.dumps(invalid), json.dumps(valid_plan())])
        result = pipeline.generate_and_validate("Calculate a budget and preview a report.", client)
        self.assertFalse(result["valid"])
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
