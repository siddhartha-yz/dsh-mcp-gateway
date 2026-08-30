from __future__ import annotations

import asyncio
import base64
import json
import unittest
from http.client import BadStatusLine
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from dsh_mcp_gateway import build_mcp_server
from dsh_mcp_gateway.harness_bridge import (
    HarnessBridgeClient,
    HarnessBridgeError,
    watch_tool_catalog,
)


class _Response:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class HarnessBridgeClientTests(unittest.TestCase):
    def test_loopback_is_required_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            HarnessBridgeClient("http://example.com:3080")

    def test_origin_params_are_rejected_during_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "without path, params"):
            HarnessBridgeClient("http://127.0.0.1:3080/;tenant=bad")

    def test_invalid_port_is_rejected_during_construction(self) -> None:
        for base_url in ("http://127.0.0.1:notaport", "http://127.0.0.1:99999"):
            with self.subTest(base_url=base_url), self.assertRaisesRegex(ValueError, "invalid port"):
                HarnessBridgeClient(base_url)

    def test_catalog_and_call_use_generic_bridge_endpoints(self) -> None:
        seen = []

        def fake_urlopen(request, timeout):
            seen.append((request.full_url, request.method, request.data, timeout))
            if request.full_url.endswith("/tools"):
                return _Response({"tools": [{"name": "community_echo", "description": "echo", "parameters": {}}]})
            if request.full_url.endswith("/skills"):
                return _Response({"skills": [{"name": "community-review", "description": "review", "source": "runtime", "provider": "demo"}]})
            if request.full_url.endswith("/skill"):
                return _Response({"skill": {"name": "community-review", "description": "review", "source": "runtime", "provider": "demo", "content": "Review carefully."}})
            return _Response({"isError": False, "value": {"echo": "hi"}, "content": []})

        client = HarnessBridgeClient(
            "http://127.0.0.1:3080",
            timeout_s=7,
            tool_call_timeout_s=130,
        )
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
        self.assertEqual([entry[3] for entry in seen[:3]], [7, 7, 7])
        self.assertEqual(seen[3][3], 130)

    def test_malformed_skill_catalog_entries_are_rejected(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        malformed = (
            ({"name": "skill", "description": "desc", "provider": "demo"}, "valid source"),
            ({"name": "skill", "description": 3, "source": "runtime", "provider": "demo"}, "string description"),
            ({"name": "skill", "description": "desc", "source": "runtime", "provider": "demo", "whenToUse": 3}, "whenToUse"),
            ({"name": "skill", "description": "desc", "source": "runtime", "provider": "demo", "resourceBase": "bad"}, "resourceBase"),
        )
        for item, expected in malformed:
            with (
                self.subTest(item=item),
                patch("dsh_mcp_gateway.harness_bridge.urlopen", return_value=_Response({"skills": [item]})),
                self.assertRaisesRegex(HarnessBridgeError, expected),
            ):
                client.skills()

    def test_malformed_skill_definition_is_rejected(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        payload = {
            "skill": {
                "name": "community-review",
                "description": "review",
                "source": "runtime",
                "provider": "demo",
                "content": 3,
            }
        }
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", return_value=_Response(payload)),
            self.assertRaisesRegex(HarnessBridgeError, "invalid skill definition"),
        ):
            client.load_skill("community-review")

    def test_malformed_tool_execution_results_are_rejected(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        malformed = (
            ({"content": []}, "boolean isError"),
            ({"isError": "false", "content": [], "value": None}, "boolean isError"),
            ({"isError": False, "value": 1}, "content list"),
            ({"isError": False, "content": []}, "without a value"),
            ({"isError": False, "content": [], "value": 1, "error": {}}, "with an error"),
            ({"isError": True, "content": [], "error": {"message": "failed"}, "value": 1}, "with a value"),
            ({"isError": True, "content": [], "error": {}}, "invalid failed tool result"),
            ({"isError": True, "content": [], "error": {"message": "failed"}, "additionalContexts": {}}, "additionalContexts"),
        )
        for payload, expected in malformed:
            with (
                self.subTest(payload=payload),
                patch("dsh_mcp_gateway.harness_bridge.urlopen", return_value=_Response(payload)),
                self.assertRaisesRegex(HarnessBridgeError, expected),
            ):
                client.call("community_echo", {})

    def test_non_positive_tool_call_timeout_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "tool_call_timeout_s"):
            HarnessBridgeClient("http://127.0.0.1:3080", tool_call_timeout_s=0)

    def test_tools_can_override_transport_timeout_for_readiness(self) -> None:
        seen = []

        def fake_urlopen(request, timeout):
            seen.append(timeout)
            return _Response({"tools": []})

        client = HarnessBridgeClient("http://127.0.0.1:3080", timeout_s=30)
        with patch("dsh_mcp_gateway.harness_bridge.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.tools(timeout_s=1.0), [])

        self.assertEqual(seen, [1.0])
        with self.assertRaisesRegex(ValueError, "timeout_s"):
            client.tools(timeout_s=0)

    def test_tool_revision_uses_bridge_revision_endpoint(self) -> None:
        seen = []

        def fake_urlopen(request, timeout):
            seen.append((request.full_url, request.method, timeout))
            return _Response({"instanceId": "bridge-a", "toolRevision": 7, "skillRevision": 3})

        client = HarnessBridgeClient("http://127.0.0.1:3080", timeout_s=4)
        with patch("dsh_mcp_gateway.harness_bridge.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.tool_revision(), 7)

        self.assertEqual(seen, [("http://127.0.0.1:3080/api/chatgpt-bridge/revision", "GET", 4)])

    def test_tool_revision_token_includes_bridge_instance(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with patch(
            "dsh_mcp_gateway.harness_bridge.urlopen",
            return_value=_Response({"instanceId": "bridge-a", "toolRevision": 7, "skillRevision": 3}),
        ):
            self.assertEqual(client.tool_revision_token(), ("bridge-a", 7))

    def test_invalid_bridge_instance_id_is_rejected_for_revision_token(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with (
            patch(
                "dsh_mcp_gateway.harness_bridge.urlopen",
                return_value=_Response({"toolRevision": 7, "skillRevision": 3}),
            ),
            self.assertRaisesRegex(HarnessBridgeError, "invalid instance id"),
        ):
            client.tool_revision_token()

    def test_invalid_tool_revision_is_rejected(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", return_value=_Response({"toolRevision": "bad"})),
            self.assertRaises(HarnessBridgeError),
        ):
            client.tool_revision()

    def test_invalid_catalog_is_rejected(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", return_value=_Response({"tools": "bad"})),
            self.assertRaises(HarnessBridgeError),
        ):
            client.tools()

        malformed = (
            ({"description": "missing name", "parameters": {}}, "valid name"),
            ({"name": "tool", "description": 3, "parameters": {}}, "non-string description"),
            ({"name": "tool", "description": "desc", "parameters": []}, "non-object parameter schema"),
        )
        for item, expected in malformed:
            with (
                self.subTest(item=item),
                patch("dsh_mcp_gateway.harness_bridge.urlopen", return_value=_Response({"tools": [item]})),
                self.assertRaisesRegex(HarnessBridgeError, expected),
            ):
                client.tools()

    def test_call_rejects_non_object_arguments_without_transport(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen") as mocked,
            self.assertRaisesRegex(ValueError, "arguments must be an object"),
        ):
            client.call("community_echo", [])  # type: ignore[arg-type]
        mocked.assert_not_called()

    def test_malformed_http_is_wrapped_as_bridge_error(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", side_effect=BadStatusLine("garbled status")),
            self.assertRaisesRegex(HarnessBridgeError, "unavailable"),
        ):
            client.tools()

    def test_http_error_arbitrary_body_is_not_reflected(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        error = HTTPError(
            "http://127.0.0.1:3080/api/chatgpt-bridge/tools",
            500,
            "Internal Server Error",
            {},
            BytesIO(b"<html>/private/workspace secret detail</html>"),
        )
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", side_effect=error),
            self.assertRaises(HarnessBridgeError) as caught,
        ):
            client.tools()
        message = str(caught.exception)
        self.assertIn("HTTP 500", message)
        self.assertIn("non-JSON error body", message)
        self.assertNotIn("/private/workspace", message)
        self.assertNotIn("secret detail", message)

    def test_http_error_exposes_only_recognized_structured_public_detail(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        error = HTTPError(
            "http://127.0.0.1:3080/api/chatgpt-bridge/skill",
            404,
            "Not Found",
            {},
            BytesIO(json.dumps({
                "error": "skill_unavailable",
                "message": 'skill "missing" is unavailable for model invocation',
            }).encode()),
        )
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", side_effect=error),
            self.assertRaisesRegex(
                HarnessBridgeError,
                'HTTP 404: skill_unavailable: skill "missing" is unavailable for model invocation',
            ),
        ):
            client.load_skill("missing")

    def test_http_error_internal_bridge_message_is_suppressed(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        error = HTTPError(
            "http://127.0.0.1:3080/api/chatgpt-bridge/call",
            500,
            "Internal Server Error",
            {},
            BytesIO(json.dumps({
                "error": "bridge_error",
                "message": "/private/workspace persistence token=secret",
            }).encode()),
        )
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", side_effect=error),
            self.assertRaises(HarnessBridgeError) as caught,
        ):
            client.call("calculator", {"expression": "1+1"})
        message = str(caught.exception)
        self.assertEqual(message, "DSH bridge HTTP 500: bridge_error")
        self.assertNotIn("/private/workspace", message)
        self.assertNotIn("secret", message)

    def test_http_error_body_read_failure_stays_wrapped_as_bridge_error(self) -> None:
        class BrokenErrorBody:
            def __init__(self) -> None:
                self.closed = False

            def read(self, *_args, **_kwargs):
                raise TimeoutError("timed out while reading HTTP error body")

            def close(self) -> None:
                self.closed = True

        client = HarnessBridgeClient("http://127.0.0.1:3080")
        body = BrokenErrorBody()
        error = HTTPError(
            "http://127.0.0.1:3080/api/chatgpt-bridge/tools",
            502,
            "Bad Gateway",
            {},
            body,
        )
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", side_effect=error),
            self.assertRaisesRegex(HarnessBridgeError, "HTTP 502"),
        ):
            client.tools()
        self.assertTrue(body.closed)

    def test_non_utf8_bridge_response_is_wrapped_as_bridge_error(self) -> None:
        class NonUtf8Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size: int = -1) -> bytes:
                return b"\xff"

        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", return_value=NonUtf8Response()),
            self.assertRaisesRegex(HarnessBridgeError, "non-JSON"),
        ):
            client.tools()

    def test_oversized_bridge_request_is_rejected_before_transport(self) -> None:
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen") as mocked,
            self.assertRaisesRegex(HarnessBridgeError, "request exceeds 1000000 bytes"),
        ):
            client.call("community_echo", {"text": "x" * 1_000_000})

        mocked.assert_not_called()

    def test_oversized_bridge_response_is_rejected_before_full_read(self) -> None:
        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size: int = -1) -> bytes:
                self.requested_size = size
                return b"x" * size

        response = OversizedResponse()
        client = HarnessBridgeClient("http://127.0.0.1:3080")
        with (
            patch("dsh_mcp_gateway.harness_bridge.urlopen", return_value=response),
            self.assertRaisesRegex(HarnessBridgeError, "response exceeds"),
        ):
            client.tools()

        self.assertEqual(response.requested_size, 16 * 1024 * 1024 + 1)


class HarnessCatalogWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_watcher_publishes_only_after_dsh_tool_revision_changes(self) -> None:
        class FakeBridge:
            def __init__(self):
                self.revisions = [4, 4, 5, 5]
                self.index = 0

            def tool_revision_token(self):
                value = self.revisions[min(self.index, len(self.revisions) - 1)]
                self.index += 1
                return "bridge-a", value

        changed = asyncio.Event()
        publishes = 0

        async def publish_changed():
            nonlocal publishes
            publishes += 1
            changed.set()

        task = asyncio.create_task(watch_tool_catalog(FakeBridge(), publish_changed, interval_s=0.001))
        try:
            await asyncio.wait_for(changed.wait(), timeout=1)
            await asyncio.sleep(0.005)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(publishes, 1)

    async def test_watcher_publishes_when_bridge_restarts_with_same_numeric_revision(self) -> None:
        class FakeBridge:
            def __init__(self):
                self.tokens = [("bridge-a", 4), ("bridge-b", 4), ("bridge-b", 4)]
                self.index = 0

            def tool_revision_token(self):
                value = self.tokens[min(self.index, len(self.tokens) - 1)]
                self.index += 1
                return value

        changed = asyncio.Event()
        publishes = 0

        async def publish_changed():
            nonlocal publishes
            publishes += 1
            changed.set()

        task = asyncio.create_task(watch_tool_catalog(FakeBridge(), publish_changed, interval_s=0.001))
        try:
            await asyncio.wait_for(changed.wait(), timeout=1)
            await asyncio.sleep(0.005)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(publishes, 1)


