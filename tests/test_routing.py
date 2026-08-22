from __future__ import annotations

import fcntl
import importlib.util
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

from dsh_mcp_gateway import __version__, build_mcp_server, build_public_sdk_gateway
from dsh_mcp_gateway.backend import (
    ColdResumeUnavailable,
    ExperimentalWebHostBackend,
    ExperimentalWebHostError,
    GoalControlUnavailable,
    HistoryPaginationUnavailable,
    MessageHistoryUnavailable,
    PublicSdkBackend,
    PublicSdkBridge,
    SessionCatalog,
    SessionSearchUnavailable,
)
from dsh_mcp_gateway.routing import (
    EnsureAction,
    GatewayService,
    PromptReceipt,
    SessionRouter,
)
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

    def history_page(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        max_messages: int = 50,
    ) -> dict[str, Any]:
        self.calls.append(("history_page", session_id, before_seq, max_messages))
        return {
            "session_id": session_id,
            "events": [{"type": "turn/end", "seq": 10}],
            "has_more": False,
            "next_before_seq": None,
        }

    def messages(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        self.calls.append(("messages", session_id, before_seq, limit))
        return {
            "session_id": session_id,
            "messages": [
                {
                    "seq": 8,
                    "time": 9,
                    "role": "assistant",
                    "message_id": "m1",
                    "source_kind": "model",
                    "text": "latest answer",
                    "omitted_block_types": [],
                }
            ],
            "has_more": False,
            "next_before_seq": None,
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        self.calls.append(("list_sessions",))
        return [{"session_id": "s1"}]

    def search_sessions(self, query: str) -> dict[str, Any]:
        self.calls.append(("search_sessions", query))
        return {"query": query, "items": [{"session_id": "s1", "snippet": "matching text"}], "has_more": False}

    def cancel(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", session_id))
        return {"session_id": session_id, "canceled": True}

    def goal_status(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("goal_status", session_id))
        return {"session_id": session_id, "goal": None}

    def goal_create(
        self,
        session_id: str,
        objective: str,
        *,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("goal_create", session_id, objective, max_goal_rounds))
        return {"session_id": session_id, "action": "created", "ref": {"id": "g1", "revision": 1}}

    def goal_edit(
        self,
        session_id: str,
        *,
        objective: str | None = None,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("goal_edit", session_id, objective, max_goal_rounds))
        return {"session_id": session_id, "action": "edited", "ref": {"id": "g1", "revision": 2}}

    def goal_resume(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("goal_resume", session_id))
        return {"session_id": session_id, "action": "resumed"}

    def goal_pause(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("goal_pause", session_id))
        return {"session_id": session_id, "action": "paused"}

    def goal_complete(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("goal_complete", session_id))
        return {"session_id": session_id, "action": "completed", "ref": {"id": "g1", "revision": 3}}

    def goal_clear(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("goal_clear", session_id))
        return {"session_id": session_id, "action": "cleared", "cleared": True}


class ConcurrentSessionBackend:
    def __init__(
        self,
        *,
        block_first_presence_for: str | None = None,
        block_first_prompt_for: str | None = None,
    ) -> None:
        self.block_first_presence_for = block_first_presence_for
        self.block_first_prompt_for = block_first_prompt_for
        self.presence_entered = threading.Event()
        self.release_presence = threading.Event()
        self.prompt_entered = threading.Event()
        self.release_prompt = threading.Event()
        self._guard = threading.Lock()
        self._presence_counts: dict[str, int] = {}
        self._prompt_counts: dict[str, int] = {}
        self.sessions: set[str] = set()
        self.create_calls: list[str] = []
        self.prompt_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[str] = []
        self.goal_resume_calls: list[str] = []

    def presence(self, session_id: str) -> SessionPresence:
        with self._guard:
            count = self._presence_counts.get(session_id, 0) + 1
            self._presence_counts[session_id] = count
            existed_at_observation = session_id in self.sessions
        if (
            session_id == self.block_first_presence_for
            and count == 1
            and not existed_at_observation
        ):
            self.presence_entered.set()
            if not self.release_presence.wait(timeout=2):
                raise TimeoutError("test presence gate was not released")
        return SessionPresence.LIVE if existed_at_observation else SessionPresence.ABSENT

    def reuse(self, session_id: str) -> SessionHandle:
        with self._guard:
            if session_id not in self.sessions:
                raise KeyError(session_id)
        return SessionHandle(session_id)

    def resume(self, session_id: str) -> SessionHandle:
        raise AssertionError("resume is not used by this fake")

    def create(self, session_id: str | None = None) -> SessionHandle:
        assert session_id is not None
        with self._guard:
            if session_id in self.sessions:
                raise RuntimeError(f"duplicate create: {session_id}")
            self.sessions.add(session_id)
            self.create_calls.append(session_id)
        return SessionHandle(session_id)

    def prompt(self, session_id: str, text: str) -> str:
        with self._guard:
            if session_id not in self.sessions:
                raise KeyError(session_id)
            count = self._prompt_counts.get(session_id, 0) + 1
            self._prompt_counts[session_id] = count
            self.prompt_calls.append((session_id, text))
        if session_id == self.block_first_prompt_for and count == 1:
            self.prompt_entered.set()
            if not self.release_prompt.wait(timeout=2):
                raise TimeoutError("test prompt gate was not released")
        return f"message-{count}"

    def cancel(self, session_id: str) -> dict[str, Any]:
        with self._guard:
            self.cancel_calls.append(session_id)
        return {"session_id": session_id, "canceled": True}

    def goal_resume(self, session_id: str) -> dict[str, Any]:
        with self._guard:
            self.goal_resume_calls.append(session_id)
        return {"session_id": session_id, "action": "resumed"}


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

    def test_concurrent_same_id_ensure_serializes_create_then_reuse(self) -> None:
        backend = ConcurrentSessionBackend(block_first_presence_for="s1")
        router = SessionRouter(backend)
        results: list[EnsureAction] = []
        errors: list[Exception] = []

        def run() -> None:
            try:
                results.append(router.ensure("s1").action)
            except (RuntimeError, KeyError, TimeoutError, AssertionError) as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        first = threading.Thread(target=run)
        first.start()
        self.assertTrue(backend.presence_entered.wait(timeout=1))
        second = threading.Thread(target=run)
        second.start()
        try:
            second.join(timeout=0.05)
            self.assertTrue(second.is_alive())
        finally:
            backend.release_presence.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(action.value for action in results), ["created", "reused"])
        self.assertEqual(backend.create_calls, ["s1"])

    def test_different_session_ids_do_not_share_router_lock(self) -> None:
        backend = ConcurrentSessionBackend(block_first_presence_for="s1")
        router = SessionRouter(backend)
        second_done = threading.Event()
        errors: list[Exception] = []

        def first_run() -> None:
            try:
                router.ensure("s1")
            except (RuntimeError, KeyError, TimeoutError, AssertionError) as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def second_run() -> None:
            try:
                router.ensure("s2")
            except (RuntimeError, KeyError, TimeoutError, AssertionError) as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                second_done.set()

        first = threading.Thread(target=first_run)
        first.start()
        self.assertTrue(backend.presence_entered.wait(timeout=1))
        second = threading.Thread(target=second_run)
        second.start()
        try:
            self.assertTrue(second_done.wait(timeout=1))
        finally:
            backend.release_presence.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertCountEqual(backend.create_calls, ["s1", "s2"])


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

    def test_same_session_prompt_admission_is_serialized(self) -> None:
        backend = ConcurrentSessionBackend(block_first_prompt_for="s1")
        service = GatewayService(backend)
        receipts: dict[str, PromptReceipt] = {}
        errors: list[Exception] = []
        second_done = threading.Event()

        def first_run() -> None:
            try:
                receipts["first"] = service.start("first", session_id="s1")
            except (RuntimeError, KeyError, TimeoutError, AssertionError) as exc:  # pragma: no cover
                errors.append(exc)

        def second_run() -> None:
            try:
                receipts["second"] = service.continue_session("s1", "second")
            except (RuntimeError, KeyError, TimeoutError, AssertionError) as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                second_done.set()

        first = threading.Thread(target=first_run)
        first.start()
        self.assertTrue(backend.prompt_entered.wait(timeout=1))
        second = threading.Thread(target=second_run)
        second.start()
        try:
            self.assertFalse(second_done.wait(timeout=0.05))
            self.assertEqual(backend.prompt_calls, [("s1", "first")])
        finally:
            backend.release_prompt.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(backend.prompt_calls, [("s1", "first"), ("s1", "second")])
        self.assertEqual(receipts["first"].action, "created")
        self.assertEqual(receipts["second"].action, "reused")

    def test_cancel_waits_for_same_session_prompt_admission(self) -> None:
        backend = ConcurrentSessionBackend(block_first_prompt_for="s1")
        service = GatewayService(backend)
        mutation_done = threading.Event()
        errors: list[Exception] = []

        def start_first() -> None:
            try:
                service.start("first", session_id="s1")
            except (RuntimeError, KeyError, TimeoutError, AssertionError) as exc:  # pragma: no cover
                errors.append(exc)

        first = threading.Thread(target=start_first)
        first.start()
        self.assertTrue(backend.prompt_entered.wait(timeout=1))

        def mutate() -> None:
            try:
                service.cancel("s1")
            except (RuntimeError, KeyError, TimeoutError, AssertionError) as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                mutation_done.set()

        second = threading.Thread(target=mutate)
        second.start()
        try:
            self.assertFalse(mutation_done.wait(timeout=0.05))
            self.assertEqual(backend.cancel_calls, [])
        finally:
            backend.release_prompt.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(backend.cancel_calls, ["s1"])

    def test_goal_mutation_waits_for_same_session_prompt_admission(self) -> None:
        backend = ConcurrentSessionBackend(block_first_prompt_for="s1")
        service = GatewayService(backend)
        mutation_done = threading.Event()
        errors: list[Exception] = []

        def start_first() -> None:
            try:
                service.start("first", session_id="s1")
            except (RuntimeError, KeyError, TimeoutError, AssertionError) as exc:  # pragma: no cover
                errors.append(exc)

        first = threading.Thread(target=start_first)
        first.start()
        self.assertTrue(backend.prompt_entered.wait(timeout=1))

        def mutate() -> None:
            try:
                service.goal_resume("s1")
            except (RuntimeError, KeyError, TimeoutError, AssertionError) as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                mutation_done.set()

        second = threading.Thread(target=mutate)
        second.start()
        try:
            self.assertFalse(mutation_done.wait(timeout=0.05))
            self.assertEqual(backend.goal_resume_calls, [])
        finally:
            backend.release_prompt.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(backend.goal_resume_calls, ["s1"])

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

    def test_history_page_delegates_cursor_without_session_routing(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        page = GatewayService(backend).history_page("s1", before_seq=42, max_messages=25)
        self.assertEqual(page["events"][0]["seq"], 10)
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_before_seq"])
        self.assertEqual(backend.calls, [("history_page", "s1", 42, 25)])

    def test_messages_delegate_cursor_without_session_routing(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        result = GatewayService(backend).messages("s1", before_seq=42, limit=20)
        self.assertEqual(result["messages"][0]["text"], "latest answer")
        self.assertFalse(result["has_more"])
        self.assertEqual(backend.calls, [("messages", "s1", 42, 20)])

    def test_list_sessions_page_bounds_output_and_returns_offset_cursor(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        backend.list_sessions = lambda: [
            {"session_id": "s1"},
            {"session_id": "s2"},
            {"session_id": "s3"},
        ]
        service = GatewayService(backend)
        first = service.list_sessions_page(limit=2)
        second = service.list_sessions_page(limit=2, offset=first["next_offset"])
        self.assertEqual(first, {
            "items": [{"session_id": "s1"}, {"session_id": "s2"}],
            "total": 3,
            "has_more": True,
            "next_offset": 2,
        })
        self.assertEqual(second, {
            "items": [{"session_id": "s3"}],
            "total": 3,
            "has_more": False,
            "next_offset": None,
        })
        with self.assertRaisesRegex(ValueError, "limit"):
            service.list_sessions_page(limit=101)
        with self.assertRaisesRegex(ValueError, "offset"):
            service.list_sessions_page(offset=-1)

    def test_search_sessions_delegates_without_session_routing(self) -> None:
        backend = FakeBackend(SessionPresence.ABSENT)
        result = GatewayService(backend).search_sessions("remembered phrase")
        self.assertEqual(result["items"][0]["session_id"], "s1")
        self.assertFalse(result["has_more"])
        self.assertEqual(backend.calls, [("search_sessions", "remembered phrase")])


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
        elif method == "session.search":
            if getattr(self.server, "search_disabled", False):
                result = {
                    "ok": False,
                    "error": {
                        "code": "internal",
                        "message": (
                            "session search failed: SessionQueryError: session search is disabled: "
                            'this deployment configures the session-query index with openAt "never"'
                        ),
                        "details": {},
                    },
                }
            else:
                query = payload["query"].casefold()
                matches = [
                    {"sessionId": session_id, "snippet": state.get("search_text", "")}
                    for session_id, state in sessions.items()
                    if query in state.get("search_text", "").casefold()
                ]
                result = {
                    "ok": True,
                    "value": {"items": matches[:20], "hasMore": len(matches) > 20},
                }
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
            sessions[session_id] = {
                "running": True,
                "cwd": payload.get("cwd"),
                "events": [],
                "goal": None,
            }
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
            state = sessions[session_id]
            before_seq = payload.get("beforeSeq")
            max_messages = payload.get("maxMessages", 50)
            candidates = [
                event
                for event in state["events"]
                if before_seq is None or event.get("seq", -1) < before_seq
            ]
            has_more = len(candidates) > max_messages
            entries = [{"event": event} for event in candidates[-max_messages:]]
            value: dict[str, Any] = {
                "events": entries,
                "hasMore": has_more,
            }
            if before_seq is None:
                value["projections"] = {
                    "asOfSeq": len(state["events"]),
                    "values": {"goal": state.get("goal")},
                }
            result = {"ok": True, "value": value}
        elif method == "goal.create":
            session_id = payload["sessionId"]
            state = sessions[session_id]
            if state.get("goal") is not None:
                result = {
                    "ok": False,
                    "error": {"code": "goal-exists", "message": "current goal exists", "details": {}},
                }
            else:
                state["goal"] = {
                    "goal": {
                        "id": "goal-created-1",
                        "revision": 1,
                        "objective": payload["objective"],
                        "phase": "active",
                        "maxGoalRounds": payload.get("maxGoalRounds", 256),
                    },
                    "roundsStarted": 0,
                    "createdAt": 1,
                    "updatedAt": 1,
                }
                result = {"ok": True, "value": {"ref": {"id": "goal-created-1", "revision": 1}}}
        elif method == "goal.edit":
            session_id = payload["sessionId"]
            state = sessions[session_id]
            projection = state.get("goal")
            if not isinstance(projection, dict) or not isinstance(projection.get("goal"), dict):
                result = {
                    "ok": False,
                    "error": {"code": "goal-missing", "message": "no current goal", "details": {}},
                }
            else:
                goal = projection["goal"]
                expected_ref = {"id": goal["id"], "revision": goal["revision"]}
                if payload.get("ref") != expected_ref:
                    result = {
                        "ok": False,
                        "error": {"code": "goal-stale-ref", "message": "stale goal ref", "details": {}},
                    }
                else:
                    goal["revision"] += 1
                    if "objective" in payload:
                        goal["objective"] = payload["objective"]
                    if "maxGoalRounds" in payload:
                        goal["maxGoalRounds"] = payload["maxGoalRounds"]
                    projection["updatedAt"] = projection.get("updatedAt", 1) + 1
                    result = {
                        "ok": True,
                        "value": {"ref": {"id": goal["id"], "revision": goal["revision"]}},
                    }
        elif method in {"goal.resume", "goal.pause"}:
            session_id = payload["sessionId"]
            state = sessions[session_id]
            projection = state.get("goal")
            if not isinstance(projection, dict) or not isinstance(projection.get("goal"), dict):
                result = {
                    "ok": False,
                    "error": {"code": "goal-missing", "message": "no current goal", "details": {}},
                }
            else:
                goal = projection["goal"]
                expected_ref = {"id": goal["id"], "revision": goal["revision"]}
                if payload.get("ref") != expected_ref:
                    result = {
                        "ok": False,
                        "error": {"code": "goal-stale-ref", "message": "stale goal ref", "details": {}},
                    }
                else:
                    goal["revision"] += 1
                    goal["phase"] = "active" if method == "goal.resume" else "paused"
                    result = {
                        "ok": True,
                        "value": {"ref": {"id": goal["id"], "revision": goal["revision"]}},
                    }
        elif method == "goal.complete":
            session_id = payload["sessionId"]
            state = sessions[session_id]
            projection = state.get("goal")
            if not isinstance(projection, dict) or not isinstance(projection.get("goal"), dict):
                result = {
                    "ok": False,
                    "error": {"code": "goal-missing", "message": "no current goal", "details": {}},
                }
            else:
                goal = projection["goal"]
                expected_ref = {"id": goal["id"], "revision": goal["revision"]}
                if payload.get("ref") != expected_ref:
                    result = {
                        "ok": False,
                        "error": {"code": "goal-stale-ref", "message": "stale goal ref", "details": {}},
                    }
                else:
                    goal["revision"] += 1
                    goal["phase"] = "complete"
                    result = {
                        "ok": True,
                        "value": {"ref": {"id": goal["id"], "revision": goal["revision"]}},
                    }
        elif method == "goal.clear":
            session_id = payload["sessionId"]
            state = sessions[session_id]
            projection = state.get("goal")
            if not isinstance(projection, dict) or not isinstance(projection.get("goal"), dict):
                result = {
                    "ok": False,
                    "error": {"code": "goal-missing", "message": "no current goal", "details": {}},
                }
            else:
                goal = projection["goal"]
                expected_ref = {"id": goal["id"], "revision": goal["revision"]}
                if payload.get("ref") != expected_ref:
                    result = {
                        "ok": False,
                        "error": {"code": "goal-stale-ref", "message": "stale goal ref", "details": {}},
                    }
                else:
                    state["goal"] = None
                    result = {"ok": True, "value": {"cleared": True}}
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
        self.server.search_disabled = False
        self.server.sessions = {
            "cold-1": {
                "running": False,
                "cwd": "/tmp/project",
                "events": [{"type": "turn/end", "seq": 1}],
                "goal": None,
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

    def test_web_host_target_must_be_an_http_origin(self) -> None:
        cases = (
            ("http://user:pass@127.0.0.1:3080", "user info"),
            ("http://127.0.0.1:3080/api", "origin without a path"),
            ("http://127.0.0.1:3080/;v=1", "origin without a path"),
            ("http://127.0.0.1:3080/?mode=test", "origin without a path"),
            ("http://127.0.0.1:3080/#debug", "origin without a path"),
            ("http://127.0.0.1:not-a-port", "invalid port"),
        )
        for base_url, message in cases:
            with self.subTest(base_url=base_url), self.assertRaisesRegex(ValueError, message):
                ExperimentalWebHostBackend(base_url, cwd="/tmp/project")

        backend = ExperimentalWebHostBackend("http://127.0.0.1:3080/", cwd="/tmp/project")
        self.assertEqual(backend.base_url, "http://127.0.0.1:3080")

    def test_goal_status_rejects_unknown_session(self) -> None:
        with self.assertRaises(KeyError):
            self.backend.goal_status("missing")

    def test_host_descriptor_is_diagnostic_only(self) -> None:
        descriptor = self.backend.describe_host()
        self.assertEqual(descriptor["version"], "0.1.0-rc.6")
        self.assertEqual([method for method, _payload in self.server.calls], ["host.describe"])

    def test_host_descriptor_can_override_transport_timeout(self) -> None:
        observed_timeouts: list[float] = []

        class FakeResponse:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return self.payload if size < 0 else self.payload[:size]

        def fake_urlopen(request, *, timeout: float):
            observed_timeouts.append(timeout)
            request_body = json.loads(request.data)
            payload = json.dumps(
                {
                    "type": "server-response",
                    "rpcId": request_body["rpcId"],
                    "result": {"ok": True, "value": {"version": "0.0.1"}},
                }
            ).encode()
            return FakeResponse(payload)

        with patch("dsh_mcp_gateway.backend.urlopen", side_effect=fake_urlopen):
            descriptor = self.backend.describe_host(timeout_s=0.25)

        self.assertEqual(descriptor, {"version": "0.0.1"})
        self.assertEqual(observed_timeouts, [0.25])
        with self.assertRaisesRegex(ValueError, "timeout_s"):
            self.backend.describe_host(timeout_s=0)

    def test_web_host_non_utf8_response_is_wrapped(self) -> None:
        class NonUtf8Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return b"\xff"

        with (
            patch("dsh_mcp_gateway.backend.urlopen", return_value=NonUtf8Response()),
            self.assertRaisesRegex(ExperimentalWebHostError, "invalid JSON"),
        ):
            self.backend.describe_host()

    def test_web_host_rejects_oversized_response_before_full_read(self) -> None:
        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                self.requested_size = size
                return b"x" * size

        response = OversizedResponse()
        with (
            patch("dsh_mcp_gateway.backend.urlopen", return_value=response),
            self.assertRaisesRegex(ExperimentalWebHostError, "response exceeds"),
        ):
            self.backend.describe_host()

        self.assertEqual(response.requested_size, 16 * 1024 * 1024 + 1)

    def test_web_host_http_error_body_read_failure_stays_wrapped(self) -> None:
        class BrokenErrorBody:
            def __init__(self) -> None:
                self.closed = False

            def read(self, *_args, **_kwargs):
                raise TimeoutError("timed out while reading HTTP error body")

            def close(self) -> None:
                self.closed = True

        body = BrokenErrorBody()
        error = HTTPError(
            "http://127.0.0.1:3080/api/host.describe",
            502,
            "Bad Gateway",
            {},
            body,
        )
        with (
            patch("dsh_mcp_gateway.backend.urlopen", side_effect=error),
            self.assertRaisesRegex(ExperimentalWebHostError, "HTTP 502"),
        ):
            self.backend.describe_host()
        self.assertTrue(body.closed)

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

    def test_non_running_session_uses_attach_resume_probe_even_in_same_gateway(self) -> None:
        service = GatewayService(self.backend)
        first = service.start("work", session_id="fresh-1")
        self.assertEqual(first.action, "created")
        # running=false is intentionally ambiguous: this can be a live-idle
        # Agent or the same durable session after the DSH Host restarted while
        # the gateway process stayed alive. There is no Host boot id to tell.
        self.server.sessions["fresh-1"]["running"] = False
        self.server.calls.clear()

        second = service.continue_session("fresh-1", "more work")
        self.assertEqual(second.action, "resumed")
        self.assertEqual(
            [method for method, _payload in self.server.calls],
            ["session.list", "session.list", "session.models", "session.prompt"],
        )
        self.server.sessions["fresh-1"]["running"] = False
        self.server.calls.clear()
        status = self.backend.status("fresh-1")
        self.assertEqual(status["state"], "persisted")
        self.assertEqual(status["status"], "not-running")
        self.assertEqual(status["attachment_state"], "ambiguous-idle-or-cold")
        self.assertTrue(status["write_attach_probe_required"])
        self.assertEqual([method for method, _payload in self.server.calls], ["session.list"])

    def test_history_page_walks_backwards_with_before_seq_cursor(self) -> None:
        self.server.sessions["page-1"] = {
            "running": False,
            "cwd": "/tmp/project",
            "events": [
                {"type": "user/message", "seq": 1},
                {"type": "assistant/message", "seq": 2},
                {"type": "turn/end", "seq": 3},
                {"type": "user/message", "seq": 4},
                {"type": "turn/end", "seq": 5},
            ],
        }
        self.server.calls.clear()

        tail = self.backend.history_page("page-1", max_messages=2)
        self.assertEqual([event["seq"] for event in tail["events"]], [4, 5])
        self.assertTrue(tail["has_more"])
        self.assertEqual(tail["next_before_seq"], 4)
        self.assertEqual(
            self.server.calls[-1],
            ("session.history", {"sessionId": "page-1", "maxMessages": 2}),
        )

        older = self.backend.history_page("page-1", before_seq=4, max_messages=2)
        self.assertEqual([event["seq"] for event in older["events"]], [2, 3])
        self.assertTrue(older["has_more"])
        self.assertEqual(older["next_before_seq"], 2)
        self.assertEqual(
            self.server.calls[-1],
            ("session.history", {"sessionId": "page-1", "maxMessages": 2, "beforeSeq": 4}),
        )

        oldest = self.backend.history_page("page-1", before_seq=2, max_messages=2)
        self.assertEqual([event["seq"] for event in oldest["events"]], [1])
        self.assertFalse(oldest["has_more"])
        self.assertIsNone(oldest["next_before_seq"])

    def test_history_page_validates_cursor_and_page_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "before_seq"):
            self.backend.history_page("cold-1", before_seq=-1)
        with self.assertRaisesRegex(ValueError, "before_seq"):
            self.backend.history_page("cold-1", before_seq=True)
        with self.assertRaisesRegex(ValueError, "max_messages"):
            self.backend.history_page("cold-1", max_messages=0)
        with self.assertRaisesRegex(ValueError, "max_messages"):
            self.backend.history_page("cold-1", max_messages=1001)

    def test_messages_cursor_skips_plugin_only_host_pages(self) -> None:
        assistant = {
            "type": "assistant/message",
            "seq": 10,
            "time": 10,
            "surfaceOp": "append",
            "data": {
                "message": {
                    "id": "a1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "latest"}],
                    "source": {"kind": "model", "provider": "mock", "model": "mock"},
                }
            },
        }
        plugin = {
            "type": "user/message",
            "seq": 5,
            "time": 5,
            "surfaceOp": "append",
            "data": {
                "id": "p1",
                "role": "user",
                "content": [{"type": "text", "text": "internal"}],
                "source": {"kind": "plugin", "plugin": "example"},
            },
        }
        user = {
            "type": "user/message",
            "seq": 4,
            "time": 4,
            "surfaceOp": "append",
            "data": {
                "id": "u1",
                "role": "user",
                "content": [{"type": "text", "text": "older human"}],
                "source": {"kind": "user"},
            },
        }
        raw_prefix = {"type": "turn/start", "seq": 0, "time": 0, "data": {"turn": 1}}

        with patch.object(
            self.backend,
            "_history_page",
            side_effect=[
                {"events": [{"event": assistant}], "hasMore": True},
                {"events": [{"event": plugin}], "hasMore": True},
                {"events": [{"event": user}], "hasMore": True},
                {"events": [{"event": user}], "hasMore": True},
                {"events": [{"event": raw_prefix}], "hasMore": False},
            ],
        ) as history:
            latest = self.backend.messages("cold-1", limit=1)
            older = self.backend.messages("cold-1", before_seq=latest["next_before_seq"], limit=1)

        self.assertEqual([message["message_id"] for message in latest["messages"]], ["a1"])
        self.assertTrue(latest["has_more"])
        self.assertEqual(latest["next_before_seq"], 5)
        self.assertEqual([message["message_id"] for message in older["messages"]], ["u1"])
        self.assertFalse(older["has_more"])
        self.assertIsNone(older["next_before_seq"])
        self.assertEqual(
            [call.kwargs for call in history.call_args_list],
            [
                {"limit": 1, "before_seq": None},
                {"limit": 16, "before_seq": 10},
                {"limit": 16, "before_seq": 5},
                {"limit": 1, "before_seq": 5},
                {"limit": 16, "before_seq": 4},
            ],
        )

    def test_messages_preserve_chronological_order_across_host_pages(self) -> None:
        assistant = {
            "type": "assistant/message",
            "seq": 10,
            "time": 10,
            "surfaceOp": "append",
            "data": {
                "message": {
                    "id": "a1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "newer answer"}],
                    "source": {"kind": "model", "provider": "mock", "model": "mock"},
                }
            },
        }
        plugin = {
            "type": "user/message",
            "seq": 5,
            "time": 5,
            "surfaceOp": "append",
            "data": {
                "id": "p1",
                "role": "user",
                "content": [{"type": "text", "text": "internal"}],
                "source": {"kind": "plugin", "plugin": "example"},
            },
        }
        user = {
            "type": "user/message",
            "seq": 4,
            "time": 4,
            "surfaceOp": "append",
            "data": {
                "id": "u1",
                "role": "user",
                "content": [{"type": "text", "text": "older question"}],
                "source": {"kind": "user"},
            },
        }
        raw_prefix = {"type": "turn/start", "seq": 0, "time": 0, "data": {"turn": 1}}

        with patch.object(
            self.backend,
            "_history_page",
            side_effect=[
                {"events": [{"event": plugin}, {"event": assistant}], "hasMore": True},
                {"events": [{"event": user}], "hasMore": True},
                {"events": [{"event": raw_prefix}], "hasMore": False},
            ],
        ):
            transcript = self.backend.messages("cold-1", limit=2)

        self.assertEqual(
            [(message["role"], message["message_id"]) for message in transcript["messages"]],
            [("user", "u1"), ("assistant", "a1")],
        )
        self.assertFalse(transcript["has_more"])
        self.assertIsNone(transcript["next_before_seq"])

    def test_messages_project_compact_human_model_transcript(self) -> None:
        self.server.sessions["transcript-1"] = {
            "running": False,
            "cwd": "/tmp/project",
            "events": [
                {
                    "type": "user/message",
                    "seq": 1,
                    "time": 10,
                    "surfaceOp": "append",
                    "data": {
                        "id": "u1",
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "image", "attachment": {"id": "img1"}},
                        ],
                        "source": {"kind": "user"},
                    },
                },
                {
                    "type": "user/message",
                    "seq": 2,
                    "time": 11,
                    "surfaceOp": "append",
                    "data": {
                        "id": "plugin1",
                        "role": "user",
                        "content": [{"type": "text", "text": "internal plugin notice"}],
                        "source": {"kind": "plugin", "plugin": "example"},
                    },
                },
                {
                    "type": "assistant/message",
                    "seq": 3,
                    "time": 12,
                    "surfaceOp": "append",
                    "data": {
                        "turn": 1,
                        "step": 1,
                        "message": {
                            "id": "a1",
                            "role": "assistant",
                            "content": [
                                {"type": "reasoning", "text": "private reasoning"},
                                {"type": "text", "text": "visible answer"},
                                {"type": "tool-call", "id": "c1", "name": "read", "arguments": "{}"},
                            ],
                            "source": {"kind": "model", "provider": "mock", "model": "mock"},
                        },
                    },
                },
                {
                    "type": "assistant/message",
                    "seq": 4,
                    "time": 13,
                    "surfaceOp": {"op": "replace", "start": 1, "end": 3},
                    "data": {
                        "turn": 1,
                        "step": 2,
                        "message": {
                            "id": "replacement",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "model-only replacement"}],
                            "source": {"kind": "model", "provider": "mock", "model": "mock"},
                        },
                    },
                },
                {"type": "turn/end", "seq": 5, "time": 14, "data": {"turn": 1}},
            ],
            "goal": None,
        }

        transcript = self.backend.messages("transcript-1", limit=10)
        self.assertEqual(
            transcript["messages"],
            [
                {
                    "seq": 1,
                    "time": 10,
                    "role": "user",
                    "message_id": "u1",
                    "source_kind": "user",
                    "text": "hello",
                    "omitted_block_types": ["image"],
                },
                {
                    "seq": 3,
                    "time": 12,
                    "role": "assistant",
                    "message_id": "a1",
                    "source_kind": "model",
                    "text": "visible answer",
                    "omitted_block_types": ["reasoning", "tool-call"],
                },
            ],
        )
        self.assertFalse(transcript["has_more"])
        self.assertIsNone(transcript["next_before_seq"])

        older = self.backend.messages("transcript-1", before_seq=3, limit=10)
        self.assertEqual([message["message_id"] for message in older["messages"]], ["u1"])
        with self.assertRaisesRegex(ValueError, "limit"):
            self.backend.messages("transcript-1", limit=101)

    def test_session_search_returns_matches_and_validates_query(self) -> None:
        self.server.sessions["search-1"] = {
            "running": False,
            "cwd": "/tmp/project",
            "events": [],
            "search_text": "Investigate durable OAuth refresh behavior",
        }
        self.server.sessions["search-2"] = {
            "running": False,
            "cwd": "/tmp/project",
            "events": [],
            "search_text": "Unrelated browser task",
        }
        self.server.calls.clear()

        result = self.backend.search_sessions("  durable oauth  ")
        self.assertEqual(result["query"], "durable oauth")
        self.assertEqual(
            result["items"],
            [{"session_id": "search-1", "snippet": "Investigate durable OAuth refresh behavior"}],
        )
        self.assertFalse(result["has_more"])
        self.assertEqual(self.server.calls[-1], ("session.search", {"query": "durable oauth"}))

        with self.assertRaisesRegex(ValueError, "non-empty"):
            self.backend.search_sessions("   ")
        with self.assertRaisesRegex(ValueError, "500"):
            self.backend.search_sessions("x" * 501)
        with self.assertRaisesRegex(ValueError, "NUL"):
            self.backend.search_sessions("bad\0query")

    def test_search_disabled_is_reported_as_capability_unavailable(self) -> None:
        self.server.search_disabled = True
        with self.assertRaisesRegex(SessionSearchUnavailable, "not enabled"):
            self.backend.search_sessions("remembered phrase")

    def test_goal_create_is_structured_and_arms_existing_session(self) -> None:
        created = self.backend.goal_create(
            "cold-1",
            "  finish the durable task  ",
            max_goal_rounds=5,
        )
        self.assertEqual(created["action"], "created")
        self.assertEqual(created["ref"], {"id": "goal-created-1", "revision": 1})
        create_calls = [payload for method, payload in self.server.calls if method == "goal.create"]
        self.assertEqual(
            create_calls,
            [
                {
                    "sessionId": "cold-1",
                    "objective": "finish the durable task",
                    "maxGoalRounds": 5,
                }
            ],
        )
        status = self.backend.goal_status("cold-1")
        self.assertEqual(status["goal"]["goal"]["objective"], "finish the durable task")
        self.assertEqual(status["goal"]["goal"]["maxGoalRounds"], 5)

    def test_goal_create_validates_objective_and_round_cap(self) -> None:
        with self.assertRaisesRegex(ValueError, "objective"):
            self.backend.goal_create("cold-1", "   ")
        with self.assertRaisesRegex(ValueError, "max_goal_rounds"):
            self.backend.goal_create("cold-1", "work", max_goal_rounds=0)

    def test_goal_edit_complete_and_clear_use_latest_cas_refs(self) -> None:
        self.server.sessions["cold-1"]["goal"] = {
            "goal": {
                "id": "goal-lifecycle-1",
                "revision": 3,
                "objective": "original objective",
                "phase": "blocked",
                "blockedReason": {"code": "round-limit", "message": "limit reached"},
                "maxGoalRounds": 2,
            },
            "roundsStarted": 2,
            "createdAt": 1,
            "updatedAt": 3,
        }
        self.server.calls.clear()

        edited = self.backend.goal_edit(
            "cold-1",
            objective="  revised objective  ",
            max_goal_rounds=5,
        )
        self.assertEqual(edited["previous_ref"], {"id": "goal-lifecycle-1", "revision": 3})
        self.assertEqual(edited["ref"], {"id": "goal-lifecycle-1", "revision": 4})
        after_edit = self.backend.goal_status("cold-1")["goal"]["goal"]
        self.assertEqual(after_edit["phase"], "blocked")
        self.assertEqual(after_edit["objective"], "revised objective")
        self.assertEqual(after_edit["maxGoalRounds"], 5)

        resumed = self.backend.goal_resume("cold-1")
        self.assertEqual(resumed["previous_ref"], {"id": "goal-lifecycle-1", "revision": 4})
        self.assertEqual(resumed["ref"], {"id": "goal-lifecycle-1", "revision": 5})
        self.assertEqual(self.backend.goal_status("cold-1")["goal"]["goal"]["phase"], "active")

        completed = self.backend.goal_complete("cold-1")
        self.assertEqual(completed["previous_ref"], {"id": "goal-lifecycle-1", "revision": 5})
        self.assertEqual(completed["ref"], {"id": "goal-lifecycle-1", "revision": 6})
        self.assertEqual(self.backend.goal_status("cold-1")["goal"]["goal"]["phase"], "complete")

        cleared = self.backend.goal_clear("cold-1")
        self.assertEqual(cleared["previous_ref"], {"id": "goal-lifecycle-1", "revision": 6})
        self.assertTrue(cleared["cleared"])
        self.assertIsNone(self.backend.goal_status("cold-1")["goal"])

        edit_calls = [payload for method, payload in self.server.calls if method == "goal.edit"]
        self.assertEqual(
            edit_calls,
            [
                {
                    "sessionId": "cold-1",
                    "ref": {"id": "goal-lifecycle-1", "revision": 3},
                    "objective": "revised objective",
                    "maxGoalRounds": 5,
                }
            ],
        )

    def test_goal_edit_validates_requested_changes(self) -> None:
        self.server.sessions["cold-1"]["goal"] = {
            "goal": {
                "id": "goal-edit-1",
                "revision": 1,
                "objective": "work",
                "phase": "paused",
                "maxGoalRounds": 5,
            },
            "roundsStarted": 1,
            "createdAt": 1,
            "updatedAt": 1,
        }
        with self.assertRaisesRegex(ValueError, "requires objective and/or"):
            self.backend.goal_edit("cold-1")
        with self.assertRaisesRegex(ValueError, "objective"):
            self.backend.goal_edit("cold-1", objective="   ")
        with self.assertRaisesRegex(ValueError, "max_goal_rounds"):
            self.backend.goal_edit("cold-1", max_goal_rounds=0)

    def test_goal_resume_uses_projection_cas_after_cold_attach(self) -> None:
        self.server.sessions["cold-1"]["goal"] = {
            "goal": {
                "id": "goal-1",
                "revision": 7,
                "objective": "continue after restart",
                "phase": "active",
                "maxGoalRounds": 20,
            },
            "roundsStarted": 3,
            "createdAt": 1,
            "updatedAt": 2,
        }
        self.server.calls.clear()

        status = self.backend.goal_status("cold-1")
        self.assertEqual(status["goal"]["goal"]["revision"], 7)
        self.assertEqual(status["activation"], "not-exposed-by-durable-projection")

        resumed = self.backend.goal_resume("cold-1")
        self.assertEqual(resumed["previous_ref"], {"id": "goal-1", "revision": 7})
        self.assertEqual(resumed["ref"], {"id": "goal-1", "revision": 8})
        goal_resume_calls = [payload for method, payload in self.server.calls if method == "goal.resume"]
        self.assertEqual(goal_resume_calls, [{"sessionId": "cold-1", "ref": {"id": "goal-1", "revision": 7}}])
        self.assertEqual(self.backend.presence("cold-1"), SessionPresence.LIVE)

        paused = self.backend.goal_pause("cold-1")
        self.assertEqual(paused["previous_ref"], {"id": "goal-1", "revision": 8})
        self.assertEqual(paused["ref"], {"id": "goal-1", "revision": 9})

    def test_prompt_receipt_is_host_rpc_id_and_cancel_works(self) -> None:
        receipt = GatewayService(self.backend).start("work", session_id="fresh-1")
        self.assertEqual(receipt.action, "created")
        self.assertTrue(receipt.message_id)
        self.assertEqual(self.backend.status("fresh-1")["state"], "live")
        canceled = self.backend.cancel("fresh-1")
        self.assertTrue(canceled["canceled"])
        self.assertEqual(self.backend.status("fresh-1")["state"], "persisted")
        self.assertEqual(self.backend.status("fresh-1")["status"], "not-running")


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
        self.on_prompt: Any = None
        self.subscription = FakeSubscription()

    def session_prompt(self, session_id: str, content_blocks: list[dict[str, Any]]) -> str:
        self.calls.append(("session_prompt", session_id, content_blocks))
        if self.on_prompt is not None:
            self.on_prompt(session_id)
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

    def test_catalog_instances_do_not_overwrite_each_others_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            first = SessionCatalog(path)
            second = SessionCatalog(path)

            first.add("s1")
            second.add("s2")

            self.assertEqual(SessionCatalog(path).ids(), ["s1", "s2"])

    def test_live_catalog_instances_refresh_external_updates_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            first = SessionCatalog(path)
            second = SessionCatalog(path)

            first.add("s1")

            self.assertTrue(second.contains("s1"))
            self.assertEqual(second.ids(), ["s1"])

    def test_catalog_updates_wait_for_cross_process_disk_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            lock_path = path.with_name(f".{path.name}.lock")
            lock_path.touch(mode=0o600)
            lock_file = lock_path.open("rb+")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            ctx = multiprocessing.get_context("fork")
            finished = ctx.Event()

            def add_from_child() -> None:
                SessionCatalog(path).add("child")
                finished.set()

            process = ctx.Process(target=add_from_child)
            process.start()
            try:
                self.assertFalse(finished.wait(timeout=0.1))
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            process.join(timeout=2)

            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(finished.is_set())
            self.assertEqual(SessionCatalog(path).ids(), ["child"])

    def test_catalog_process_lock_rejects_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sessions.json"
            target = root / "target.txt"
            target.write_text("sentinel\n", encoding="utf-8")
            target.chmod(0o644)
            path.with_name(f".{path.name}.lock").symlink_to(target)

            with self.assertRaises(OSError):
                SessionCatalog(path).add("s1")

            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")
            self.assertFalse(path.exists())

    def test_catalog_path_rejects_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sessions.json"
            target = root / "target.json"
            target.write_text('{"version":1,"sessions":["victim"]}\n', encoding="utf-8")
            target.chmod(0o644)
            path.symlink_to(target)

            with self.assertRaises(OSError):
                SessionCatalog(path)

            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                '{"version":1,"sessions":["victim"]}\n',
            )

    def test_catalog_file_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            previous_umask = os.umask(0o022)
            try:
                SessionCatalog(path).add("s1")
            finally:
                os.umask(previous_umask)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            path.chmod(0o644)
            SessionCatalog(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_catalog_temp_file_is_private_before_sensitive_write_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            observed_modes: list[int] = []
            original_write = os.write

            def recording_write(fd: int, data: bytes) -> int:
                observed_modes.append(os.fstat(fd).st_mode & 0o777)
                return original_write(fd, data)

            previous_umask = os.umask(0o022)
            try:
                with patch("dsh_mcp_gateway.backend.os.write", recording_write):
                    SessionCatalog(path).add("s1")
            finally:
                os.umask(previous_umask)

            self.assertTrue(observed_modes)
            self.assertTrue(all(mode == 0o600 for mode in observed_modes))

    def test_catalog_temp_file_cannot_be_swapped_to_symlink_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sessions.json"
            target = root / "target.txt"
            target.write_text("sentinel\n", encoding="utf-8")
            original_write = os.write
            swapped = False

            def swap_after_open(fd: int, data: bytes) -> int:
                nonlocal swapped
                opened = Path(os.readlink(f"/proc/self/fd/{fd}"))
                if not swapped and opened.name.startswith(".sessions.json.") and opened.name.endswith(".tmp"):
                    opened.unlink()
                    opened.symlink_to(target)
                    swapped = True
                return original_write(fd, data)

            with (
                patch("dsh_mcp_gateway.backend.os.write", swap_after_open),
                self.assertRaises(OSError),
            ):
                SessionCatalog(path).add("s1")

            self.assertTrue(swapped)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")
            self.assertFalse(path.exists())

    def test_catalog_temp_file_cannot_be_swapped_to_symlink_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sessions.json"
            target = root / "target.txt"
            target.write_text("sentinel\n", encoding="utf-8")
            original_replace = os.replace
            swapped = False

            def swap_before_replace(src: str | bytes, dst: str | bytes, *args: Any, **kwargs: Any) -> None:
                nonlocal swapped
                source = Path(src)
                if source.name.startswith(".sessions.json.") and source.name.endswith(".tmp"):
                    source.unlink()
                    source.symlink_to(target)
                    swapped = True
                original_replace(src, dst, *args, **kwargs)

            with (
                patch("dsh_mcp_gateway.backend.os.replace", swap_before_replace),
                self.assertRaises(OSError),
            ):
                SessionCatalog(path).add("s1")

            self.assertTrue(swapped)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")
            self.assertFalse(path.exists())

    def test_catalog_rolls_back_memory_when_persistence_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            catalog = SessionCatalog(path)

            with (
                patch.object(Path, "replace", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                catalog.add("s1")
            self.assertFalse(catalog.contains("s1"))
            self.assertFalse(path.exists())

            catalog.add("s1")
            with (
                patch.object(Path, "replace", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                catalog.remove("s1")
            self.assertTrue(catalog.contains("s1"))
            self.assertTrue(SessionCatalog(path).contains("s1"))

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
            status = backend.status("s1")
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["event_count"], 1)
            self.assertEqual(status["retained_event_count"], 1)
            self.assertFalse(status["history_truncated"])
            self.assertEqual(backend.history("s1"), [{"type": "turn/start", "seq": 1}])

    def test_live_event_projection_uses_bounded_ring_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = PublicSdkBackend(
                FakePublicSdkClient(),
                SessionCatalog(Path(tmp) / "sessions.json"),
                event_buffer_size=2,
            )
            for seq in range(1, 6):
                backend.observe_notification(
                    "session.event",
                    {"sessionId": "s1", "event": {"type": "assistant/chunk", "seq": seq}},
                )

            self.assertEqual([event["seq"] for event in backend.history("s1", limit=1000)], [4, 5])
            status = backend.status("s1")
            self.assertEqual(status["event_count"], 5)
            self.assertEqual(status["retained_event_count"], 2)
            self.assertTrue(status["history_truncated"])
            with self.assertRaisesRegex(ValueError, "event_buffer_size"):
                PublicSdkBackend(
                    FakePublicSdkClient(),
                    SessionCatalog(Path(tmp) / "other-sessions.json"),
                    event_buffer_size=0,
                )

    def test_public_sdk_compact_transcript_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakePublicSdkClient()
            backend = PublicSdkBackend(client, SessionCatalog(Path(tmp) / "sessions.json"))
            GatewayService(backend).start("work", session_id="s1")
            with self.assertRaisesRegex(MessageHistoryUnavailable, "authoritative durable transcript"):
                backend.messages("s1")
            with self.assertRaisesRegex(ValueError, "limit"):
                backend.messages("s1", limit=0)

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

    def test_failed_prompt_keeps_catalog_if_notification_already_marked_session_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            client = FakePublicSdkClient()
            backend = PublicSdkBackend(client, SessionCatalog(path))
            backend.create("s1")
            client.on_prompt = lambda session_id: backend.observe_notification(
                "session.status",
                {"sessionId": session_id, "status": "running"},
            )
            client.fail = True

            with self.assertRaisesRegex(RuntimeError, "sdk prompt failed"):
                backend.prompt("s1", "work")

            self.assertEqual(backend.presence("s1"), SessionPresence.LIVE)
            self.assertTrue(SessionCatalog(path).contains("s1"))

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

    def test_public_sdk_goal_controls_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = PublicSdkBackend(FakePublicSdkClient(), SessionCatalog(Path(tmp) / "sessions.json"))
            GatewayService(backend).start("work", session_id="s1")
            with self.assertRaises(GoalControlUnavailable):
                backend.goal_status("s1")
            with self.assertRaises(GoalControlUnavailable):
                backend.goal_create("s1", "work", max_goal_rounds=5)
            with self.assertRaises(GoalControlUnavailable):
                backend.goal_edit("s1", max_goal_rounds=10)
            with self.assertRaises(GoalControlUnavailable):
                backend.goal_resume("s1")
            with self.assertRaises(GoalControlUnavailable):
                backend.goal_pause("s1")
            with self.assertRaises(GoalControlUnavailable):
                backend.goal_complete("s1")
            with self.assertRaises(GoalControlUnavailable):
                backend.goal_clear("s1")

    def test_public_sdk_durable_history_pagination_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = PublicSdkBackend(FakePublicSdkClient(), SessionCatalog(Path(tmp) / "sessions.json"))
            GatewayService(backend).start("work", session_id="s1")
            with self.assertRaisesRegex(HistoryPaginationUnavailable, "authoritative durable pages"):
                backend.history_page("s1")

    def test_public_sdk_durable_session_search_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = PublicSdkBackend(FakePublicSdkClient(), SessionCatalog(Path(tmp) / "sessions.json"))
            GatewayService(backend).start("work", session_id="s1")
            with self.assertRaisesRegex(SessionSearchUnavailable, "authoritative durable session messages"):
                backend.search_sessions("work")

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
        self.assertEqual(server.version, __version__)
        tools = await server.list_tools()
        self.assertEqual(
            [tool.name for tool in tools],
            [
                "dsh_start",
                "dsh_continue",
                "dsh_status",
                "dsh_history",
                "dsh_history_page",
                "dsh_messages",
                "dsh_list",
                "dsh_search",
                "dsh_cancel",
                "dsh_goal_status",
                "dsh_goal_create",
                "dsh_goal_edit",
                "dsh_goal_resume",
                "dsh_goal_pause",
                "dsh_goal_complete",
                "dsh_goal_clear",
            ],
        )
        start = tools[0]
        self.assertEqual(start.input_schema["required"], ["prompt"])
        self.assertEqual(set(start.input_schema["properties"]), {"prompt", "session_id"})

        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(set(by_name["dsh_list"].input_schema["properties"]), {"limit", "offset"})
        self.assertEqual(by_name["dsh_list"].input_schema.get("required", []), [])
        for name in (
            "dsh_status",
            "dsh_history",
            "dsh_history_page",
            "dsh_messages",
            "dsh_list",
            "dsh_search",
            "dsh_goal_status",
        ):
            annotations = by_name[name].annotations
            assert annotations is not None
            self.assertTrue(annotations.read_only_hint)
            self.assertFalse(annotations.destructive_hint)
            self.assertTrue(annotations.idempotent_hint)
            self.assertFalse(annotations.open_world_hint)

        for name in (
            "dsh_start",
            "dsh_continue",
            "dsh_cancel",
            "dsh_goal_create",
            "dsh_goal_edit",
            "dsh_goal_resume",
            "dsh_goal_pause",
            "dsh_goal_complete",
            "dsh_goal_clear",
        ):
            annotations = by_name[name].annotations
            assert annotations is not None
            self.assertFalse(annotations.read_only_hint)
            self.assertTrue(annotations.destructive_hint)
            self.assertFalse(annotations.idempotent_hint)
            self.assertTrue(annotations.open_world_hint)

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

    async def test_history_page_tool_returns_structured_cursor(self) -> None:
        backend = FakeBackend(SessionPresence.PERSISTED)
        server = build_mcp_server(GatewayService(backend))
        result = await server.call_tool(
            "dsh_history_page",
            {"session_id": "s1", "before_seq": 42, "max_messages": 25},
        )
        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {
                "session_id": "s1",
                "events": [{"type": "turn/end", "seq": 10}],
                "has_more": False,
                "next_before_seq": None,
            },
        )
        self.assertEqual(backend.calls, [("history_page", "s1", 42, 25)])

    async def test_messages_tool_returns_compact_transcript(self) -> None:
        backend = FakeBackend(SessionPresence.PERSISTED)
        server = build_mcp_server(GatewayService(backend))
        result = await server.call_tool(
            "dsh_messages",
            {"session_id": "s1", "before_seq": 42, "limit": 20},
        )
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["messages"][0]["text"], "latest answer")
        self.assertEqual(result.structured_content["next_before_seq"], None)
        self.assertEqual(backend.calls, [("messages", "s1", 42, 20)])

    async def test_list_tool_returns_bounded_page(self) -> None:
        backend = FakeBackend(SessionPresence.PERSISTED)
        backend.list_sessions = lambda: [
            {"session_id": "s1"},
            {"session_id": "s2"},
            {"session_id": "s3"},
        ]
        server = build_mcp_server(GatewayService(backend))
        result = await server.call_tool("dsh_list", {"limit": 2, "offset": 1})
        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {
                "items": [{"session_id": "s2"}, {"session_id": "s3"}],
                "total": 3,
                "has_more": False,
                "next_offset": None,
            },
        )

    async def test_search_tool_returns_structured_session_match(self) -> None:
        backend = FakeBackend(SessionPresence.PERSISTED)
        server = build_mcp_server(GatewayService(backend))
        result = await server.call_tool("dsh_search", {"query": "remembered phrase"})
        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {
                "query": "remembered phrase",
                "items": [{"session_id": "s1", "snippet": "matching text"}],
                "has_more": False,
            },
        )
        self.assertEqual(backend.calls, [("search_sessions", "remembered phrase")])

    async def test_goal_create_tool_returns_structured_ref(self) -> None:
        backend = FakeBackend(SessionPresence.LIVE)
        server = build_mcp_server(GatewayService(backend))
        result = await server.call_tool(
            "dsh_goal_create",
            {"session_id": "s1", "objective": "finish work", "max_goal_rounds": 12},
        )
        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {"session_id": "s1", "action": "created", "ref": {"id": "g1", "revision": 1}},
        )
        self.assertEqual(backend.calls, [("goal_create", "s1", "finish work", 12)])

    async def test_goal_lifecycle_tools_delegate_structured_operations(self) -> None:
        backend = FakeBackend(SessionPresence.LIVE)
        server = build_mcp_server(GatewayService(backend))

        edited = await server.call_tool(
            "dsh_goal_edit",
            {"session_id": "s1", "objective": "revised", "max_goal_rounds": 20},
        )
        self.assertFalse(edited.is_error)
        self.assertEqual(edited.structured_content["action"], "edited")

        completed = await server.call_tool("dsh_goal_complete", {"session_id": "s1"})
        self.assertFalse(completed.is_error)
        self.assertEqual(completed.structured_content["action"], "completed")

        cleared = await server.call_tool("dsh_goal_clear", {"session_id": "s1"})
        self.assertFalse(cleared.is_error)
        self.assertEqual(cleared.structured_content["action"], "cleared")
        self.assertEqual(
            backend.calls,
            [
                ("goal_edit", "s1", "revised", 20),
                ("goal_complete", "s1"),
                ("goal_clear", "s1"),
            ],
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
