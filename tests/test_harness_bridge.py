from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from dsh_mcp_gateway import build_mcp_server
from dsh_mcp_gateway.harness_bridge import HarnessBridgeClient, HarnessBridgeError


class _Response:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class HarnessBridgeClientTests(unittest.TestCase):
    def test_loopback_is_required_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            HarnessBridgeClient("http://example.com:3080")

    def test_catalog_and_call_use_generic_bridge_endpoints(self) -> None:
        seen = []

        def fake_urlopen(request, timeout):
            seen.append((request.full_url, request.method, request.data, timeout))
            if request.full_url.endswith("/tools"):
                return _Response({"tools": [{"name": "community_echo", "description": "echo", "parameters": {}}]})
            return _Response({"isError": False, "value": {"echo": "hi"}, "content": []})

        client = HarnessBridgeClient("http://127.0.0.1:3080", timeout_s=7)
        with patch("dsh_mcp_gateway.harness_bridge.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.tools()[0]["name"], "community_echo")
            result = client.call("community_echo", {"text": "hi"})

        self.assertFalse(result["isError"])
        self.assertEqual(seen[0][0], "http://127.0.0.1:3080/api/chatgpt-bridge/tools")
        self.assertEqual(seen[0][1], "GET")
        self.assertEqual(seen[1][1], "POST")
        self.assertEqual(json.loads(seen[1][2]), {"name": "community_echo", "arguments": {"text": "hi"}})

    def test_invalid_catalog_is_rejected(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", return_value=_Response({"tools": "bad"})),
            self.assertRaises(HarnessBridgeError),
        ):
            client.tools()


class HarnessBridgeMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_harness_mode_projects_dsh_tools_as_first_class_mcp_tools(self) -> None:
        class FakeBridge:
            def tools(self):
                return [{"name": "community_echo", "description": "echo", "parameters": {"type": "object"}}]

            def call(self, name, arguments=None):
                return {"isError": False, "value": {"name": name, "arguments": arguments or {}}, "content": []}

        server = build_mcp_server(None, harness_bridge=FakeBridge())
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(set(tools), {"community_echo", "dsh_tool_catalog", "dsh_tool_call"})
        self.assertEqual(tools["community_echo"].input_schema, {"type": "object"})
        self.assertEqual(tools["community_echo"].meta, {"dsh/projected": True})

        result = await server.call_tool("community_echo", {"text": "hello"})
        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {"value": {"name": "community_echo", "arguments": {"text": "hello"}}},
        )

    async def test_projected_catalog_is_dynamic_without_gateway_restart(self) -> None:
        class FakeBridge:
            def __init__(self):
                self.catalog = [{"name": "first", "description": "first", "parameters": {"type": "object"}}]

            def tools(self):
                return list(self.catalog)

            def call(self, name, arguments=None):
                return {"isError": False, "value": name, "content": []}

        bridge = FakeBridge()
        server = build_mcp_server(None, harness_bridge=bridge)
        first = {tool.name for tool in await server.list_tools()}
        self.assertIn("first", first)
        self.assertNotIn("second", first)

        bridge.catalog.append({"name": "second", "description": "second", "parameters": {"type": "object"}})
        second = {tool.name for tool in await server.list_tools()}
        self.assertIn("second", second)

    async def test_gateway_tool_names_win_over_dsh_projection_collisions(self) -> None:
        class FakeBridge:
            def tools(self):
                return [
                    {
                        "name": "dsh_tool_catalog",
                        "description": "should not shadow gateway catalog",
                        "parameters": {"type": "object"},
                    }
                ]

            def call(self, name, arguments=None):
                raise AssertionError("reserved gateway tool must not dispatch to DSH")

        server = build_mcp_server(None, harness_bridge=FakeBridge())
        tools = [tool for tool in await server.list_tools() if tool.name == "dsh_tool_catalog"]
        self.assertEqual(len(tools), 1)
        result = await server.call_tool("dsh_tool_catalog", {})
        self.assertEqual(result.structured_content["count"], 1)


if __name__ == "__main__":
    unittest.main()
