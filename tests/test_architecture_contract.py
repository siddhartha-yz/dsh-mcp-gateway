from __future__ import annotations

import subprocess
import sys
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
        self.assertIn("stable set of meta-tools", contract)
        self.assertIn("must not depend on dynamic first-class tool refresh", contract)
        self.assertIn("default gateway mode is **meta-only**", contract)

        self.assertIn("AGENTS.md", readme)
        self.assertIn("Give ChatGPT Web a mature DSH Harness", readme)
        self.assertIn("AGENTS.md", architecture)
        self.assertIn("give ChatGPT Web a mature DSH Harness", architecture)

    def test_primary_cli_import_does_not_eagerly_load_legacy_runtime_modules(self) -> None:
        code = """
import json
import sys
import dsh_mcp_gateway.cli
names = [
    'dsh_mcp_gateway.backend',
    'dsh_mcp_gateway.routing',
    'dsh_mcp_gateway.session_runtime',
]
print(json.dumps([name for name in names if name in sys.modules]))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "[]")

    def test_legacy_top_level_exports_remain_lazy_and_compatible(self) -> None:
        code = """
import json
import sys
import dsh_mcp_gateway
before = 'dsh_mcp_gateway.backend' in sys.modules
_ = dsh_mcp_gateway.PublicSdkBridge
after = 'dsh_mcp_gateway.backend' in sys.modules
print(json.dumps([before, after]))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "[false, true]")

    def test_dsh_bridge_uses_native_tool_runtime_seam(self) -> None:
        plugin = (ROOT / "dsh-bridge-plugin" / "index.js").read_text(encoding="utf-8")
        overlay = (ROOT / "deploy" / "dsh" / "chatgpt-bridge.cordis.yml").read_text(encoding="utf-8")

        self.assertIn("'agents', 'agentPresets'", plugin)
        self.assertIn("presets.standingKeyFor(presetId)", plugin)
        self.assertIn("ctx.tools.schemas(scope)", plugin)
        self.assertNotIn("scope ? 'dsh-preset-standing' : 'global'", plugin)
        self.assertIn("ctx.skills.list", plugin)
        self.assertIn("ctx.skills.get", plugin)
        self.assertIn("CAPABILITY_SESSION_PREFIX", plugin)
        self.assertIn("createHash('sha256')", plugin)
        self.assertIn("JSON.stringify([cwd, presetId, presetPath, stamp.mtimeMs, stamp.size, stamp.digest])", plugin)
        self.assertIn("presetStamp(preset.path)", plugin)
        self.assertIn("samePresetStamp(stamp, mountedStamp)", plugin)
        self.assertIn("handle.read(chunk", plugin)
        self.assertIn("MAX_PRESET_BYTES", plugin)
        self.assertIn("agents.resume", plugin)
        self.assertIn("agents.create", plugin)
        self.assertIn("presets.mount(agentCtx, presetId)", plugin)
        self.assertIn("ctx.tools.execute", plugin)
        self.assertIn("agent: capability.agent", plugin)
        self.assertNotIn("...(agent ? { agent } : {})", plugin)
        self.assertNotIn("sessionId: `chatgpt-bridge-${randomUUID()}`", plugin)
        self.assertIn("ExternalChatGPTCapabilityAdapter", plugin)
        self.assertIn("inputModalities: ['text', 'image']", plugin)
        self.assertIn("the capability identity cannot perform model inference", plugin)
        self.assertIn("provider: EXTERNAL_PROVIDER", plugin)
        self.assertIn("model: EXTERNAL_MODEL", plugin)
        self.assertIn("const instanceId = randomUUID()", plugin)
        self.assertIn("{ instanceId, toolRevision, skillRevision }", plugin)
        self.assertNotIn("DEEPSEEK_API_KEY", plugin)

        gateway_unit = (ROOT / "deploy" / "systemd" / "dsh-mcp-gateway.service").read_text(encoding="utf-8")
        dsh_unit = (ROOT / "deploy" / "systemd" / "dsh-web-host.service").read_text(encoding="utf-8")
        dsh_env = (ROOT / "deploy" / "systemd" / "dsh.env.example").read_text(encoding="utf-8")
        self.assertIn("--dsh-harness-url http://127.0.0.1:3080", gateway_unit)
        self.assertIn("--tool-surface meta-only", gateway_unit)
        self.assertNotIn("--dsh-web-url", gateway_unit)
        self.assertIn("--patch /srv/dsh-mcp-gateway/deploy/dsh/chatgpt-bridge.cordis.yml", dsh_unit)
        self.assertNotIn("DEEPSEEK_API_KEY", dsh_env)
        self.assertIn("dsh-bridge-plugin/index.js", overlay)


if __name__ == "__main__":
    unittest.main()
