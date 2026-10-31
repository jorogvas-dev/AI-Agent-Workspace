import json
import os
import re
import subprocess
import sys
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT_DIR = Path("/workspace/scripts").resolve()
WORK_DIR = Path("/workspace/temp").resolve()
TOKEN = os.environ["EXECUTOR_TOKEN"]
MAX_RUNTIME = 30
WORKFLOW_RUNTIME = 90
MAX_OUTPUT = 20000
ALLOWED_SCRIPTS = {
    "job_offer_workflow_v03.py": WORKFLOW_RUNTIME,
}

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/run":
            return self.send_json(404, {"error": "Not found"})

        supplied = self.headers.get("Authorization", "")
        if not secrets.compare_digest(supplied, "Bearer " + TOKEN):
            return self.send_json(401, {"error": "Unauthorized"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                return self.send_json(400, {"error": "Invalid request size"})

            data = json.loads(self.rfile.read(length))
            filename = data.get("filename", "")

            if not isinstance(filename, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]+\.py", filename
            ):
                return self.send_json(400, {"error": "Use a Python filename only"})

            if filename not in ALLOWED_SCRIPTS:
                return self.send_json(403, {"error": "Script is not approved"})

            script = (SCRIPT_DIR / filename).resolve()

            if script.parent != SCRIPT_DIR or not script.is_file():
                return self.send_json(404, {"error": "Script not found"})

            child_env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(WORK_DIR),
                "PYTHONIOENCODING": "utf-8"
            }

            if filename == "job_offer_workflow_v03.py":
                # The deployment must provide this path as a read-only secret
                # mount. The token itself never enters argv, logs, or output.
                child_env["KNOWLEDGE_TOKEN_FILE"] = (
                    "/run/secrets/agent_knowledge_token"
                )

            result = subprocess.run(
                [sys.executable, "-I", str(script)],
                cwd=str(WORK_DIR),
                capture_output=True,
                text=True,
                timeout=ALLOWED_SCRIPTS[filename],
                env=child_env
            )

            response = {
                "return_code": result.returncode,
                "stdout": result.stdout[:MAX_OUTPUT],
                "stderr": result.stderr[:MAX_OUTPUT],
                "timed_out": False
            }

            if filename == "job_offer_workflow_v03.py":
                try:
                    structured = json.loads(result.stdout)
                except (TypeError, ValueError):
                    structured = None

                if isinstance(structured, dict):
                    response["result"] = structured

            return self.send_json(200, response)

        except subprocess.TimeoutExpired:
            return self.send_json(200, {
                "error": "Execution timed out",
                "timed_out": True
            })
        except Exception as e:
            return self.send_json(500, {"error": str(e)})

    def log_message(self, format, *args):
        print("Executor:", format % args, flush=True)

def main():
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
