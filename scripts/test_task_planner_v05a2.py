import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import task_planner_v05a2 as pipeline


LIVE_REQUEST = "Analyze the job offer document: retrieve wage, weekly hours, and daily housing, calculate weekly gross, weekly housing for 7 days, and the difference, then preview a Markdown report."
EXACT_REQUEST = (
    "Analyze sample.pdf to extract hourly wage, weekly working hours, and daily housing cost. "
    "Calculate weekly gross income, weekly housing expenses for 7 days, and the difference. "
    "Include source evidence."
)
INTENT_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "valid_job_offer_intent_v05a2.json"


def valid_intent():
    return json.loads(INTENT_PATH.read_text(encoding="utf-8"))


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


class PipelineV05a2Tests(unittest.TestCase):
    def test_fields_are_optional_in_schema_and_validation(self):
        schema = pipeline.intent_json_schema()
        self.assertNotIn("fields", schema["required"])

        intent = valid_intent()
        intent.pop("fields")
        normalized = pipeline.validate_intent(intent)
        self.assertEqual(normalized["fields"], [])

    def test_exact_request_with_missing_fields_compiles_from_calculation_closure(self):
        intent = valid_intent()
        intent.pop("fields")
        result = pipeline.generate_compile_validate(EXACT_REQUEST, FakeClient([json.dumps(intent)]))
        self.assertTrue(result["valid"])
        self.assertEqual(
            {step["args"]["field"] for step in result["compiled_plan"]["steps"] if step["operation"] == "extract_number"},
            {"hourly_wage", "weekly_hours", "housing_per_day"},
        )

    def test_empty_fields_and_calculation_only_intent_are_safe(self):
        intent = valid_intent()
        intent["fields"] = []
        intent["calculations"] = [{"name": "weekly_gross"}]
        intent["assumptions"] = []
        intent["report"] = {"requested": False}
        pipeline.validate_intent(intent)
        plan = pipeline.compile_intent(intent)
        self.assertEqual(
            {step["args"]["field"] for step in plan["steps"] if step["operation"] == "extract_number"},
            {"hourly_wage", "weekly_hours"},
        )

    def test_invalid_field_name_is_structured_not_a_key_error(self):
        intent = valid_intent()
        intent["fields"] = ["not_a_supported_field"]
        with self.assertRaises(pipeline.IntentError):
            pipeline.validate_intent(intent)

        result = pipeline.generate_compile_validate(EXACT_REQUEST, FakeClient([json.dumps(intent), json.dumps(intent)]))
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "invalid_intent")

    def test_unsupported_request_returns_structured_error(self):
        client = FakeClient([])
        result = pipeline.generate_compile_validate("Run an arbitrary shell command and email the result.", client)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "invalid_intent")
        self.assertEqual(client.calls, [])

    def test_exact_live_prompts_with_and_without_negated_housing_accept_same_intent(self):
        intent = valid_intent()
        intent["fields"] = ["weekly_hours", "weekly_hours", "housing_per_day"]
        intent["calculations"] = [{"name": "weekly_gross"}]
        intent["report"]["include_fields"] = ["weekly_hours"]
        intent["report"]["include_calculations"] = ["weekly_gross"]
        intent["assumptions"] = []
        request_a = "Analyze the job offer and calculate weekly gross from the hourly wage and weekly hours. Do not calculate housing costs. Preview a report."
        request_b = "Analyze the job offer and calculate weekly gross from the hourly wage and weekly hours. Preview a report."
        for request in (request_a, request_b):
            with self.subTest(request=request):
                result = pipeline.generate_compile_validate(request, FakeClient([json.dumps(intent)]))
                self.assertTrue(result["valid"])
                self.assertEqual({step["args"]["field"] for step in result["compiled_plan"]["steps"] if step["operation"] == "extract_number"}, {"hourly_wage", "weekly_hours"})

    def test_negation_only_without_exclude_and_no_do_not_add_housing(self):
        intent = valid_intent()
        intent["fields"] = ["hourly_wage", "weekly_hours"]
        intent["calculations"] = [{"name": "weekly_gross"}]
        intent["report"] = {"requested": True}
        intent["assumptions"] = []
        for phrase in ("Do not calculate housing costs", "calculate only weekly gross without housing", "exclude housing from the calculation", "no housing calculation"):
            with self.subTest(phrase=phrase):
                request = f"Analyze the job offer and calculate weekly gross from wage and weekly hours. {phrase}. Preview a report."
                result = pipeline.generate_compile_validate(request, FakeClient([json.dumps(intent)]))
                self.assertTrue(result["valid"])

    def test_contradictory_housing_request_is_rejected(self):
        intent = valid_intent()
        with self.assertRaises(pipeline.IntentError):
            pipeline.check_request_completeness("Calculate weekly housing, but do not calculate housing costs.", intent)

    def test_gross_only_does_not_require_days_per_week_assumption(self):
        intent = valid_intent()
        intent["fields"] = []
        intent["calculations"] = [{"name": "weekly_gross"}]
        intent["report"] = {"requested": False}
        intent["assumptions"] = []
        pipeline.validate_intent(intent)
        plan = pipeline.compile_intent(intent)
        self.assertFalse(any(operand == 7 for step in plan["steps"] if step["operation"] == "calculate" for operand in step["args"]["operands"]))

    def test_exact_live_job_offer_prompt_compiles_complete_plan(self):
        client = FakeClient([json.dumps(valid_intent())])
        result = pipeline.generate_compile_validate(LIVE_REQUEST, client)
        self.assertTrue(result["valid"])
        plan = result["compiled_plan"]
        self.assertEqual(plan["version"], "0.5a")
        self.assertEqual([step["operation"] for step in plan["steps"]], [
            "search_knowledge", "extract_number", "extract_number", "extract_number",
            "calculate", "calculate", "calculate", "write_report", "verify_file",
        ])
        self.assertEqual(result["execution_performed"], False)
        self.assertEqual(len(client.calls), 1)

    def test_exact_live_failed_intent_with_duplicate_weekly_hours_is_repaired(self):
        intent = valid_intent()
        intent["fields"] = ["weekly_hours", "weekly_hours", "housing_per_day"]
        result = pipeline.generate_compile_validate(LIVE_REQUEST, FakeClient([json.dumps(intent)]))
        self.assertTrue(result["valid"])
        fields = [step["args"]["field"] for step in result["compiled_plan"]["steps"] if step["operation"] == "extract_number"]
        self.assertEqual(set(fields), {"hourly_wage", "weekly_hours", "housing_per_day"})

    def test_duplicate_fields_are_deduplicated_without_losing_dependencies(self):
        intent = valid_intent()
        intent["fields"] = ["weekly_hours", "weekly_hours"]
        intent["calculations"] = [{"name": "weekly_gross"}]
        intent["report"]["include_fields"] = ["weekly_hours"]
        intent["report"]["include_calculations"] = ["weekly_gross"]
        intent["assumptions"] = []
        result = pipeline.generate_compile_validate("Calculate a job offer weekly gross and preview a report.", FakeClient([json.dumps(intent)]))
        self.assertTrue(result["valid"])
        fields = [step["args"]["field"] for step in result["compiled_plan"]["steps"] if step["operation"] == "extract_number"]
        self.assertEqual(fields, ["weekly_hours", "hourly_wage"])

    def test_difference_dependency_closure_derives_all_source_fields(self):
        intent = valid_intent()
        intent["fields"] = []
        intent["calculations"] = [{"name": "difference"}]
        intent["report"] = {"requested": False}
        intent["assumptions"] = []
        with self.assertRaises(pipeline.IntentError):
            pipeline.validate_intent(intent)

        intent["assumptions"] = [{"name": "days_per_week", "value": 7, "unit": "days/week"}]
        intent["report"] = {"requested": True}
        # The intent is valid once the compiler sees the calculation closure;
        # the user-facing request still needs to name the requested output.
        pipeline.validate_intent(intent)
        plan = pipeline.compile_intent(intent)
        fields = {step["args"]["field"] for step in plan["steps"] if step["operation"] == "extract_number"}
        calculations = {step["args"]["save_as"] for step in plan["steps"] if step["operation"] == "calculate"}
        self.assertEqual(fields, {"hourly_wage", "weekly_hours", "housing_per_day"})
        self.assertEqual(calculations, {"weekly_gross", "weekly_housing", "difference"})

    def test_weekly_gross_only_and_weekly_housing_only_compile(self):
        intent = valid_intent()
        intent["fields"] = []
        intent["calculations"] = [{"name": "weekly_gross"}]
        intent["report"] = {"requested": False}
        intent["assumptions"] = []
        plan = pipeline.compile_intent(intent)
        self.assertEqual({step["args"]["field"] for step in plan["steps"] if step["operation"] == "extract_number"}, {"hourly_wage", "weekly_hours"})

        intent["calculations"] = [{"name": "weekly_housing"}]
        intent["assumptions"] = [{"name": "days_per_week", "value": 7, "unit": "days/week"}]
        plan = pipeline.compile_intent(intent)
        self.assertEqual({step["args"]["field"] for step in plan["steps"] if step["operation"] == "extract_number"}, {"housing_per_day"})

    def test_model_invented_values_are_not_accepted(self):
        invented = valid_intent()
        invented["assumptions"].append({"name": "weekly_wage", "value": 1200, "unit": "USD/week"})
        client = FakeClient([json.dumps(invented), json.dumps(invented)])
        result = pipeline.generate_compile_validate(LIVE_REQUEST, client)
        self.assertFalse(result["valid"])
        self.assertEqual(len(client.calls), 2)
        self.assertIn(result["errors"][0]["code"], {"invalid_intent", "identical_intent"})

    def test_raw_regex_formula_and_unknown_fields_are_rejected(self):
        for mutation in (
            lambda intent: intent["calculations"][0].update({"formula": "hourly_wage * weekly_hours"}),
            lambda intent: intent.update({"raw_regex": "(.*)"}),
            lambda intent: intent["document"].update({"quote": "$1200"}),
        ):
            candidate = valid_intent()
            mutation(candidate)
            with self.subTest(candidate=candidate):
                client = FakeClient([json.dumps(candidate), json.dumps(candidate)])
                result = pipeline.generate_compile_validate(LIVE_REQUEST, client)
                self.assertFalse(result["valid"])

    def test_unsupported_document_and_missing_fields_are_rejected(self):
        unsupported = valid_intent()
        unsupported["document"]["source"] = "other.pdf"
        with self.subTest(candidate=unsupported):
            client = FakeClient([json.dumps(unsupported), json.dumps(unsupported)])
            result = pipeline.generate_compile_validate(LIVE_REQUEST, client)
            self.assertFalse(result["valid"])

        missing = valid_intent()
        missing["fields"].remove("housing_per_day")
        result = pipeline.generate_compile_validate(LIVE_REQUEST, FakeClient([json.dumps(missing)]))
        self.assertTrue(result["valid"])
        compiled_fields = {step["args"]["field"] for step in result["compiled_plan"]["steps"] if step["operation"] == "extract_number"}
        self.assertIn("housing_per_day", compiled_fields)

    def test_invalid_assumption_and_incomplete_calculation_are_rejected(self):
        bad_assumption = valid_intent()
        bad_assumption["assumptions"][0]["value"] = 5
        incomplete = valid_intent()
        incomplete["calculations"] = [{"name": "weekly_gross"}]
        for candidate in (bad_assumption, incomplete):
            with self.subTest(candidate=candidate):
                client = FakeClient([json.dumps(candidate), json.dumps(candidate)])
                result = pipeline.generate_compile_validate(LIVE_REQUEST, client)
                self.assertFalse(result["valid"])

    def test_unsupported_calculation_is_rejected(self):
        candidate = valid_intent()
        candidate["calculations"] = [{"name": "monthly_net_profit"}]
        result = pipeline.generate_compile_validate(LIVE_REQUEST, FakeClient([json.dumps(candidate), json.dumps(candidate)]))
        self.assertFalse(result["valid"])

    def test_valid_alternate_supported_intent(self):
        intent = valid_intent()
        intent["fields"] = ["hourly_wage", "weekly_hours"]
        intent["calculations"] = [{"name": "weekly_gross"}]
        intent["report"]["include_fields"] = ["hourly_wage", "weekly_hours"]
        intent["report"]["include_calculations"] = ["weekly_gross"]
        intent["assumptions"] = []
        result = pipeline.generate_compile_validate("Calculate a job offer weekly gross and preview a report.", FakeClient([json.dumps(intent)]))
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["compiled_plan"]["steps"]), 6)

    def test_task_completeness_and_dependency_validation_are_preserved(self):
        intent = valid_intent()
        intent["calculations"] = [{"name": "weekly_gross"}, {"name": "weekly_housing"}]
        intent["report"]["include_calculations"] = ["weekly_gross", "weekly_housing"]
        result = pipeline.generate_compile_validate(LIVE_REQUEST, FakeClient([json.dumps(intent), json.dumps(intent)]))
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "invalid_intent")

    def test_two_generation_limit_and_identical_intent_detection(self):
        content = json.dumps(valid_intent())
        client = FakeClient([content, content, content])
        result = pipeline.generate_compile_validate(LIVE_REQUEST, client)
        self.assertTrue(result["valid"])
        self.assertEqual(len(client.calls), 1)

        invalid = valid_intent()
        invalid["assumptions"][0]["value"] = 6
        client = FakeClient([json.dumps(invalid), json.dumps(invalid), json.dumps(valid_intent())])
        result = pipeline.generate_compile_validate(LIVE_REQUEST, client)
        self.assertFalse(result["valid"])
        self.assertEqual(len(client.calls), 2)

    def test_no_retrieval_writes_approvals_or_execution(self):
        client = FakeClient([json.dumps(valid_intent())])
        with tempfile.TemporaryDirectory() as directory:
            result = pipeline.generate_compile_validate(LIVE_REQUEST, client)
            self.assertTrue(result["valid"])
            self.assertFalse(result["execution_performed"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_lm_unavailable_and_structured_output_failure_are_clear(self):
        unavailable = FakeClient([pipeline.LMUnavailable("unavailable")])
        result = pipeline.generate_compile_validate(LIVE_REQUEST, unavailable)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "lm_studio_unavailable")

        unsupported = FakeClient([pipeline.StructuredOutputUnsupported("unsupported")])
        result = pipeline.generate_compile_validate(LIVE_REQUEST, unsupported)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "structured_output_unsupported")

    def test_lm_client_uses_fixed_intent_schema(self):
        response = Response(json.dumps({"choices": [{"message": {"content": json.dumps(valid_intent())}}]}).encode())
        opener = Mock(return_value=response)
        client = pipeline.IntentLMStudioClient(opener=opener)
        self.assertIsInstance(client.generate([{"role": "user", "content": LIVE_REQUEST}]), str)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, pipeline.LM_STUDIO_ENDPOINT)
        sent = json.loads(request.data.decode())
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertEqual(sent["response_format"]["json_schema"]["schema"]["properties"]["intent_version"]["const"], pipeline.INTENT_VERSION)

    def test_lm_endpoint_allowlist_supports_windows_and_docker_only(self):
        self.assertEqual(pipeline.resolve_lm_studio_endpoint(pipeline.LM_STUDIO_WINDOWS_ENDPOINT), pipeline.LM_STUDIO_WINDOWS_ENDPOINT)
        self.assertEqual(pipeline.resolve_lm_studio_endpoint(pipeline.LM_STUDIO_DOCKER_ENDPOINT), pipeline.LM_STUDIO_DOCKER_ENDPOINT)
        for value in (
            "https://localhost:1234/v1/chat/completions",
            "http://evil.example:1234/v1/chat/completions",
            "http://localhost:9999/v1/chat/completions",
            "http://user:pass@localhost:1234/v1/chat/completions",
            "http://localhost:1234/v1/models?redirect=1",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    pipeline.resolve_lm_studio_endpoint(value)

    def test_default_lm_opener_invokes_open_method(self):
        response = type("Response", (), {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *args: False,
            "read": lambda self, limit: json.dumps({"choices": [{"message": {"content": json.dumps(valid_intent())}}]}).encode(),
        })()
        director = Mock()
        director.open.return_value = response
        builder = Mock(return_value=director)
        with patch.object(pipeline.urllib.request, "build_opener", builder):
            client = pipeline.IntentLMStudioClient()
            client.generate([{"role": "user", "content": "make a plan"}])
        builder.assert_called_once()
        director.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
