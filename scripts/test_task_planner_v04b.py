import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("task_planner_v04b.py")
SPEC = importlib.util.spec_from_file_location("task_planner_v04b", SCRIPT)
PLANNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PLANNER)


def plan():
    return {
        "version": "0.4b",
        "variables": {
            "results": {"type": "search_results"},
            "wage": {"type": "number", "unit": "USD/hour"},
            "hours": {"type": "number", "unit": "hours/week"},
            "weekly": {"type": "number", "unit": "USD/week"},
        },
        "steps": [
            {"id": "search", "operation": "search_knowledge", "args": {"query": "wage", "limit": 5, "save_as": "results"}},
            {"id": "extract", "operation": "extract_number", "depends_on": ["search"], "args": {"from": "search", "field": "wage", "source": "sample.pdf", "patterns": [r"(\d+(?:\.\d+)?)\s*\$\s*per hour"], "save_as": "wage"}},
            {"id": "calc", "operation": "calculate", "depends_on": ["extract"], "args": {"operation": "multiply", "operands": ["wage", 32], "save_as": "weekly"}},
            {"id": "report", "operation": "write_report", "depends_on": ["calc"], "args": {"path": "output/report.md", "content": "Weekly: {weekly}"}},
            {"id": "verify", "operation": "verify_file", "depends_on": ["report"], "args": {"path": "output/report.md", "reference": "report"}},
        ],
    }


def response(*texts):
    return {"status": "success", "results": [
        {"source": "sample.pdf", "text": text, "location": f"Page {index + 1}", "chunk_id": f"chunk-{index}"}
        for index, text in enumerate(texts)
    ]}


class PlannerV04bTests(unittest.TestCase):
    def test_extraction_success_preserves_typed_evidence(self):
        result = PLANNER.dry_run_plan(plan(), lambda query, limit: response("Pay is 16 $ per hour"))
        self.assertTrue(result["success"])
        fact = next(item for item in result["records"] if item["id"] == "extract")["fact"]
        self.assertEqual(fact["value"], "16.00")
        self.assertEqual(fact["source"], "sample.pdf")
        self.assertEqual(fact["location"], "Page 1")
        self.assertIn("Pay is 16", fact["evidence"])

    def test_extraction_rejects_missing_ambiguous_and_conflicting_facts(self):
        for fetched in (
            lambda q, l: {"status": "success", "results": []},
            lambda q, l: response("Pay is 16 $ per hour and 18 $ per hour"),
            lambda q, l: response("Pay is 16 $ per hour", "Pay is 18 $ per hour"),
        ):
            result = PLANNER.dry_run_plan(plan(), fetched)
            self.assertFalse(result["success"])
            self.assertEqual(next(item for item in result["records"] if item["id"] == "extract")["status"], "failed")

    def test_extraction_rejects_nonnumeric_and_missing_evidence_metadata(self):
        bad = {"status": "success", "results": [{"source": "sample.pdf", "text": "Pay is abc $ per hour"}]}
        result = PLANNER.dry_run_plan(plan(), lambda q, l: bad)
        self.assertFalse(result["success"])

    def test_validation_rejects_type_mismatch(self):
        candidate = plan()
        candidate["variables"]["wage"]["type"] = "search_results"
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(candidate)

    def test_validation_rejects_invalid_dependencies_and_bad_placeholders(self):
        candidate = plan()
        candidate["steps"][1]["depends_on"] = []
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(candidate)
        candidate = plan()
        candidate["steps"][3]["args"]["content"] = "{weekly.malicious}"
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(candidate)

    def test_validation_rejects_path_traversal(self):
        candidate = plan()
        candidate["steps"][3]["args"]["path"] = "output/../knowledge/leak.txt"
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(candidate)

    def test_validation_rejects_unapproved_network_operation(self):
        candidate = plan()
        candidate["steps"][0] = {"id": "net", "operation": "http_request", "args": {"url": "https://example.invalid"}}
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(candidate)

    def test_dry_run_with_mock_fetcher_simulates_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            result = PLANNER.dry_run_plan(plan(), lambda q, l: response("Pay is 16 $ per hour"), root)
            self.assertTrue(result["success"])
            self.assertTrue(result["dry_run"])
            self.assertTrue(result["execution_performed"])
            self.assertFalse((root / "output" / "report.md").exists())

    def test_cli_dry_run_does_not_fetch_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan()), encoding="utf-8")
            self.assertEqual(PLANNER.main([str(plan_path)]), 0)
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
