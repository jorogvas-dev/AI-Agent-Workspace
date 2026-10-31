import hashlib
import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import task_planner_v04d as planner


PLAN_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "full_job_offer_plan_v04d.json"


def full_plan():
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def payload():
    return {
        "status": "success",
        "result_count": 3,
        "results": [
            {"text": "The offered wage is 16 $ per hour.", "source": "sample.pdf", "location": "Page 1", "chunk_id": "wage-1"},
            {"text": "Expected schedule is 32 hours per week.", "source": "sample.pdf", "location": "Page 2", "chunk_id": "hours-1"},
            {"text": "Housing cost: $15.0\nHow often is rent due? Per day.", "source": "sample.pdf", "location": "Page 3", "chunk_id": "housing-1"},
        ],
    }


def adapter():
    result = Mock()
    result.search.return_value = payload()
    return result


class PlannerV04dTests(unittest.TestCase):
    def make_isolated(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "output").mkdir()
        approvals = root / "approvals"
        self.addCleanup(temporary.cleanup)
        return root, approvals

    def prepare(self, root, approvals):
        return planner.prepare(full_plan(), adapter=adapter(), isolated_root=root, approval_dir=approvals, now=100.0)

    @staticmethod
    def confirmation(prepared):
        return f"APPROVE {prepared['approval_id']} {prepared['target']} {prepared['content_sha256']}"

    def test_prepare_creates_bounded_pending_approval_without_output_file(self):
        root, approvals = self.make_isolated()
        prepared = self.prepare(root, approvals)
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(len(full_plan()["steps"]), 9)
        self.assertFalse((root / "output" / "job-offer-v04d-full.md").exists())
        approval_file = approvals / f"approval-{prepared['approval_id']}.json"
        record = json.loads(approval_file.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "pending")
        self.assertLessEqual(approval_file.stat().st_size, planner.MAX_APPROVAL_RECORD_BYTES)
        self.assertEqual(record["content_sha256"], hashlib.sha256(record["content"].encode()).hexdigest())

    def test_commit_success_is_atomic_verified_and_single_use(self):
        root, approvals = self.make_isolated()
        prepared = self.prepare(root, approvals)
        committed = planner.commit(
            prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals,
            approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"],
            approved_plan_sha256=prepared["plan_sha256"], approve=True, human_confirmation=self.confirmation(prepared), now=200.0,
        )
        target = root / "output" / "job-offer-v04d-full.md"
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(committed["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        with self.assertRaisesRegex(planner.ApprovalError, "already been committed"):
            planner.commit(
                prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals,
                approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"],
                approved_plan_sha256=prepared["plan_sha256"], approve=True, human_confirmation=self.confirmation(prepared), now=201.0,
            )

    def test_commit_requires_exact_approval_and_rejects_missing_expired_tampered_and_replay(self):
        root, approvals = self.make_isolated()
        with self.assertRaisesRegex(planner.ApprovalError, "unavailable"):
            planner.commit("A" * 40, full_plan(), isolated_root=root, approval_dir=approvals, approved_target="output/x", approved_content_sha256="x", approved_plan_sha256="x", approve=True, human_confirmation="APPROVE " + "A" * 40 + " output/x x", now=100)
        prepared = self.prepare(root, approvals)
        with self.assertRaisesRegex(planner.ApprovalError, "expired"):
            planner.commit(prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals, approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"], approved_plan_sha256=prepared["plan_sha256"], approve=True, human_confirmation=self.confirmation(prepared), now=prepared["expires_at"])

        prepared = self.prepare(root, approvals)
        approval_file = approvals / f"approval-{prepared['approval_id']}.json"
        record = json.loads(approval_file.read_text(encoding="utf-8"))
        record["content"] += "tampered"
        approval_file.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(planner.ApprovalError, "hash mismatch"):
            planner.commit(prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals, approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"], approved_plan_sha256=prepared["plan_sha256"], approve=True, human_confirmation=self.confirmation(prepared), now=200)

    def test_commit_rejects_without_explicit_approval_hash_mismatch_existing_target_and_path_escape(self):
        root, approvals = self.make_isolated()
        prepared = self.prepare(root, approvals)
        with self.assertRaisesRegex(planner.ApprovalError, "explicit human confirmation"):
            planner.commit(prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals, approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"], approved_plan_sha256=prepared["plan_sha256"], now=200)
        with self.assertRaisesRegex(planner.ApprovalError, "content hash"):
            planner.commit(prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals, approved_target=prepared["target"], approved_content_sha256="0" * 64, approved_plan_sha256=prepared["plan_sha256"], approve=True, human_confirmation=f"APPROVE {prepared['approval_id']} {prepared['target']} {'0' * 64}", now=200)

        (root / "output" / "job-offer-v04d-full.md").write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(planner.ApprovalError, "already exists|unavailable"):
            planner.commit(prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals, approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"], approved_plan_sha256=prepared["plan_sha256"], approve=True, human_confirmation=self.confirmation(prepared), now=200)

        bad = full_plan()
        bad["steps"][7]["args"]["path"] = "output/../knowledge/leak.md"
        with self.assertRaises(planner.PlanValidationError):
            planner.prepare(bad, adapter=adapter(), isolated_root=root, approval_dir=approvals)

    def test_symlink_and_interrupted_write_fail_closed(self):
        root, approvals = self.make_isolated()
        outside = root / "outside"
        outside.mkdir()
        try:
            (root / "output" / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        bad = full_plan()
        bad["steps"][7]["args"]["path"] = "output/link/report.md"
        with self.assertRaises(planner.PlanValidationError):
            planner.prepare(bad, adapter=adapter(), isolated_root=root, approval_dir=approvals)

        root, approvals = self.make_isolated()
        prepared = self.prepare(root, approvals)
        with patch.object(planner.os, "link", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                planner.commit(prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals, approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"], approved_plan_sha256=prepared["plan_sha256"], approve=True, human_confirmation=self.confirmation(prepared), now=200)
        self.assertFalse((root / "output" / "job-offer-v04d-full.md").exists())

    def test_concurrent_commit_has_one_exclusive_winner(self):
        root, approvals = self.make_isolated()
        prepared = self.prepare(root, approvals)
        entered = threading.Event()
        release = threading.Event()
        original_create = planner._atomic_create
        first_call = {"value": True}

        def blocked_create(target, content):
            if first_call["value"]:
                first_call["value"] = False
                entered.set()
                self.assertTrue(release.wait(5))
            return original_create(target, content)

        results = []

        def run_commit():
            try:
                results.append(planner.commit(
                    prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals,
                    approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"],
                    approved_plan_sha256=prepared["plan_sha256"], approve=True,
                    human_confirmation=self.confirmation(prepared), now=200.0,
                ))
            except Exception as exc:
                results.append(exc)

        with patch.object(planner, "_atomic_create", side_effect=blocked_create):
            first = threading.Thread(target=run_commit)
            first.start()
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(planner.ApprovalError, "already committing|already claimed"):
                planner.commit(
                    prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals,
                    approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"],
                    approved_plan_sha256=prepared["plan_sha256"], approve=True,
                    human_confirmation=self.confirmation(prepared), now=200.0,
                )
            release.set()
            first.join(5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "committed")

    def test_reconcile_crash_before_target_marks_failed_without_retry(self):
        root, approvals = self.make_isolated()
        prepared = self.prepare(root, approvals)
        with patch.object(planner, "_atomic_create", side_effect=KeyboardInterrupt("crash before target")):
            with self.assertRaises(KeyboardInterrupt):
                planner.commit(
                    prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals,
                    approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"],
                    approved_plan_sha256=prepared["plan_sha256"], approve=True,
                    human_confirmation=self.confirmation(prepared), now=200.0,
                )
        with patch.object(planner, "_pid_alive", return_value=False):
            with self.assertRaisesRegex(planner.ApprovalError, "before target creation"):
                planner.reconcile_approval(prepared["approval_id"], isolated_root=root, approval_dir=approvals, now=201.0)
        record = json.loads((approvals / f"approval-{prepared['approval_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], planner.STATE_FAILED)
        self.assertFalse((root / "output" / "job-offer-v04d-full.md").exists())

    def test_reconcile_crash_after_target_preserves_target_and_requires_reconciliation(self):
        root, approvals = self.make_isolated()
        prepared = self.prepare(root, approvals)
        original_create = planner._atomic_create
        calls = {"count": 0}

        def crash_before_receipt(target, content):
            calls["count"] += 1
            if calls["count"] == 1:
                return original_create(target, content)
            raise KeyboardInterrupt("crash before receipt")

        with patch.object(planner, "_atomic_create", side_effect=crash_before_receipt):
            with self.assertRaises(KeyboardInterrupt):
                planner.commit(
                    prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals,
                    approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"],
                    approved_plan_sha256=prepared["plan_sha256"], approve=True,
                    human_confirmation=self.confirmation(prepared), now=200.0,
                )
        target = root / "output" / "job-offer-v04d-full.md"
        with patch.object(planner, "_pid_alive", return_value=False):
            with self.assertRaisesRegex(planner.ApprovalError, "without a durable receipt"):
                planner.reconcile_approval(prepared["approval_id"], isolated_root=root, approval_dir=approvals, now=201.0)
        self.assertTrue(target.exists())

    def test_reconcile_after_receipt_persistence_finalizes_commit(self):
        root, approvals = self.make_isolated()
        prepared = self.prepare(root, approvals)
        original_json = planner._atomic_json

        def crash_after_receipt(path, record):
            if record.get("status") == planner.STATE_COMMITTED:
                raise KeyboardInterrupt("crash after receipt")
            return original_json(path, record)

        with patch.object(planner, "_atomic_json", side_effect=crash_after_receipt):
            with self.assertRaises(KeyboardInterrupt):
                planner.commit(
                    prepared["approval_id"], full_plan(), isolated_root=root, approval_dir=approvals,
                    approved_target=prepared["target"], approved_content_sha256=prepared["content_sha256"],
                    approved_plan_sha256=prepared["plan_sha256"], approve=True,
                    human_confirmation=self.confirmation(prepared), now=200.0,
                )
        with patch.object(planner, "_pid_alive", return_value=False):
            recovered = planner.reconcile_approval(prepared["approval_id"], isolated_root=root, approval_dir=approvals, now=201.0)
        self.assertEqual(recovered["status"], planner.STATE_COMMITTED)
        self.assertTrue((root / "output" / "job-offer-v04d-full.md").exists())

    def test_model_facing_excerpt_is_short_and_not_full_chunk(self):
        candidate = full_plan()
        fake = adapter()
        fake.search.return_value = {
            "status": "success", "result_count": 3, "results": [
                {"text": "The offered wage is 16 $ per hour. Unrelated identifying detail " + "X" * 500, "source": "sample.pdf", "location": "Page 1", "chunk_id": "wage-1"},
                payload()["results"][1], payload()["results"][2],
            ]}
        result = planner.BASE.run_mode("test", {**candidate, "version": "0.4c"}, adapter=fake)
        fact = next(record["fact"] for record in result["records"] if record["id"] == "extract_hourly_wage")
        self.assertLessEqual(len(fact["evidence_excerpt"]), 120)
        self.assertNotIn("Unrelated identifying detail", fact["evidence_excerpt"])
        self.assertNotIn("evidence", fact)

    def test_cli_validate_and_dry_run_statuses_are_distinct(self):
        root, approvals = self.make_isolated()
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps(full_plan()), encoding="utf-8")
        self.assertEqual(planner.main(["validate", str(plan_path), "--workspace", str(root)]), 0)
        self.assertEqual(planner.main(["dry_run", str(plan_path), "--workspace", str(root)]), 1)
        self.assertFalse((root / "output" / "job-offer-v04d-full.md").exists())

    def test_production_root_and_executor_allowlist_remain_closed(self):
        with self.assertRaisesRegex(planner.ApprovalError, "isolated test-environment"):
            planner._ensure_isolated_paths(
                Path(r"E:\AI-Agent-Workspace"),
                Path(r"E:\AI-Agent-Workspace\test-environment\approvals"),
            )
        executor_source = (Path(__file__).resolve().parent.parent / "executor" / "server.py").read_text(encoding="utf-8-sig")
        self.assertNotIn("task_planner_v04d.py", executor_source)


if __name__ == "__main__":
    unittest.main()
