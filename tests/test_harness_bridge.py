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
    async def test_harness_mode_exposes_only_generic_dsh_capability_bridge(self) -> None:
        class FakeBridge:
            def tools(self):
                return [{"name": "community_echo", "description": "echo", "parameters": {"type": "object"}}]

            def call(self, name, arguments=None):
                return {"isError": False, "value": {"name": name, "arguments": arguments or {}}, "content": []}

        server = build_mcp_server(None, harness_bridge=FakeBridge())
        names = {tool.name for tool in await server.list_tools()}
        self.assertEqual(names, {"dsh_tool_catalog", "dsh_tool_call"})


if __name__ == "__main__":
    unittest.main()
