from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dsh_mcp_gateway import build_mcp_server, build_public_sdk_gateway
from dsh_mcp_gateway.backend import (
    ColdResumeUnavailable,
    ExperimentalWebHostBackend,
    PublicSdkBackend,
    PublicSdkBridge,
    SessionCatalog,
)
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


class FakeWebHostHandler(BaseHTTPRequestHandler):
    server: Any

    def log_message(self, _format: str, *_args: Any) -> None:
        return None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        method = body["method"]
        payload = body["payload"]
        self.server.calls.append((method, payload))
        sessions = self.server.sessions
        result: dict[str, Any]
        if method == "host.describe":
            result = {
                "ok": True,
                "value": {
                    "version": "0.1.0-rc.6",
                    "cwd": "/tmp/project",
                    "attachedSessions": 0,
                    "canOpenPath": False,
                },
            }
        elif method == "session.list":
            items = [
                {
                    "sessionId": session_id,
                    "updatedAt": 1,
                    "running": state["running"],
                    "blank": False,
                    "cwd": state["cwd"],
                }
                for session_id, state in sessions.items()
            ]
            result = {"ok": True, "value": {"items": items}}
        elif method == "session.models":
            session_id = payload["sessionId"]
            if session_id not in sessions:
                result = {
                    "ok": False,
                    "error": {
                        "code": "session-not-found",
                        "message": "missing",
                        "details": {"sessionId": session_id},
                    },
                }
            else:
                sessions[session_id]["running"] = True
                result = {
                    "ok": True,
                    "value": {
                        "current": {"provider": "deepseek-official", "model": "deepseek-v4-flash"},
                        "routable": True,
                        "groups": [],
                        "failures": [],
                    },
                }
        elif method == "session.create":
            session_id = payload.get("sessionId", "generated-web-session")
            sessions[session_id] = {"running": True, "cwd": payload.get("cwd"), "events": []}
            result = {"ok": True, "value": {"sessionId": session_id}}
        elif method == "session.prompt":
            session_id = payload["sessionId"]
            sessions[session_id]["running"] = True
            sessions[session_id]["events"].append(
                {"type": "user/message", "seq": len(sessions[session_id]["events"]) + 1}
            )
            result = {"ok": True, "value": {"accepted": True}}
        elif method == "session.history":
            session_id = payload["sessionId"]
            entries = [{"event": event} for event in sessions[session_id]["events"]]
            result = {"ok": True, "value": {"events": entries, "hasMore": False}}
        elif method == "session.cancel":
            session_id = payload["sessionId"]
            sessions[session_id]["running"] = False
            result = {"ok": True, "value": {"accepted": True}}
        else:
            result = {"ok": False, "error": {"code": "bad-request", "message": method, "details": {}}}
        response = json.dumps(
            {
                "type": "server-response",
                "rpcId": body["rpcId"],
                "result": result,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class ExperimentalWebHostBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWebHostHandler)
        self.server.calls = []
        self.server.sessions = {
            "cold-1": {
                "running": False,
                "cwd": "/tmp/project",
                "events": [{"type": "turn/end", "seq": 1}],
            }
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.backend = ExperimentalWebHostBackend(
            f"http://{host}:{port}",
            cwd="/tmp/project",
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_non_loopback_target_is_rejected_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "no network authentication"):
            ExperimentalWebHostBackend("http://example.com:3080", cwd="/tmp/project")

    def test_host_descriptor_is_diagnostic_only(self) -> None:
        descriptor = self.backend.describe_host()
        self.assertEqual(descriptor["version"], "0.1.0-rc.6")
        self.assertEqual([method for method, _payload in self.server.calls], ["host.describe"])

    def test_cold_session_resumes_before_prompt_without_opening_a_turn(self) -> None:
        receipt = GatewayService(self.backend).continue_session("cold-1", "continue")
        self.assertEqual(receipt.action, "resumed")
        self.assertEqual(receipt.session_id, "cold-1")
        self.assertEqual(
            [method for method, _payload in self.server.calls],
            ["session.list", "session.list", "session.models", "session.prompt"],
        )
        self.assertEqual(self.backend.presence("cold-1"), SessionPresence.LIVE)
        self.assertEqual(self.backend.history("cold-1")[-1]["type"], "user/message")

    def test_idle_attached_session_is_reused_without_resume_probe(self) -> None:
        service = GatewayService(self.backend)
        first = service.start("work", session_id="fresh-1")
        self.assertEqual(first.action, "created")
        self.server.sessions["fresh-1"]["running"] = False
        self.server.calls.clear()

        second = service.continue_session("fresh-1", "more work")
        self.assertEqual(second.action, "reused")
        self.assertNotIn("session.models", [method for method, _payload in self.server.calls])
        self.server.sessions["fresh-1"]["running"] = False
        self.assertEqual(self.backend.status("fresh-1")["state"], "live")
        self.assertEqual(self.backend.status("fresh-1")["status"], "idle")

    def test_prompt_receipt_is_host_rpc_id_and_cancel_works(self) -> None:
        receipt = GatewayService(self.backend).start("work", session_id="fresh-1")
        self.assertEqual(receipt.action, "created")
        self.assertTrue(receipt.message_id)
        self.assertEqual(self.backend.status("fresh-1")["state"], "live")
        canceled = self.backend.cancel("fresh-1")
        self.assertTrue(canceled["canceled"])
        self.assertEqual(self.backend.status("fresh-1")["state"], "live")
        self.assertEqual(self.backend.status("fresh-1")["status"], "idle")


class FakeSubscription:
    def __init__(self) -> None:
        self.notifications: list[Any] = []
        self.closed = False

    def emit(self, notification: Any) -> None:
        self.notifications.append(notification)

    def drain(self, on_notification: Any) -> None:
        pending, self.notifications = self.notifications, []
        for notification in pending:
            on_notification(notification)

    def close(self) -> None:
        self.closed = True


class FakePublicSdkClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.fail = False
        self.subscription = FakeSubscription()

    def session_prompt(self, session_id: str, content_blocks: list[dict[str, Any]]) -> str:
        self.calls.append(("session_prompt", session_id, content_blocks))
        if self.fail:
            raise RuntimeError("sdk prompt failed")
        return "sdk-message-1"

    def subscribe_notifications(self, notification_filter: Any = None) -> FakeSubscription:
        return self.subscription


class PublicSdkBackendTests(unittest.TestCase):
    def test_catalog_survives_gateway_process_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            first = SessionCatalog(path)
            first.add("s1")
            second = SessionCatalog(path)
            self.assertTrue(second.contains("s1"))
            self.assertEqual(second.ids(), ["s1"])

    def test_live_prompt_then_notifications_project_status_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakePublicSdkClient()
            backend = PublicSdkBackend(client, SessionCatalog(Path(tmp) / "sessions.json"))
            receipt = GatewayService(backend).start("work", session_id="s1")
            self.assertEqual(receipt.action, "created")
            self.assertEqual(receipt.message_id, "sdk-message-1")
            self.assertEqual(backend.presence("s1"), SessionPresence.LIVE)
            backend.observe_notification("session.status", {"sessionId": "s1", "status": "running"})
            backend.observe_notification(
                "session.event",
                {"sessionId": "s1", "event": {"type": "turn/start", "seq": 1}},
            )
            self.assertEqual(backend.status("s1")["status"], "running")
            self.assertEqual(backend.history("s1"), [{"type": "turn/start", "seq": 1}])

    def test_failed_first_prompt_rolls_back_catalog_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            client = FakePublicSdkClient()
            client.fail = True
            backend = PublicSdkBackend(client, SessionCatalog(path))
            with self.assertRaisesRegex(RuntimeError, "sdk prompt failed"):
                GatewayService(backend).start("work", session_id="s1")
            self.assertEqual(backend.presence("s1"), SessionPresence.ABSENT)
            self.assertFalse(SessionCatalog(path).contains("s1"))

    def test_restart_detects_persisted_id_and_fails_before_sdk_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            first_client = FakePublicSdkClient()
            first = PublicSdkBackend(first_client, SessionCatalog(path))
            GatewayService(first).start("work", session_id="s1")

            second_client = FakePublicSdkClient()
            restarted = PublicSdkBackend(second_client, SessionCatalog(path))
            self.assertEqual(restarted.presence("s1"), SessionPresence.PERSISTED)
            with self.assertRaisesRegex(ColdResumeUnavailable, "cannot cold-resume"):
                GatewayService(restarted).continue_session("s1", "continue")
            self.assertEqual(second_client.calls, [])

    def test_public_sdk_cancel_is_explicitly_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = PublicSdkBackend(FakePublicSdkClient(), SessionCatalog(Path(tmp) / "sessions.json"))
            GatewayService(backend).start("work", session_id="s1")
            result = backend.cancel("s1")
            self.assertFalse(result["canceled"])
            self.assertEqual(result["reason"], "unsupported-by-public-sdk")

    def test_notification_bridge_projects_sdk_events_without_owning_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakePublicSdkClient()
            bridge = PublicSdkBridge(client, SessionCatalog(Path(tmp) / "sessions.json"))
            GatewayService(bridge.backend).start("work", session_id="s1")
            client.subscription.emit(
                {"method": "session.status", "payload": {"sessionId": "s1", "status": "running"}}
            )
            client.subscription.emit(
                {
                    "method": "session.event",
                    "payload": {"sessionId": "s1", "event": {"type": "goal/change", "seq": 4}},
                }
            )
            bridge.poll_once()
            self.assertEqual(bridge.backend.status("s1")["status"], "running")
            self.assertEqual(bridge.backend.history("s1"), [{"type": "goal/change", "seq": 4}])
            bridge.close()
            self.assertTrue(client.subscription.closed)


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

    async def test_public_sdk_factory_wires_mcp_to_event_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakePublicSdkClient()
            gateway = build_public_sdk_gateway(client, Path(tmp) / "sessions.json")
            started = await gateway.server.call_tool(
                "dsh_start",
                {"prompt": "do work", "session_id": "s1"},
            )
            self.assertEqual(started.structured_content["action"], "created")
            client.subscription.emit(
                {"method": "session.status", "payload": {"sessionId": "s1", "status": "running"}}
            )
            gateway.bridge.poll_once()
            status = await gateway.server.call_tool("dsh_status", {"session_id": "s1"})
            self.assertEqual(status.structured_content["status"], "running")
            gateway.close()
            self.assertTrue(client.subscription.closed)


if __name__ == "__main__":
    unittest.main()
