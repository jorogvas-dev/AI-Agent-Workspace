import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("task_planner_v04a.py")
SPEC = importlib.util.spec_from_file_location("task_planner_v04a", SCRIPT)
PLANNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PLANNER)


def valid_plan():
    return {
        "version": "0.4a",
        "steps": [
            {
                "id": "search_wage",
                "operation": "search_knowledge",
                "args": {"query": "hourly wage", "limit": 5, "save_as": "wage"},
            },
            {
                "id": "calculate_total",
                "operation": "calculate",
                "depends_on": ["search_wage"],
                "args": {"operation": "multiply", "operands": ["wage", 32], "save_as": "weekly"},
            },
            {
                "id": "write_report",
                "operation": "write_report",
                "depends_on": ["calculate_total"],
                "args": {"path": "output/report.md", "content": "Weekly total: {weekly}"},
            },
            {
                "id": "verify_report",
                "operation": "verify_file",
                "depends_on": ["write_report"],
                "args": {"path": "output/report.md"},
            },
        ],
    }


class PlannerTests(unittest.TestCase):
    def test_valid_plan_returns_dry_run_order_without_execution(self):
        result = PLANNER.validate_plan(valid_plan())
        self.assertTrue(result["valid"])
        self.assertEqual(result["execution_order"], [
            "search_wage", "calculate_total", "write_report", "verify_report"
        ])

    def test_rejects_unknown_operation_and_arbitrary_command(self):
        plan = valid_plan()
        plan["steps"][0] = {
            "id": "shell",
            "operation": "run_command",
            "args": {"command": "whoami"},
        }
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(plan)

        plan = valid_plan()
        plan["steps"][0]["args"]["url"] = "https://example.invalid"
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(plan)

    def test_rejects_malicious_workspace_paths(self):
        for path in ("../secret.txt", "output/../secret.txt", "output\\secret.txt", "C:/secret.txt"):
            plan = valid_plan()
            plan["steps"][2]["args"]["path"] = path
            with self.subTest(path=path):
                with self.assertRaises(PLANNER.PlanValidationError):
                    PLANNER.validate_plan(plan)

    def test_rejects_dependency_cycle(self):
        plan = valid_plan()
        plan["steps"][0]["depends_on"] = ["verify_report"]
        with self.assertRaisesRegex(PLANNER.PlanValidationError, "cycle"):
            PLANNER.validate_plan(plan)

    def test_rejects_oversized_plan(self):
        plan = {"version": "0.4a", "steps": []}
        for index in range(11):
            plan["steps"].append({
                "id": f"step_{index}",
                "operation": "verify_file",
                "args": {"path": f"temp/file_{index}.txt"},
            })
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(plan)

    def test_rejects_unknown_dependencies_and_duplicate_ids(self):
        plan = valid_plan()
        plan["steps"][1]["depends_on"] = ["missing"]
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(plan)

        plan = valid_plan()
        plan["steps"][1]["id"] = plan["steps"][0]["id"]
        with self.assertRaises(PLANNER.PlanValidationError):
            PLANNER.validate_plan(plan)

    def test_rejects_existing_symlink_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            target = root / "outside"
            target.mkdir()
            try:
                (root / "output" / "link").symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable in this test environment")
            plan = valid_plan()
            plan["steps"][2]["args"]["path"] = "output/link/report.md"
            with self.assertRaises(PLANNER.PlanValidationError):
                PLANNER.validate_plan(plan, root)

    def test_cli_only_reports_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            plan_path.write_text(json.dumps(valid_plan()), encoding="utf-8")
            self.assertEqual(PLANNER.main([str(plan_path)]), 0)


if __name__ == "__main__":
    unittest.main()
