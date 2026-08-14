from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ArchitectureContractTests(unittest.TestCase):
    def test_canonical_contract_is_present_and_linked_from_primary_docs(self) -> None:
        contract = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

        self.assertIn("give ChatGPT Web a mature DSH Harness", contract)
        self.assertIn("ChatGPT Web is the only primary reasoning/model agent", contract)
        self.assertIn("local-shell-mcp is not the primary harness", contract)
        self.assertIn("community DSH extension", contract)

        self.assertIn("AGENTS.md", readme)
        self.assertIn("Give ChatGPT Web a mature DSH Harness", readme)
        self.assertIn("AGENTS.md", architecture)
        self.assertIn("give ChatGPT Web a mature DSH Harness", architecture)

    def test_dsh_bridge_uses_native_tool_runtime_seam(self) -> None:
        plugin = (ROOT / "dsh-bridge-plugin" / "index.js").read_text(encoding="utf-8")
        overlay = (ROOT / "deploy" / "dsh" / "chatgpt-bridge.cordis.yml").read_text(encoding="utf-8")

        self.assertIn("ctx.tools.schemas()", plugin)
        self.assertIn("ctx.tools.execute", plugin)
        self.assertIn("ctx.skills.list", plugin)
        self.assertIn("ctx.skills.get", plugin)
        self.assertNotIn("DEEPSEEK_API_KEY", plugin)

        gateway_unit = (ROOT / "deploy" / "systemd" / "dsh-mcp-gateway.service").read_text(encoding="utf-8")
        dsh_unit = (ROOT / "deploy" / "systemd" / "dsh-web-host.service").read_text(encoding="utf-8")
        dsh_env = (ROOT / "deploy" / "systemd" / "dsh.env.example").read_text(encoding="utf-8")
        self.assertIn("--dsh-harness-url http://127.0.0.1:3080", gateway_unit)
        self.assertNotIn("--dsh-web-url", gateway_unit)
        self.assertIn("--patch /srv/dsh-mcp-gateway/deploy/dsh/chatgpt-bridge.cordis.yml", dsh_unit)
        self.assertNotIn("DEEPSEEK_API_KEY", dsh_env)
        self.assertIn("dsh-bridge-plugin/index.js", overlay)


if __name__ == "__main__":
    unittest.main()
