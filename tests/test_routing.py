from __future__ import annotations

import unittest

from dsh_mcp_gateway.routing import EnsureAction, SessionRouter
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


if __name__ == "__main__":
    unittest.main()
