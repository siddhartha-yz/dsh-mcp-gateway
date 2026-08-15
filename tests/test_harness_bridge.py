from __future__ import annotations

import base64
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
            if request.full_url.endswith("/skills"):
                return _Response({"skills": [{"name": "community-review", "description": "review", "provider": "demo"}]})
            if request.full_url.endswith("/skill"):
                return _Response({"skill": {"name": "community-review", "description": "review", "provider": "demo", "content": "Review carefully."}})
            return _Response({"isError": False, "value": {"echo": "hi"}, "content": []})

        client = HarnessBridgeClient("http://127.0.0.1:3080", timeout_s=7)
        with patch("dsh_mcp_gateway.harness_bridge.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.tools()[0]["name"], "community_echo")
            self.assertEqual(client.skills()[0]["name"], "community-review")
            self.assertEqual(client.load_skill("community-review")["content"], "Review carefully.")
            result = client.call("community_echo", {"text": "hi"})

        self.assertFalse(result["isError"])
        self.assertEqual(seen[0][0], "http://127.0.0.1:3080/api/chatgpt-bridge/tools")
        self.assertEqual(seen[0][1], "GET")
        self.assertEqual(seen[1][0], "http://127.0.0.1:3080/api/chatgpt-bridge/skills")
        self.assertEqual(seen[1][1], "GET")
        self.assertEqual(seen[2][0], "http://127.0.0.1:3080/api/chatgpt-bridge/skill")
        self.assertEqual(json.loads(seen[2][2]), {"name": "community-review"})
        self.assertEqual(seen[3][1], "POST")
        self.assertEqual(json.loads(seen[3][2]), {"name": "community_echo", "arguments": {"text": "hi"}})

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

            def skills(self):
                return [{"name": "community-review", "description": "review", "provider": "demo"}]

            def load_skill(self, name):
                return {"name": name, "description": "review", "provider": "demo", "content": "Review carefully."}

            def call(self, name, arguments=None):
                return {"isError": False, "value": {"name": name, "arguments": arguments or {}}, "content": []}

        server = build_mcp_server(None, harness_bridge=FakeBridge())
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(
            set(tools),
            {"community_echo", "dsh_tool_catalog", "dsh_tool_call", "dsh_skill_catalog", "dsh_skill_load"},
        )
        self.assertEqual(tools["community_echo"].input_schema, {"type": "object"})
        self.assertEqual(tools["community_echo"].meta, {"dsh/projected": True})

        skill_catalog = await server.call_tool("dsh_skill_catalog", {})
        self.assertEqual(skill_catalog.structured_content["count"], 1)
        loaded_skill = await server.call_tool("dsh_skill_load", {"name": "community-review"})
        self.assertEqual(loaded_skill.structured_content["skill"]["content"], "Review carefully.")

        result = await server.call_tool("community_echo", {"text": "hello"})
        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {"value": {"name": "community_echo", "arguments": {"text": "hello"}}},
        )

    async def test_projected_dsh_image_content_reaches_mcp_without_a_bespoke_wrapper(self) -> None:
        class FakeBridge:
            def tools(self):
                return [{"name": "community_image", "description": "image", "parameters": {"type": "object"}}]

            def call(self, name, arguments=None):
                return {
                    "isError": False,
                    "value": {"name": name},
                    "content": [
                        {"type": "text", "text": "community image"},
                        {
                            "type": "image",
                            "data": base64.b64encode(b"fake-png").decode("ascii"),
                            "mediaType": "image/png",
                        },
                    ],
                }

        server = build_mcp_server(None, harness_bridge=FakeBridge())
        result = await server.call_tool("community_image", {})

        self.assertFalse(result.is_error)
        self.assertEqual([block.type for block in result.content], ["text", "image"])
        self.assertEqual(result.content[0].text, "community image")
        self.assertEqual(result.content[1].mime_type, "image/png")
        self.assertEqual(base64.b64decode(result.content[1].data), b"fake-png")

    async def test_dsh_additional_contexts_become_model_visible_mcp_content(self) -> None:
        class FakeBridge:
            def tools(self):
                return [{"name": "run_code", "description": "code", "parameters": {"type": "object"}}]

            def call(self, name, arguments=None):
                return {
                    "isError": False,
                    "value": {"name": name},
                    "content": [{"type": "text", "text": "outer result"}],
                    "additionalContexts": [
                        {
                            "role": "user",
                            "source": {"kind": "plugin", "plugin": "image-forwarder"},
                            "content": [
                                {"type": "text", "text": "forwarded image"},
                                {
                                    "type": "image",
                                    "data": base64.b64encode(b"forwarded-png").decode("ascii"),
                                    "mediaType": "image/png",
                                },
                            ],
                        }
                    ],
                }

        server = build_mcp_server(None, harness_bridge=FakeBridge())
        result = await server.call_tool("run_code", {})

        self.assertFalse(result.is_error)
        self.assertEqual([block.type for block in result.content], ["text", "text", "text", "image"])
        self.assertEqual(result.content[0].text, "outer result")
        self.assertEqual(result.content[1].text, "[DSH harness context from plugin image-forwarder]")
        self.assertEqual(result.content[2].text, "forwarded image")
        self.assertEqual(base64.b64decode(result.content[3].data), b"forwarded-png")
        self.assertEqual(result.structured_content, {"value": {"name": "run_code"}})

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
