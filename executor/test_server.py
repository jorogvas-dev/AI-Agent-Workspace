import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("EXECUTOR_TOKEN", "executor-test-token")
sys.path.insert(0, os.path.dirname(__file__))
import server


class FakeHandler:
    def __init__(self, filename="job_offer_workflow_v03.py", token="executor-test-token"):
        body = json.dumps({"filename": filename}).encode("utf-8")
        self.path = "/run"
        self.headers = {
            "Authorization": "Bearer " + token,
            "Content-Length": str(len(body)),
        }
        self.rfile = io.BytesIO(body)
        self.responses = []

    def send_json(self, status, data):
        self.responses.append((status, data))


class ExecutorTests(unittest.TestCase):
    def test_rejects_unapproved_script_with_403(self):
        handler = FakeHandler("task_controller.py")

        with patch.object(server.subprocess, "run") as run:
            server.Handler.do_POST(handler)

        self.assertEqual(handler.responses, [(403, {"error": "Script is not approved"})])
        run.assert_not_called()

    def test_rejects_invalid_authentication(self):
        handler = FakeHandler(token="wrong-token")

        server.Handler.do_POST(handler)

        self.assertEqual(handler.responses, [(401, {"error": "Unauthorized"})])

    def test_v03_uses_approved_timeout_and_secret_file_path(self):
        handler = FakeHandler("job_offer_workflow_v03.py")
        completed = type("Completed", (), {
            "returncode": 0,
            "stdout": json.dumps({"success": True}),
            "stderr": "",
        })()

        with tempfile.TemporaryDirectory() as directory:
            script_dir = Path(directory) / "scripts"
            work_dir = Path(directory) / "temp"
            script_dir.mkdir()
            work_dir.mkdir()
            (script_dir / "job_offer_workflow_v03.py").write_text("", encoding="utf-8")
            with patch.object(server, "SCRIPT_DIR", script_dir.resolve()), patch.object(
                server, "WORK_DIR", work_dir.resolve()
            ), patch.object(server.subprocess, "run", return_value=completed) as run:
                server.Handler.do_POST(handler)

        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["timeout"], server.WORKFLOW_RUNTIME)
        self.assertEqual(
            kwargs["env"]["KNOWLEDGE_TOKEN_FILE"],
            "/run/secrets/agent_knowledge_token",
        )
        self.assertEqual(handler.responses[0][1]["result"], {"success": True})
        self.assertEqual(handler.responses[0][1]["return_code"], 0)

    def test_approved_v03_timeout_returns_timeout_result(self):
        handler = FakeHandler("job_offer_workflow_v03.py")

        with tempfile.TemporaryDirectory() as directory:
            script_dir = Path(directory) / "scripts"
            work_dir = Path(directory) / "temp"
            script_dir.mkdir()
            work_dir.mkdir()
            (script_dir / "job_offer_workflow_v03.py").write_text("", encoding="utf-8")
            with patch.object(server, "SCRIPT_DIR", script_dir.resolve()), patch.object(
                server, "WORK_DIR", work_dir.resolve()
            ), patch.object(
                server.subprocess,
                "run",
                side_effect=server.subprocess.TimeoutExpired("script", 30),
            ):
                server.Handler.do_POST(handler)

        self.assertEqual(
            handler.responses,
            [(200, {"error": "Execution timed out", "timed_out": True})],
        )


if __name__ == "__main__":
    unittest.main()
