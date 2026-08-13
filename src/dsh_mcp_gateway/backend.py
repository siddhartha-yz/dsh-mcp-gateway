from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Protocol

from .types import SessionHandle, SessionPresence


class DshSessionBackend(Protocol):
    def presence(self, session_id: str) -> SessionPresence: ...

    def reuse(self, session_id: str) -> SessionHandle: ...

    def resume(self, session_id: str) -> SessionHandle: ...

    def create(self, session_id: str | None = None) -> SessionHandle: ...


class DshControlBackend(DshSessionBackend, Protocol):
    def prompt(self, session_id: str, text: str) -> str: ...

    def status(self, session_id: str) -> dict[str, Any]: ...

    def history(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...

    def cancel(self, session_id: str) -> dict[str, Any]: ...


class PublicSdkSubscription(Protocol):
    def drain(self, on_notification: Any) -> None: ...

    def close(self) -> None: ...


class PublicSdkClient(Protocol):
    def session_prompt(self, session_id: str, content_blocks: list[dict[str, Any]]) -> str: ...

    def subscribe_notifications(self, notification_filter: Any = None) -> PublicSdkSubscription: ...


class ColdResumeUnavailable(RuntimeError):
    """The current public DSH SDK cannot reopen an on-disk session."""


class SessionCatalog:
    """Small gateway-owned index used to distinguish persisted from absent ids."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ids = self._load()

    def contains(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._ids

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._ids)

    def add(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._ids:
                return
            self._ids.add(session_id)
            self._save()

    def remove(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._ids:
                return
            self._ids.remove(session_id)
            self._save()

    def _load(self) -> set[str]:
        if not self.path.exists():
            return set()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("sessions"), list):
            raise ValueError(f"invalid session catalog: {self.path}")
        sessions = raw["sessions"]
        if not all(isinstance(item, str) and item for item in sessions):
            raise ValueError(f"invalid session catalog ids: {self.path}")
        return set(sessions)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps({"version": 1, "sessions": sorted(self._ids)}, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)


class PublicSdkBridge:
    """Project public-SDK notifications into a `PublicSdkBackend`.

    The bridge does not own or close the SDK client itself. It owns only the
    notification subscription, so client/runtime lifecycle can remain outside
    this adapter while the MCP-facing state projection stays replaceable.
    """

    def __init__(
        self,
        client: PublicSdkClient,
        catalog: SessionCatalog,
        *,
        poll_interval_s: float = 0.05,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.backend = PublicSdkBackend(client, catalog)
        self._subscription = client.subscribe_notifications()
        self._poll_interval_s = poll_interval_s
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: BaseException | None = None

    def start(self) -> None:
        if self._closed.is_set():
            raise RuntimeError("bridge is closed")
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="dsh-public-sdk-events", daemon=True)
        self._thread.start()

    def poll_once(self) -> None:
        if self._closed.is_set():
            return
        self._subscription.drain(self._record)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._subscription.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self._poll_interval_s * 4))

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                self.poll_once()
            except BaseException as exc:
                self.last_error = exc
                return
            self._closed.wait(self._poll_interval_s)

    def _record(self, notification: Any) -> None:
        method = getattr(notification, "method", None)
        payload = getattr(notification, "payload", None)
        if method is None and isinstance(notification, dict):
            method = notification.get("method")
            payload = notification.get("payload") or notification.get("params")
        if isinstance(method, str) and isinstance(payload, dict):
            self.backend.observe_notification(method, payload)


class PublicSdkBackend:
    """Live-session adapter for the public DSH Python SDK.

    It deliberately fails closed for catalogued sessions after a gateway/runtime
    restart because the current SDK wire exposes create/prompt but not cold
    resume. A future resumable transport can replace this class without changing
    GatewayService or the MCP tool schemas.
    """

    def __init__(self, client: PublicSdkClient, catalog: SessionCatalog) -> None:
        self._client = client
        self._catalog = catalog
        self._lock = threading.RLock()
        self._allocated: set[str] = set()
        self._live: set[str] = set()
        self._statuses: dict[str, str] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}

    def presence(self, session_id: str) -> SessionPresence:
        with self._lock:
            if session_id in self._allocated or session_id in self._live:
                return SessionPresence.LIVE
        if self._catalog.contains(session_id):
            return SessionPresence.PERSISTED
        return SessionPresence.ABSENT

    def reuse(self, session_id: str) -> SessionHandle:
        with self._lock:
            if session_id not in self._allocated and session_id not in self._live:
                raise KeyError(session_id)
        return SessionHandle(session_id)

    def resume(self, session_id: str) -> SessionHandle:
        if not self._catalog.contains(session_id):
            raise KeyError(session_id)
        raise ColdResumeUnavailable(
            f"session {session_id!r} is persisted, but the current public DSH SDK cannot cold-resume it"
        )

    def create(self, session_id: str | None = None) -> SessionHandle:
        session_id = session_id or f"session-{uuid.uuid4()}"
        with self._lock:
            if session_id in self._allocated or session_id in self._live or self._catalog.contains(session_id):
                raise ValueError(f"session already exists: {session_id}")
            self._allocated.add(session_id)
            self._catalog.add(session_id)
        return SessionHandle(session_id)

    def prompt(self, session_id: str, text: str) -> str:
        with self._lock:
            if session_id not in self._allocated and session_id not in self._live:
                raise KeyError(session_id)
            newly_allocated = session_id in self._allocated
        try:
            message_id = self._client.session_prompt(
                session_id,
                [{"type": "text", "text": text}],
            )
        except Exception:
            if newly_allocated:
                with self._lock:
                    self._allocated.discard(session_id)
                    self._catalog.remove(session_id)
            raise
        with self._lock:
            self._allocated.discard(session_id)
            self._live.add(session_id)
        return message_id

    def observe_notification(self, method: str, payload: dict[str, Any]) -> None:
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str):
            return
        with self._lock:
            self._catalog.add(session_id)
            self._allocated.discard(session_id)
            self._live.add(session_id)
            if method == "session.status" and isinstance(payload.get("status"), str):
                self._statuses[session_id] = payload["status"]
            elif method == "session.event" and isinstance(payload.get("event"), dict):
                self._events.setdefault(session_id, []).append(dict(payload["event"]))

    def status(self, session_id: str) -> dict[str, Any]:
        presence = self.presence(session_id)
        with self._lock:
            if presence is SessionPresence.LIVE:
                state = "allocated" if session_id in self._allocated else "live"
                return {
                    "session_id": session_id,
                    "state": state,
                    "status": self._statuses.get(session_id, "unknown"),
                    "event_count": len(self._events.get(session_id, ())),
                }
        if presence is SessionPresence.PERSISTED:
            return {"session_id": session_id, "state": "persisted", "status": "cold"}
        return {"session_id": session_id, "state": "absent"}

    def history(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or limit > 1000:
            raise ValueError("limit must be an integer in [1, 1000]")
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        with self._lock:
            return [dict(event) for event in self._events.get(session_id, ())[-limit:]]

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self.status(session_id) for session_id in self._catalog.ids()]

    def cancel(self, session_id: str) -> dict[str, Any]:
        presence = self.presence(session_id)
        return {
            "session_id": session_id,
            "canceled": False,
            "reason": "unsupported-by-public-sdk" if presence is SessionPresence.LIVE else "not-live",
        }
