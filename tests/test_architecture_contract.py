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
        self.assertIn("stable set of meta-tools", contract)
        self.assertIn("must not depend on dynamic first-class tool refresh", contract)
        self.assertIn("default gateway mode is **meta-only**", contract)

        self.assertIn("AGENTS.md", readme)
        self.assertIn("Give ChatGPT Web a mature DSH Harness", readme)
        self.assertIn("AGENTS.md", architecture)
        self.assertIn("give ChatGPT Web a mature DSH Harness", architecture)

    def test_legacy_gateway_runtime_modules_are_removed(self) -> None:
        package = ROOT / "src" / "dsh_mcp_gateway"
        for name in ("backend.py", "routing.py", "session_runtime.py", "types.py"):
            with self.subTest(name=name):
                self.assertFalse((package / name).exists())

        import dsh_mcp_gateway

        for name in ("PublicSdkBridge", "GatewayService", "DurableSessionRuntime"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(dsh_mcp_gateway, name))

    def test_dsh_bridge_uses_native_tool_runtime_seam(self) -> None:
        plugin = (ROOT / "dsh-bridge-plugin" / "index.js").read_text(encoding="utf-8")
        profile = (ROOT / "dsh-bridge-plugin" / "chatgpt-capability-profile.js").read_text(encoding="utf-8")
        task_plugin = (ROOT / "dsh-task-state-plugin" / "index.js").read_text(encoding="utf-8")
        task_store = (ROOT / "dsh-task-state-plugin" / "task-store.js").read_text(encoding="utf-8")
        gui_bridge = (ROOT / "dsh-chatgpt-web-bridge-plugin" / "index.js").read_text(encoding="utf-8")
        continuation = (ROOT / "dsh-chatgpt-web-bridge-plugin" / "continuation-controller.js").read_text(encoding="utf-8")
        observer = (ROOT / "browser-extension" / "dsh-chatgpt-web-observer" / "chatgpt-observer.js").read_text(encoding="utf-8")
        observer_background = (ROOT / "browser-extension" / "dsh-chatgpt-web-observer" / "background.js").read_text(encoding="utf-8")
        companion = (ROOT / "src" / "dsh_mcp_gateway" / "chatgpt_web_companion.py").read_text(encoding="utf-8")
        gateway_bridge = (ROOT / "src" / "dsh_mcp_gateway" / "harness_bridge.py").read_text(encoding="utf-8")
        overlay = (ROOT / "deploy" / "dsh" / "chatgpt-bridge.cordis.yml").read_text(encoding="utf-8")

        self.assertIn("'agents', 'agentPresets'", plugin)
        self.assertIn("presets.standingKeyFor(presetId)", plugin)
        self.assertIn("ctx.tools.schemas(scope)", plugin)
        self.assertIn("tools: externalToolSchemas(scope)", plugin)
        self.assertNotIn("tools: ctx.tools.schemas(scope)", plugin)
        self.assertIn("await assertExternalToolAvailable(toolName)", plugin)
        self.assertIn("buildChatGPTCapabilityProfile", plugin)
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
        self.assertIn("{ instanceId, toolRevision, skillRevision, capabilityProfile: capabilityProfile.id }", plugin)
        self.assertNotIn("DEEPSEEK_API_KEY", plugin)
        self.assertIn("chatgpt-external-v1", profile)
        self.assertIn("DEFAULT_CHATGPT_TOOL_NAMES", profile)
        self.assertIn("REVIEW_REQUIRED_DSH_AGENT_TOOL_NAMES", profile)
        self.assertIn("'workflow'", profile)
        self.assertIn("DEDICATED_CHATGPT_SURFACE_TOOL_NAMES", profile)
        self.assertIn("'skill'", profile)
        self.assertNotIn("DEFAULT_CHATGPT_TOOL_NAMES", gateway_bridge)
        self.assertNotIn("REVIEW_REQUIRED_DSH_AGENT_TOOL_NAMES", gateway_bridge)

        self.assertIn("export const inject = ['storageDomain', 'tools']", task_plugin)
        self.assertIn("ctx.storageDomain.open(taskDomainSpec)", task_plugin)
        self.assertIn("ctx.tools.register(createTaskStateTool(store))", task_plugin)
        self.assertIn("TASK_STATE_READ_SERVICE", task_plugin)
        self.assertIn("ctx.provide(TASK_STATE_READ_SERVICE", task_plugin)
        self.assertIn("includeHistory: false", task_plugin)
        self.assertIn("name: 'task_state'", task_plugin)
        self.assertIn("layout: 'per-record'", task_plugin)
        self.assertNotIn("ctx.llm", task_plugin)
        self.assertNotIn("ctx.get('agents')", task_plugin)
        self.assertNotIn("ctx.get(\"agents\")", task_plugin)
        self.assertNotIn("sessionPersistence", task_plugin)
        self.assertNotIn("app.sendMessage", task_plugin)
        self.assertNotIn("fetch(", task_store)
        self.assertNotIn("child_process", task_store)

        self.assertIn("name: 'chatgpt_web_bridge'", gui_bridge)
        self.assertIn("begin_send", gui_bridge)
        self.assertIn("ContinuationController", gui_bridge)
        self.assertIn("TASK_STATE_READ_SERVICE", gui_bridge)
        self.assertIn("/controller", gui_bridge)
        self.assertNotIn("ctx.llm", gui_bridge)
        self.assertNotIn("child_process", gui_bridge)
        self.assertIn("task_state_not_advanced", continuation)
        self.assertIn("continuationMessage", continuation)
        self.assertIn("lastContinuedTurnKey", continuation)
        self.assertNotIn("ctx.llm", continuation)
        self.assertNotIn("fetch(", continuation)
        self.assertNotIn("api.openai.com", continuation)
        self.assertIn("ui/message", companion)
        self.assertIn('visibility=["app"]', companion)
        self.assertIn("chatgpt_web_bridge_transport", companion)
        self.assertNotIn("api.openai.com", companion)
        self.assertNotIn("workspace_agents", companion)

        self.assertIn("MutationObserver", observer)
        self.assertIn('data-message-author-role="assistant"', observer)
        self.assertIn("bridge_degraded", observer)
        self.assertIn("observer_heartbeat", observer)
        self.assertNotIn(".click(", observer)
        self.assertNotIn(".submit(", observer)
        self.assertNotIn("fetch(", observer)
        self.assertNotIn("XMLHttpRequest", observer)
        self.assertNotIn("api.openai.com", observer)
        self.assertIn("https://chatgpt.com", observer_background)
        self.assertNotIn("fetch(", observer_background)
        self.assertNotIn("scripting.executeScript", observer_background)

        gateway_unit = (ROOT / "deploy" / "systemd" / "dsh-mcp-gateway.service").read_text(encoding="utf-8")
        dsh_unit = (ROOT / "deploy" / "systemd" / "dsh-web-host.service").read_text(encoding="utf-8")
        dsh_env = (ROOT / "deploy" / "systemd" / "dsh.env.example").read_text(encoding="utf-8")
        self.assertIn("--dsh-harness-url http://127.0.0.1:3080", gateway_unit)
        self.assertIn("--tool-surface meta-only", gateway_unit)
        self.assertNotIn("--dsh-web-url", gateway_unit)
        self.assertIn("--patch /srv/dsh-mcp-gateway/deploy/dsh/chatgpt-bridge.cordis.yml", dsh_unit)
        self.assertNotIn("DEEPSEEK_API_KEY", dsh_env)
        self.assertIn("dsh-task-state-plugin/index.js", overlay)
        self.assertIn("dsh-chatgpt-web-bridge-plugin/index.js", overlay)
        self.assertIn("dsh-bridge-plugin/index.js", overlay)
        self.assertIn("allowExtraTools:", overlay)
        self.assertIn("- task_state", overlay)
        self.assertIn("- chatgpt_web_bridge", overlay)


if __name__ == "__main__":
    unittest.main()
