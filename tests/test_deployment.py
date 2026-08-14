from __future__ import annotations

import configparser
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def read_unit(name: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(interpolation=None, strict=True)
    loaded = parser.read(SYSTEMD / name, encoding="utf-8")
    if not loaded:
        raise AssertionError(f"missing systemd unit: {name}")
    return parser


class DeploymentTemplateTests(unittest.TestCase):
    def test_gateway_stays_loopback_and_does_not_require_host_lifetime(self) -> None:
        unit = read_unit("dsh-mcp-gateway.service")
        unit_section = unit["Unit"]
        service = unit["Service"]
        command = service["ExecStart"]

        self.assertIn("dsh-web-host.service", unit_section["Wants"])
        self.assertNotIn("Requires", unit_section)
        self.assertIn("--dsh-web-url http://127.0.0.1:3080", command)
        self.assertIn("--bind-host 127.0.0.1", command)
        self.assertIn("--port 18766", command)
        self.assertEqual(service["UMask"], "0077")
        self.assertEqual(service["StateDirectoryMode"], "0700")
        self.assertEqual(service["NoNewPrivileges"], "true")
        self.assertEqual(service["ProtectSystem"], "strict")

    def test_dsh_web_host_is_loopback_only_and_separately_restartable(self) -> None:
        unit = read_unit("dsh-web-host.service")
        service = unit["Service"]
        command = service["ExecStart"]

        self.assertIn("--host 127.0.0.1", command)
        self.assertIn("--port 3080", command)
        self.assertEqual(service["Restart"], "on-failure")
        self.assertEqual(service["UMask"], "0077")
        self.assertIn("/var/lib/dsh-harness", service["ReadWritePaths"])
        self.assertIn("/srv/dsh-workspace", service["ReadWritePaths"])

    def test_environment_examples_contain_no_committed_secret_values(self) -> None:
        gateway_env = (SYSTEMD / "gateway.env.example").read_text(encoding="utf-8")
        dsh_env = (SYSTEMD / "dsh.env.example").read_text(encoding="utf-8")

        self.assertIn("DSH_MCP_GATEWAY_ADMIN_PIN=\n", gateway_env)
        self.assertIn("DEEPSEEK_API_KEY=\n", dsh_env)
        self.assertNotIn("poc-key", gateway_env + dsh_env)
        self.assertNotIn("trycloudflare.com", gateway_env + dsh_env)


if __name__ == "__main__":
    unittest.main()
