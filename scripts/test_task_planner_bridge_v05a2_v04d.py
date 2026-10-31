import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import task_planner_bridge_v05a2_v04d as bridge


FULL_PLAN = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "full_job_offer_plan_v04d.json"
INTENT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "valid_job_offer_intent_v05a2.json"


def full_plan_v05a():
    return bridge.PLANNER.compile_intent(full_intent())


def full_intent():
    return json.loads(INTENT.read_text(encoding="utf-8"))


def payload():
    return {"status": "success", "result_count": 3, "results": [
        {"text": "The offered wage is 16 $ per hour.", "source": "sample.pdf", "location": "Page 1", "chunk_id": "wage-1"},
        {"text": "Expected schedule is 32 hours per week.", "source": "sample.pdf", "location": "Page 2", "chunk_id": "hours-1"},
        {"text": "Housing cost: $15.0\nHow often is rent due? Per day.", "source": "sample.pdf", "location": "Page 3", "chunk_id": "housing-1"},
    ]}


def adapter():
    value = Mock()
    value.search.return_value = payload()
    return value


class BridgeTests(unittest.TestCase):
    def isolated(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "output").mkdir()
        approvals = root / "approvals"
        self.addCleanup(temporary.cleanup)
        return root, approvals

    def test_full_job_offer_prepare_is_nine_steps_and_creates_no_report(self):
        root, approvals = self.isolated()
        result = bridge.prepare_from_plan(full_plan_v05a(), isolated_root=root, approval_dir=approvals, adapter=adapter())
        self.assertTrue(result["valid"])
        self.assertFalse(result["execution_performed"])
        self.assertFalse(result["commit_performed"])
        self.assertEqual(len(result["compiled_plan"]["steps"]), 9)
        target = root / result["compiled_plan"]["steps"][-2]["args"]["path"]
        self.assertFalse(target.exists())
        self.assertTrue(approvals.exists())

    def test_natural_language_to_intent_to_prepare_pipeline(self):
        root, approvals = self.isolated()
        client = Mock()
        client.generate.return_value = json.dumps(full_intent())
        result = bridge.prepare_from_request(
            "Analyze the job offer and preview weekly gross, housing, and difference.",
            model_client=client,
            isolated_root=root,
            approval_dir=approvals,
            adapter=adapter(),
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["planning"]["execution_performed"], False)
        self.assertEqual(len(result["compiled_plan"]["steps"]), 9)
        self.assertEqual(result["prepare"]["status"], "prepared")

    def test_weekly_gross_only_prepare_is_six_steps(self):
        intent = full_intent()
        intent["fields"] = ["weekly_hours", "weekly_hours"]
        intent["calculations"] = [{"name": "weekly_gross"}]
        intent["report"] = {"requested": True}
        intent["assumptions"] = []
        planning = bridge.PLANNER.generate_compile_validate("Calculate a job offer weekly gross from wage and weekly hours and preview a report.", Mock(generate=Mock(return_value=json.dumps(intent))))
        self.assertTrue(planning["valid"])
        root, approvals = self.isolated()
        result = bridge.prepare_from_plan(planning["compiled_plan"], isolated_root=root, approval_dir=approvals, adapter=adapter())
        self.assertEqual(len(result["compiled_plan"]["steps"]), 6)
        self.assertFalse((root / "output" / "task-plan-v05a2-preview.md").exists())

    def test_incompatible_versions_operations_and_targets_are_rejected(self):
        root, approvals = self.isolated()
        bad_version = full_plan_v05a()
        bad_version["version"] = "0.5b"
        with self.assertRaises(bridge.BridgeError):
            bridge.prepare_from_plan(bad_version, isolated_root=root, approval_dir=approvals, adapter=adapter())

        bad_operation = full_plan_v05a()
        bad_operation["steps"][0]["operation"] = "run_command"
        with self.assertRaises(bridge.BridgeError):
            bridge.prepare_from_plan(bad_operation, isolated_root=root, approval_dir=approvals, adapter=adapter())

        bad_target = full_plan_v05a()
        bad_target["steps"][-2]["args"]["path"] = "output/model-chosen.md"
        with self.assertRaises(bridge.BridgeError):
            bridge.prepare_from_plan(bad_target, isolated_root=root, approval_dir=approvals, adapter=adapter())

    def test_missing_conflicting_evidence_and_calculation_failure_create_no_report(self):
        root, approvals = self.isolated()
        missing = adapter()
        missing.search.return_value = {"status": "success", "result_count": 1, "results": [payload()["results"][0]]}
        with self.assertRaises(bridge.V04D.ApprovalError):
            bridge.prepare_from_plan(full_plan_v05a(), isolated_root=root, approval_dir=approvals, adapter=missing)
        self.assertFalse(list((root / "output").iterdir()))

        conflict = adapter()
        conflict.search.return_value = {"status": "success", "result_count": 4, "results": payload()["results"] + [{"text": "Housing cost 18 per day", "source": "sample.pdf", "location": "Page 4", "chunk_id": "housing-2"}]}
        with self.assertRaises(bridge.V04D.ApprovalError):
            bridge.prepare_from_plan(full_plan_v05a(), isolated_root=root, approval_dir=approvals, adapter=conflict)
        self.assertFalse(list((root / "output").iterdir()))

    def test_production_adapter_and_commit_are_not_reachable(self):
        self.assertNotIn("commit(", Path(__file__).with_name("task_planner_bridge_v05a2_v04d.py").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(bridge.BridgeError):
                bridge._test_adapter(Path(directory) / "token")

    def test_windows_and_linux_token_paths_are_platform_exact(self):
        self.assertEqual(bridge._expected_test_token("nt"), bridge.WINDOWS_TEST_TOKEN)
        self.assertEqual(bridge._expected_test_token("posix"), bridge.LINUX_TEST_TOKEN)
        bridge._validate_test_token_path(Path(bridge.WINDOWS_TEST_TOKEN), "nt", require_existing=False)
        bridge._validate_test_token_path(bridge.LINUX_TEST_TOKEN, "posix", require_existing=False)

        with self.assertRaises(bridge.BridgeError):
            bridge._validate_test_token_path(bridge.LINUX_TEST_TOKEN, "nt", require_existing=False)
        with self.assertRaises(bridge.BridgeError):
            bridge._validate_test_token_path(Path(bridge.WINDOWS_TEST_TOKEN), "posix", require_existing=False)

    def test_arbitrary_files_and_directories_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            arbitrary = Path(directory) / "token"
            arbitrary.write_text("not used", encoding="utf-8")
            with self.assertRaises(bridge.BridgeError):
                bridge._validate_test_token_path(arbitrary, os.name, require_existing=False)
            with self.assertRaises(bridge.BridgeError):
                bridge._validate_test_token_path(Path(directory), os.name, require_existing=False)

    def test_schema_mismatch_and_adapter_failure_are_fail_closed(self):
        root, approvals = self.isolated()
        mismatch = full_plan_v05a()
        mismatch["steps"][-2]["args"]["path"] = "temp/not-the-compiler-target.md"
        with self.assertRaises(bridge.BridgeError):
            bridge.prepare_from_plan(mismatch, isolated_root=root, approval_dir=approvals, adapter=adapter())

        failed_adapter = Mock()
        failed_adapter.search.side_effect = RuntimeError("retrieval failed")
        with self.assertRaises(RuntimeError):
            bridge.prepare_from_plan(full_plan_v05a(), isolated_root=root, approval_dir=approvals, adapter=failed_adapter)
        self.assertFalse(list((root / "output").iterdir()))


if __name__ == "__main__":
    unittest.main()
