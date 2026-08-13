from __future__ import annotations

from typing import Protocol

from .types import SessionHandle, SessionPresence


class DshSessionBackend(Protocol):
    def presence(self, session_id: str) -> SessionPresence: ...

    def reuse(self, session_id: str) -> SessionHandle: ...

    def resume(self, session_id: str) -> SessionHandle: ...

    def create(self, session_id: str | None = None) -> SessionHandle: ...
