from __future__ import annotations

import unittest

from dsh_mcp_gateway import build_mcp_server
from dsh_mcp_gateway.chatgpt_web_companion import (
    BRIDGE_DSH_TOOL_NAME,
    COMPANION_HTML,
    COMPANION_RESOURCE_URI,
    COMPANION_TOOL_NAME,
    COMPANION_TOOL_NAME_V3,
    TRANSPORT_TOOL_NAME,
)


class _FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def tools(self):
        return []

    def skills(self):
        return []

    def load_skill(self, name):
        raise AssertionError(name)

    def call(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {
            "isError": False,
            "value": {"forwarded": {"name": name, "arguments": arguments}},
            "content": [],
        }


class ChatGPTWebCompanionTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_server_keeps_stable_four_tool_surface(self) -> None:
        server = build_mcp_server(_FakeBridge())
        self.assertEqual(
            {tool.name for tool in await server.list_tools()},
            {"dsh_tool_catalog", "dsh_tool_call", "dsh_skill_catalog", "dsh_skill_load"},
        )
        self.assertEqual(await server.list_resources(), [])

    async def test_companion_requires_a_real_harness_bridge(self) -> None:
        with self.assertRaisesRegex(ValueError, "enable_chatgpt_web_companion requires harness_bridge"):
            build_mcp_server(None, enable_chatgpt_web_companion=True)

    async def test_opt_in_companion_adds_model_ui_and_app_only_transport(self) -> None:
        bridge = _FakeBridge()
        server = build_mcp_server(bridge, enable_chatgpt_web_companion=True)
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(
            set(tools),
            {
                "dsh_tool_catalog",
                "dsh_tool_call",
                "dsh_skill_catalog",
                "dsh_skill_load",
                COMPANION_TOOL_NAME,
                COMPANION_TOOL_NAME_V3,
                TRANSPORT_TOOL_NAME,
            },
        )

        companion = tools[COMPANION_TOOL_NAME]
        self.assertEqual(companion.meta["ui"]["resourceUri"], COMPANION_RESOURCE_URI)
        self.assertEqual(COMPANION_RESOURCE_URI, "ui://dsh-chatgpt-web-bridge/companion-v3.html")
        self.assertEqual(companion.meta["ui"]["visibility"], ["model", "app"])
        companion_v3 = tools[COMPANION_TOOL_NAME_V3]
        self.assertEqual(companion_v3.meta["ui"]["resourceUri"], COMPANION_RESOURCE_URI)
        self.assertEqual(companion_v3.meta["ui"]["visibility"], ["model", "app"])
        transport = tools[TRANSPORT_TOOL_NAME]
        self.assertEqual(transport.meta["ui"]["resourceUri"], COMPANION_RESOURCE_URI)
        self.assertEqual(transport.meta["ui"]["visibility"], ["app"])

        resources = await server.list_resources()
        self.assertEqual(len(resources), 1)
        self.assertEqual(str(resources[0].uri), COMPANION_RESOURCE_URI)
        self.assertEqual(resources[0].mime_type, "text/html;profile=mcp-app")

        opened = await server.call_tool(COMPANION_TOOL_NAME, {})
        self.assertFalse(opened.is_error)
        self.assertEqual(opened.structured_content["status"], "armed")
        self.assertEqual(opened.structured_content["experiment"], "B1-auto-relay")

        relayed = await server.call_tool(
            TRANSPORT_TOOL_NAME,
            {"action": "poll", "client_id": "companion-test"},
        )
        self.assertFalse(relayed.is_error)
        self.assertEqual(
            bridge.calls,
            [(BRIDGE_DSH_TOOL_NAME, {"action": "poll", "client_id": "companion-test"})],
        )
        self.assertEqual(
            relayed.structured_content["value"]["forwarded"]["name"],
            BRIDGE_DSH_TOOL_NAME,
        )

    async def test_companion_html_is_automatic_official_host_relay(self) -> None:
        self.assertIn("ui/initialize", COMPANION_HTML)
        self.assertIn("ui/notifications/initialized", COMPANION_HTML)
        self.assertIn("ui/message", COMPANION_HTML)
        self.assertIn("tools/call", COMPANION_HTML)
        self.assertIn("capabilities.message", COMPANION_HTML)
        self.assertIn("capabilities.serverTools", COMPANION_HTML)
        self.assertIn(TRANSPORT_TOOL_NAME, COMPANION_HTML)
        self.assertIn("begin_send", COMPANION_HTML)
        self.assertIn("window.setTimeout", COMPANION_HTML)
        self.assertIn("automatic relay armed", COMPANION_HTML)
        self.assertIn("window.parent.postMessage", COMPANION_HTML)
        self.assertIn("resolveValidatedHostId", COMPANION_HTML)
        self.assertIn("status?.observers", COMPANION_HTML)
        self.assertIn("observer?.hostId === candidate", COMPANION_HTML)
        self.assertNotIn("event.source !== globalThis.parent", COMPANION_HTML)
        self.assertNotIn("Send follow-up probe", COMPANION_HTML)
        self.assertNotIn("<textarea", COMPANION_HTML)
        self.assertNotIn("setInterval", COMPANION_HTML)
        self.assertNotIn("api.openai.com", COMPANION_HTML)
        self.assertNotIn("workspace_agents", COMPANION_HTML)
        self.assertNotIn("Responses API", COMPANION_HTML)


if __name__ == "__main__":
    unittest.main()
