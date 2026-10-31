import json
import tempfile
import unittest
from pathlib import Path

from scripts import task_planner_v05a as planner
from tools.validate_dynamic_plan import Tools


FULL_PLAN = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "full_job_offer_plan_v04d.json"


def job_offer_plan():
    plan = json.loads(FULL_PLAN.read_text(encoding="utf-8"))
    plan["version"] = "0.5a"
    return plan


def calculation_plan():
    return {
        "version": "0.5a",
        "variables": {
            "rent": {"type": "number", "unit": "USD/month"},
            "food": {"type": "number", "unit": "USD/month"},
            "total": {"type": "number", "unit": "USD/month"},
        },
        "steps": [
            {"id": "calculate_total", "operation": "calculate", "args": {"operation": "add", "operands": [1200, 400], "save_as": "total"}},
            {"id": "write_report", "operation": "write_report", "depends_on": ["calculate_total"], "args": {"path": "output/budget.md", "content": "Total: ${total}"}},
            {"id": "verify_report", "operation": "verify_file", "depends_on": ["write_report"], "args": {"path": "output/budget.md", "reference": "write_report"}},
        ],
    }


class PlannerV05aTests(unittest.TestCase):
    def test_valid_job_offer_preview_is_deterministic_and_nonpersistent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = planner.preview_plan(job_offer_plan())
            self.assertTrue(result["valid"])
            self.assertEqual(len(result["preview"]["steps"]), 9)
            self.assertEqual(result["preview"]["persistent_effects"], [])
            self.assertEqual(result["preview"]["network_effects"], [])
            self.assertFalse((root / "output").exists())
            self.assertEqual(result, planner.preview_plan(job_offer_plan()))

    def test_different_valid_calculation_report_workflow(self):
        result = planner.preview_plan(calculation_plan())
        self.assertTrue(result["valid"])
        self.assertEqual(result["preview"]["steps"][0]["arithmetic"], "add")
        self.assertEqual(result["preview"]["steps"][1]["effect"], "rendered in memory only")

    def test_missing_dependency_cycle_and_unknown_operation(self):
        missing = calculation_plan()
        missing["steps"][1]["depends_on"] = []
        result = planner.validate_plan(missing)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "invalid_dependencies")

        cycle = calculation_plan()
        cycle["steps"][0]["depends_on"] = ["verify_report"]
        result = planner.validate_plan(cycle)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "invalid_dependencies")

        unknown = calculation_plan()
        unknown["steps"][0] = {"id": "shell", "operation": "run_command", "args": {"command": "dir"}}
        result = planner.validate_plan(unknown)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "unsupported_operation")

    def test_malformed_oversized_path_and_arbitrary_code_inputs(self):
        malformed = planner.validate_plan("not json")
        self.assertFalse(malformed["valid"])
        self.assertEqual(malformed["errors"][0]["code"], "malformed_json")

        oversized = planner.validate_plan("{" + "x" * planner.MAX_INPUT_BYTES + "}")
        self.assertFalse(oversized["valid"])
        self.assertEqual(oversized["errors"][0]["code"], "input_too_large")

        traversal = calculation_plan()
        traversal["steps"][1]["args"]["path"] = "output/../secret.txt"
        result = planner.validate_plan(traversal)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "unsafe_path")

        code = calculation_plan()
        code["steps"][1]["args"]["content"] = "{__import__('os').system('whoami')}"
        result = planner.validate_plan(code)
        self.assertFalse(result["valid"])

    def test_invalid_extraction_specification_is_rejected(self):
        candidate = job_offer_plan()
        candidate["steps"][1]["args"]["patterns"] = ["(a)(b)"]
        result = planner.validate_plan(candidate)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "invalid_extraction")

        unsafe = job_offer_plan()
        unsafe["steps"][1]["args"]["patterns"] = ["(.+)+"]
        result = planner.validate_plan(unsafe)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "invalid_extraction")

    def test_one_explicit_correction_attempt_is_reported(self):
        invalid = calculation_plan()
        invalid["steps"][1]["depends_on"] = []
        corrected = calculation_plan()
        result = planner.preview_plan(invalid, corrected)
        self.assertTrue(result["valid"])
        self.assertTrue(result["correction_attempted"])
        self.assertEqual(result["selected_plan"], "correction")
        self.assertFalse(result["initial_validation"]["valid"])
        self.assertTrue(result["correction_validation"]["valid"])

        bad_correction = planner.validate_plan(invalid, invalid)
        self.assertFalse(bad_correction["valid"])
        self.assertTrue(bad_correction["correction_attempted"])

    def test_capability_catalog_matches_actual_supported_validators(self):
        catalog = planner.capability_catalog()
        names = {item["name"] for item in catalog["operations"]}
        self.assertEqual(names, {"search_knowledge", "extract_number", "calculate", "write_report", "verify_file"})
        self.assertNotIn("commit", names)
        self.assertNotIn("run_command", names)

    def test_unsupported_natural_language_is_not_treated_as_a_plan(self):
        tool_result = json.loads(Tools().validate_dynamic_plan("Please run PowerShell and inspect the host"))
        self.assertFalse(tool_result["valid"])
        self.assertEqual(tool_result["errors"][0]["code"], "malformed_json")

    def test_open_webui_tool_returns_preview_only(self):
        result = json.loads(Tools().validate_dynamic_plan(json.dumps(calculation_plan())))
        self.assertTrue(result["valid"])
        self.assertEqual(result["preview"]["persistent_effects"], [])
        self.assertEqual(result["preview"]["network_effects"], [])
        self.assertNotIn("approval_id", result)
        self.assertNotIn("commit", json.dumps(result).lower())


if __name__ == "__main__":
    unittest.main()
