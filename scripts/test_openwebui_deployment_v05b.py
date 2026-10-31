import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "test-environment" / "docker-compose.openwebui-test.yml"
TOOL = ROOT / "tools" / "analyze_with_planner.py"
SERVICE = ROOT / "scripts" / "planner_service_v05b.py"


class OpenWebUITestDeploymentTests(unittest.TestCase):
    def test_separate_port_volume_networks_and_tool_mount(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:3001:8080"', text)
        self.assertIn("open-webui-test-data", text)
        self.assertIn("name: agent-internal", text)
        self.assertIn("      - agent-internal\n      - planner-egress", text)
        self.assertIn("  planner-egress:\n    external: true\n    name: planner-egress-test", text)
        self.assertIn("analyze_with_planner.py", text)
        self.assertIn("planner-api-token", text)
        self.assertNotIn("docker.sock", text)
        self.assertNotIn("open-webui-data", text)
        self.assertNotIn("knowledge-token", text)

    def test_tool_has_fixed_api_and_no_execution_surfaces(self):
        text = TOOL.read_text(encoding="utf-8")
        self.assertIn("http://planner-service:8105/analyze", text)
        self.assertIn("/run/secrets/planner_api_token", text)
        self.assertNotIn("task_planner_v04d", text)
        self.assertNotIn("docker.sock", text)
        self.assertNotIn("knowledge-token", text)

    def test_service_has_health_and_analysis_only(self):
        text = SERVICE.read_text(encoding="utf-8")
        self.assertIn('self.path != "/analyze"', text)
        self.assertIn('self.path == "/health"', text)
        self.assertNotIn('self.path == "/commit"', text)
        self.assertNotIn('self.path == "/reconcile"', text)
        self.assertNotIn("PLANNER_DIAGNOSTIC", text)


if __name__ == "__main__":
    unittest.main()
