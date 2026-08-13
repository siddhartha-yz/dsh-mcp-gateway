from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .backend import DshSessionBackend
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

    def ensure(self, session_id: str | None = None) -> EnsureResult:
        if session_id is None:
            return EnsureResult(self._backend.create(), EnsureAction.CREATED)

        presence = self._backend.presence(session_id)
        if presence is SessionPresence.LIVE:
            return EnsureResult(self._backend.reuse(session_id), EnsureAction.REUSED)
        if presence is SessionPresence.PERSISTED:
            return EnsureResult(self._backend.resume(session_id), EnsureAction.RESUMED)
        if presence is SessionPresence.ABSENT:
            return EnsureResult(self._backend.create(session_id), EnsureAction.CREATED)

        raise AssertionError(f"unhandled session presence: {presence!r}")