class ServerRuntimeModeBoundaryTests(unittest.TestCase):
    def test_server_builder_rejects_mixed_runtime_authorities(self) -> None:
        cases = (
            (object(), object(), None),
            (object(), None, object()),
            (None, object(), object()),
        )
        for index, (service, session_runtime, harness_bridge) in enumerate(cases):
            with self.subTest(case=index), self.assertRaisesRegex(
                ValueError,
                "mutually exclusive runtime modes",
            ):
                build_mcp_server(
                    service,
                    session_runtime=session_runtime,
                    harness_bridge=harness_bridge,
                )

    def test_projected_surface_requires_harness_bridge(self) -> None:
        with self.assertRaisesRegex(ValueError, "project_dsh_tools requires harness_bridge"):
            build_mcp_server(None, project_dsh_tools=True)


class HarnessBridgeMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_meta_tools_unlock_new_plugin_without_tools_list_refresh(self) -> None:
        class FakeBridge:
            def __init__(self):
                self.catalog = [{"name": "first", "description": "first", "parameters": {"type": "object"}}]

            def tools(self):
                return list(self.catalog)

            def skills(self):
                return []

            def load_skill(self, name):
                raise AssertionError(name)

            def call(self, name, arguments=None):
                return {
                    "isError": False,
                    "value": {"name": name, "arguments": arguments or {}},
                    "content": [{"type": "text", "text": f"called {name}"}],
                }

        bridge = FakeBridge()
        server = build_mcp_server(None, harness_bridge=bridge)

        # Simulate ChatGPT approving/caching one initial MCP tool snapshot.
        initial_snapshot = {tool.name for tool in await server.list_tools()}
        self.assertEqual(
            initial_snapshot,
            {"dsh_tool_catalog", "dsh_tool_call", "dsh_skill_catalog", "dsh_skill_load"},
        )
        capabilities = server._lowlevel_server.get_capabilities(protocol_version="2026-07-28")
        self.assertFalse(capabilities.tools.list_changed)

        # A DSH community plugin appears later. Do not refresh tools/list.
        bridge.catalog.append({"name": "second", "description": "second", "parameters": {"type": "object"}})

        # Even if the client asks for tools/list again, meta-only mode keeps the
        # ChatGPT-facing schema frozen to the same four generic tools.
        refreshed_snapshot = {tool.name for tool in await server.list_tools()}
        self.assertEqual(refreshed_snapshot, initial_snapshot)

        catalog = await server.call_tool("dsh_tool_catalog", {})
        self.assertEqual([item["name"] for item in catalog.structured_content["tools"]], ["first", "second"])

        result = await server.call_tool(
            "dsh_tool_call",
            {"name": "second", "arguments": {"value": 7}},
        )
        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, "called second")
        self.assertEqual(
            result.structured_content,
            {"value": {"name": "second", "arguments": {"value": 7}}},
        )

    async def test_stable_meta_tools_unlock_hot_added_skill_without_tools_list_refresh(self) -> None:
        class FakeBridge:
            def __init__(self):
                self.skill_catalog = []

            def tools(self):
                return []

            def skills(self):
                return list(self.skill_catalog)

            def load_skill(self, name):
                match = next((skill for skill in self.skill_catalog if skill["name"] == name), None)
                if match is None:
                    raise AssertionError(name)
                return {
                    **match,
                    "content": "Build a red-capable feedback loop before forming a root-cause theory.",
                }

            def call(self, name, arguments=None):
                raise AssertionError((name, arguments))

        bridge = FakeBridge()
        server = build_mcp_server(None, harness_bridge=bridge)

        initial_snapshot = {tool.name for tool in await server.list_tools()}
        self.assertEqual(
            initial_snapshot,
            {"dsh_tool_catalog", "dsh_tool_call", "dsh_skill_catalog", "dsh_skill_load"},
        )
        capabilities = server._lowlevel_server.get_capabilities(protocol_version="2026-07-28")
        self.assertFalse(capabilities.tools.list_changed)

        initial_catalog = await server.call_tool("dsh_skill_catalog", {})
        self.assertEqual(initial_catalog.structured_content, {"skills": [], "count": 0})

        # Simulate DSH SkillFilesystem hot-discovering a new community SKILL.md
        # after ChatGPT has already approved/cached the four-tool MCP surface.
        bridge.skill_catalog.append(
            {
                "name": "diagnosing-bugs",
                "description": "Diagnosis loop for hard bugs.",
                "provider": "filesystem",
                "source": "user-dsh",
            }
        )

        refreshed_snapshot = {tool.name for tool in await server.list_tools()}
        self.assertEqual(refreshed_snapshot, initial_snapshot)

        catalog = await server.call_tool("dsh_skill_catalog", {})
        self.assertEqual(catalog.structured_content["count"], 1)
        self.assertEqual(catalog.structured_content["skills"][0]["name"], "diagnosing-bugs")

        loaded = await server.call_tool("dsh_skill_load", {"name": "diagnosing-bugs"})
        self.assertIn("red-capable feedback loop", loaded.structured_content["skill"]["content"])

    async def test_meta_tool_call_preserves_native_image_content(self) -> None:
        class FakeBridge:
            def tools(self):
                return []

            def skills(self):
                return []

            def load_skill(self, name):
                raise AssertionError(name)

            def call(self, name, arguments=None):
                return {
                    "isError": False,
                    "value": {"name": name},
                    "content": [
                        {"type": "text", "text": "meta image"},
                        {
                            "type": "image",
                            "data": base64.b64encode(b"meta-png").decode("ascii"),
                            "mediaType": "image/png",
                        },
                    ],
                }

        server = build_mcp_server(None, harness_bridge=FakeBridge())
        result = await server.call_tool("dsh_tool_call", {"name": "community_image", "arguments": {}})

        self.assertFalse(result.is_error)
        self.assertEqual([block.type for block in result.content], ["text", "image"])
        self.assertEqual(result.content[0].text, "meta image")
        self.assertEqual(base64.b64decode(result.content[1].data), b"meta-png")

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

        server = build_mcp_server(None, harness_bridge=FakeBridge(), project_dsh_tools=True)
        capabilities = server._lowlevel_server.get_capabilities(protocol_version="2026-07-28")
        self.assertTrue(capabilities.tools.list_changed)

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

        server = build_mcp_server(None, harness_bridge=FakeBridge(), project_dsh_tools=True)
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

        server = build_mcp_server(None, harness_bridge=FakeBridge(), project_dsh_tools=True)
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
        server = build_mcp_server(None, harness_bridge=bridge, project_dsh_tools=True)
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

        server = build_mcp_server(None, harness_bridge=FakeBridge(), project_dsh_tools=True)
        tools = [tool for tool in await server.list_tools() if tool.name == "dsh_tool_catalog"]
        self.assertEqual(len(tools), 1)
        result = await server.call_tool("dsh_tool_catalog", {})
        self.assertEqual(result.structured_content["count"], 1)


if __name__ == "__main__":
    unittest.main()
