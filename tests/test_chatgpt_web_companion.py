from __future__ import annotations

import unittest

from dsh_mcp_gateway import build_mcp_server
from dsh_mcp_gateway.chatgpt_web_companion import (
    COMPANION_HTML,
    COMPANION_RESOURCE_URI,
    COMPANION_TOOL_NAME,
)


class _FakeBridge:
    def tools(self):
        return []

    def skills(self):
        return []

    def load_skill(self, name):
        raise AssertionError(name)

    def call(self, name, arguments=None):
        raise AssertionError((name, arguments))


class ChatGPTWebCompanionTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_server_keeps_stable_four_tool_surface(self) -> None:
        server = build_mcp_server(_FakeBridge())
        self.assertEqual(
            {tool.name for tool in await server.list_tools()},
            {"dsh_tool_catalog", "dsh_tool_call", "dsh_skill_catalog", "dsh_skill_load"},
        )
        self.assertEqual(await server.list_resources(), [])

    async def test_opt_in_companion_adds_one_ui_bound_probe_tool(self) -> None:
        server = build_mcp_server(_FakeBridge(), enable_chatgpt_web_companion=True)
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(
            set(tools),
            {
                "dsh_tool_catalog",
                "dsh_tool_call",
                "dsh_skill_catalog",
                "dsh_skill_load",
                COMPANION_TOOL_NAME,
            },
        )

        companion = tools[COMPANION_TOOL_NAME]
        self.assertEqual(companion.meta["ui"]["resourceUri"], COMPANION_RESOURCE_URI)
        self.assertEqual(companion.meta["ui"]["visibility"], ["model", "app"])

        resources = await server.list_resources()
        self.assertEqual(len(resources), 1)
        self.assertEqual(str(resources[0].uri), COMPANION_RESOURCE_URI)
        self.assertEqual(resources[0].mime_type, "text/html;profile=mcp-app")

        result = await server.call_tool(COMPANION_TOOL_NAME, {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["status"], "ready")
        self.assertEqual(result.structured_content["experiment"], "B1-ui-message")

    async def test_companion_html_is_self_contained_official_ui_message_probe(self) -> None:
        self.assertIn("ui/initialize", COMPANION_HTML)
        self.assertIn("ui/notifications/initialized", COMPANION_HTML)
        self.assertIn("ui/message", COMPANION_HTML)
        self.assertIn("hostCapabilities.message", COMPANION_HTML)
        self.assertIn("window.parent.postMessage", COMPANION_HTML)
        self.assertNotIn("api.openai.com", COMPANION_HTML)
        self.assertNotIn("workspace_agents", COMPANION_HTML)
        self.assertNotIn("Responses API", COMPANION_HTML)
        self.assertNotIn("setInterval", COMPANION_HTML)


if __name__ == "__main__":
    unittest.main()
