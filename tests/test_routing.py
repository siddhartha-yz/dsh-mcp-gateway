from __future__ import annotations

import importlib.util
import unittest
from typing import Any

from dsh_mcp_gateway import build_mcp_server
from dsh_mcp_gateway.routing import EnsureAction, GatewayService, SessionRouter
from dsh_mcp_gateway.types import SessionHandle, SessionPresence


class FakeBackend:
    def __init__(self, presence: SessionPresence) -> None:
        self._presence = presence
        self.calls: list[tuple[str, str | None]] = []
        self.fail_resume = False

    def presence(self, session_id: str) -> SessionPresence:
        self.calls.append(("presence", session_id))
        return self._presence

    def reuse(self, session_id: str) -> SessionHandle:
        self.calls.append(("reuse", session_id))
        return SessionHandle(session_id)

    def resume(self, session_id: str) -> SessionHandle:
        self.calls.append(("resume", session_id))
        if self.fail_resume:
            raise RuntimeError("resume unavailable")
        return SessionHandle(session_id)

    def create(self, session_id: str | None = None) -> SessionHandle:
        self.calls.append(("create", session_id))
        return SessionHandle(session_id or "generated-session")

    def prompt(self, session_id: str, text: str) -> str:
        self.calls.append(("prompt", session_id, text))
        return "message-1"

    def status(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("status", session_id))
        return {"session_id": session_id, "status": "idle"}

    def history(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self.calls.append(("history", session_id, limit))
        return [{"type": "turn/end"}]

    def list_sessions(self) -> list[dict[str, Any]]:
        self.calls.append(("list_sessions",))
        return [{"session_id": "s1"}]

    def cancel(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", session_id))
        return {"session_id": session_id, "canceled": True}


class SessionRouterTests(unittest.TestCase):
    def test_reuses_live_session(self) -> None:
        backend = FakeBackend(SessionPresence.LIVE)
        result = SessionRouter(backend).ensure("s1")
        self.assertEqual(result.action, EnsureAction.REUSED)
        self.assertEqual(backend.calls, [("presence", "s1"), ("reuse", "s1")])

    def test_resumes_persisted_session(self) -> None:
        backend = FakeBackend(SessionPresence.PERSISTED)
        result = SessionRouter(backend).ensure("s1")
        self.assertEqual(result.action, EnsureAction.RESUMED)
        self.assertEqual(backend.calls, [("presence", "s1"), ("resume", "s1")])

    def test_creates_absent_requested_session(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        result = SessionRouter(backend).ensure("s1")
        self.assertEqual(result.action, EnsureAction.CREATED)
        self.assertEqual(backend.calls, [("presence", "s1"), ("create", "s1")])

    def test_creates_generated_session_when_no_id_requested(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        result = SessionRouter(backend).ensure()
        self.assertEqual(result.handle.session_id, "generated-session")
        self.assertEqual(backend.calls, [("create", None)])

    def test_resume_failure_never_falls_back_to_create(self) -> None:
        backend = FakeBackend(SessionPresence.PERSISTED)
        backend.fail_resume = True
        with self.assertRaisesRegex(RuntimeError, "resume unavailable"):
            SessionRouter(backend).ensure("s1")
        self.assertEqual(backend.calls, [("presence", "s1"), ("resume", "s1")])


class GatewayServiceTests(unittest.TestCase):
    def test_start_creates_then_prompts(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        receipt = GatewayService(backend).start("do work", session_id="s1")
        self.assertEqual(receipt.session_id, "s1")
        self.assertEqual(receipt.action, EnsureAction.CREATED.value)
        self.assertEqual(receipt.message_id, "message-1")
        self.assertEqual(
            backend.calls,
            [("presence", "s1"), ("create", "s1"), ("prompt", "s1", "do work")],
        )

    def test_start_without_id_uses_generated_handle(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        receipt = GatewayService(backend).start("do work")
        self.assertEqual(receipt.session_id, "generated-session")
        self.assertEqual(backend.calls, [("create", None), ("prompt", "generated-session", "do work")])

    def test_persisted_session_resumes_before_prompt(self) -> None:
        backend = FakeBackend(SessionPresence.PERSISTED)
        receipt = GatewayService(backend).continue_session("s1", "continue")
        self.assertEqual(receipt.action, EnsureAction.RESUMED.value)
        self.assertEqual(
            backend.calls,
            [("presence", "s1"), ("resume", "s1"), ("prompt", "s1", "continue")],
        )

    def test_resume_failure_never_sends_prompt(self) -> None:
        backend = FakeBackend(SessionPresence.PERSISTED)
        backend.fail_resume = True
        with self.assertRaisesRegex(RuntimeError, "resume unavailable"):
            GatewayService(backend).continue_session("s1", "continue")
        self.assertEqual(backend.calls, [("presence", "s1"), ("resume", "s1")])

    def test_observation_and_cancel_delegate_without_routing(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        service = GatewayService(backend)
        self.assertEqual(service.status("s1")["status"], "idle")
        self.assertEqual(service.history("s1", limit=5)[0]["type"], "turn/end")
        self.assertEqual(service.list_sessions()[0]["session_id"], "s1")
        self.assertTrue(service.cancel("s1")["canceled"])
        self.assertEqual(
            backend.calls,
            [("status", "s1"), ("history", "s1", 5), ("list_sessions",), ("cancel", "s1")],
        )


MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(MCP_AVAILABLE, "install dsh-mcp-gateway[server] to test the MCP surface")
class McpSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_tool_surface_and_start_schema(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        server = build_mcp_server(GatewayService(backend))
        tools = await server.list_tools()
        self.assertEqual(
            [tool.name for tool in tools],
            ["dsh_start", "dsh_continue", "dsh_status", "dsh_history", "dsh_list", "dsh_cancel"],
        )
        start = tools[0]
        self.assertEqual(start.input_schema["required"], ["prompt"])
        self.assertEqual(set(start.input_schema["properties"]), {"prompt", "session_id"})

    async def test_start_tool_returns_structured_receipt(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        server = build_mcp_server(GatewayService(backend))
        result = await server.call_tool("dsh_start", {"prompt": "do work", "session_id": "s1"})
        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {"session_id": "s1", "action": "created", "message_id": "message-1"},
        )
        self.assertEqual(
            backend.calls,
            [("presence", "s1"), ("create", "s1"), ("prompt", "s1", "do work")],
        )


if __name__ == "__main__":
    unittest.main()
