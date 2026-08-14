from __future__ import annotations

import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from .backend import DshControlBackend, DshSessionBackend
from .types import SessionHandle, SessionPresence


class EnsureAction(str, Enum):
    REUSED = "reused"
    RESUMED = "resumed"
    CREATED = "created"


@dataclass(frozen=True, slots=True)
class EnsureResult:
    handle: SessionHandle
    action: EnsureAction


class SessionRouter:
    def __init__(self, backend: DshSessionBackend) -> None:
        self._backend = backend
        self._session_locks_guard = threading.Lock()
        self._session_locks: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()

    def ensure(self, session_id: str | None = None) -> EnsureResult:
        if session_id is None:
            return EnsureResult(self._backend.create(), EnsureAction.CREATED)

        with self.admission(session_id):
            presence = self._backend.presence(session_id)
            if presence is SessionPresence.LIVE:
                return EnsureResult(self._backend.reuse(session_id), EnsureAction.REUSED)
            if presence is SessionPresence.PERSISTED:
                return EnsureResult(self._backend.resume(session_id), EnsureAction.RESUMED)
            if presence is SessionPresence.ABSENT:
                return EnsureResult(self._backend.create(session_id), EnsureAction.CREATED)

            raise AssertionError(f"unhandled session presence: {presence!r}")

    @contextmanager
    def admission(self, session_id: str | None) -> Iterator[None]:
        if session_id is None:
            yield
            return
        with self._lock_for(session_id):
            yield

    def _lock_for(self, session_id: str) -> Any:
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[session_id] = lock
            return lock


@dataclass(frozen=True, slots=True)
class PromptReceipt:
    session_id: str
    action: str
    message_id: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class GatewayService:
    """Stable control API that future MCP adapters can expose unchanged."""

    def __init__(self, backend: DshControlBackend) -> None:
        self._backend = backend
        self._router = SessionRouter(backend)

    def start(self, prompt: str, *, session_id: str | None = None) -> PromptReceipt:
        with self._router.admission(session_id):
            ensured = self._router.ensure(session_id)
            message_id = self._backend.prompt(ensured.handle.session_id, prompt)
            return PromptReceipt(
                session_id=ensured.handle.session_id,
                action=ensured.action.value,
                message_id=message_id,
            )

    def continue_session(self, session_id: str, prompt: str) -> PromptReceipt:
        return self.start(prompt, session_id=session_id)

    def status(self, session_id: str) -> dict[str, Any]:
        return self._backend.status(session_id)

    def history(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._backend.history(session_id, limit=limit)

    def history_page(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        max_messages: int = 50,
    ) -> dict[str, Any]:
        return self._backend.history_page(
            session_id,
            before_seq=before_seq,
            max_messages=max_messages,
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._backend.list_sessions()

    def search_sessions(self, query: str) -> dict[str, Any]:
        return self._backend.search_sessions(query)

    def cancel(self, session_id: str) -> dict[str, Any]:
        return self._backend.cancel(session_id)

    def goal_status(self, session_id: str) -> dict[str, Any]:
        return self._backend.goal_status(session_id)

    def goal_create(
        self,
        session_id: str,
        objective: str,
        *,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        return self._backend.goal_create(
            session_id,
            objective,
            max_goal_rounds=max_goal_rounds,
        )

    def goal_edit(
        self,
        session_id: str,
        *,
        objective: str | None = None,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        return self._backend.goal_edit(
            session_id,
            objective=objective,
            max_goal_rounds=max_goal_rounds,
        )

    def goal_resume(self, session_id: str) -> dict[str, Any]:
        return self._backend.goal_resume(session_id)

    def goal_pause(self, session_id: str) -> dict[str, Any]:
        return self._backend.goal_pause(session_id)

    def goal_complete(self, session_id: str) -> dict[str, Any]:
        return self._backend.goal_complete(session_id)

    def goal_clear(self, session_id: str) -> dict[str, Any]:
        return self._backend.goal_clear(session_id)
