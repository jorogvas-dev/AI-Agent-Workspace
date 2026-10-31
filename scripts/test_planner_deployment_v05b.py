import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "test-environment" / "docker-compose.planner-service.yml"


class PlannerDeploymentConfigTests(unittest.TestCase):
    def setUp(self):
        self.text = COMPOSE.read_text(encoding="utf-8")

    def test_networks_and_no_published_ports(self):
        self.assertIn("name: agent-internal", self.text)
        self.assertIn("name: bridge", self.text)
        self.assertIn('target: /workspace/scripts', self.text)
        self.assertIn('target: /run/secrets/agent_knowledge_token', self.text)
        self.assertNotIn("ports:", self.text)
        self.assertNotIn("docker.sock", self.text)

    def test_fixed_endpoints_and_test_only_mounts(self):
        self.assertIn("http://host.docker.internal:1234/v1/chat/completions", self.text)
        self.assertIn("/planner-test", self.text)
        self.assertNotIn("knowledge-db", self.text)
        self.assertNotIn("EXECUTOR_TOKEN", self.text)

    def test_restricted_runtime_configuration(self):
        for setting in ("read_only: true", "no-new-privileges:true", "pids_limit: 64", "mem_limit: 512m", "cpus: 1.0"):
            self.assertIn(setting, self.text)


if __name__ == "__main__":
    unittest.main()
