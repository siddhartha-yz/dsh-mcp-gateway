from __future__ import annotations

import ipaddress
import json
import os
import threading
import uuid
from http.client import HTTPException
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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


class ExperimentalWebHostError(RuntimeError):
    """DSH developer-preview Web Host API transport or business failure."""


class ExperimentalWebHostBackend:
    """Cold-resumable adapter over DSH's developer-preview Web Host API.

    This deliberately targets an external DSH Web Host rather than owning its
    process. The Host API currently has no stable protocol version and DSH's Web
    server has no authentication, so non-loopback targets are refused by
    default. Put this gateway in front of a loopback/private DSH Host instead.
    """

    def __init__(
        self,
        base_url: str,
        *,
        cwd: str | os.PathLike[str],
        timeout_s: float = 10.0,
        allow_non_loopback: bool = False,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute http(s) URL")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if not allow_non_loopback and not self._is_loopback_host(parsed.hostname):
            raise ValueError(
                "DSH Web Host has no network authentication; use a loopback base_url "
                "or explicitly set allow_non_loopback=True behind a trusted private network"
            )
        self.base_url = base_url.rstrip("/")
        self.cwd = str(Path(cwd).resolve())
        self.timeout_s = timeout_s
        # session.list.running reports turn activity, not attachment. Keep the
        # sessions this adapter has explicitly created/resumed so an idle live
        # agent is not repeatedly misclassified as cold within one gateway run.
        self._attached_sessions: set[str] = set()
        self._attached_lock = threading.Lock()

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def describe_host(self) -> dict[str, Any]:
        """Return the Host diagnostic descriptor.

        `version` is currently a DSH placeholder, not a protocol compatibility
        marker, so this method must not be used as a schema-version gate.
        """
        return self._call("host.describe", {})

    def presence(self, session_id: str) -> SessionPresence:
        with self._attached_lock:
            if session_id in self._attached_sessions:
                return SessionPresence.LIVE
        for item in self.list_sessions():
            if item.get("session_id") != session_id:
                continue
            if item.get("state") == "live":
                with self._attached_lock:
                    self._attached_sessions.add(session_id)
                return SessionPresence.LIVE
            return SessionPresence.PERSISTED
        return SessionPresence.ABSENT

    def reuse(self, session_id: str) -> SessionHandle:
        if self.presence(session_id) is not SessionPresence.LIVE:
            raise KeyError(session_id)
        return SessionHandle(session_id)

    def resume(self, session_id: str) -> SessionHandle:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        # session.models is intentionally turn-free but resolves through the
        # Host's shared agent resolver, which cold-resumes ordinary sessions.
        self._call("session.models", {"sessionId": session_id})
        with self._attached_lock:
            self._attached_sessions.add(session_id)
        return SessionHandle(session_id)

    def create(self, session_id: str | None = None) -> SessionHandle:
        payload: dict[str, Any] = {"cwd": self.cwd}
        if session_id is not None:
            payload["sessionId"] = session_id
        value = self._call("session.create", payload)
        created = value.get("sessionId")
        if not isinstance(created, str) or not created:
            raise ExperimentalWebHostError("session.create returned no sessionId")
        with self._attached_lock:
            self._attached_sessions.add(created)
        return SessionHandle(created)

    def prompt(self, session_id: str, text: str) -> str:
        rpc_id, value = self._call_with_id(
            "session.prompt",
            {
                "sessionId": session_id,
                "mode": "queue",
                "content": [{"type": "text", "text": text}],
            },
        )
        if value.get("accepted") is not True:
            raise ExperimentalWebHostError("session.prompt was not accepted")
        # The Host does not return a MessageId; its durable user/message source
        # carries this exact rpcId, so it is the stable admission receipt.
        return rpc_id

    def status(self, session_id: str) -> dict[str, Any]:
        presence = self.presence(session_id)
        if presence is SessionPresence.ABSENT:
            return {"session_id": session_id, "state": "absent"}
        for item in self.list_sessions():
            if item.get("session_id") == session_id:
                return item
        return {"session_id": session_id, "state": "absent"}

    def history(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or limit > 1000:
            raise ValueError("limit must be an integer in [1, 1000]")
        value = self._call(
            "session.history",
            {"sessionId": session_id, "maxMessages": limit},
        )
        entries = value.get("events")
        if not isinstance(entries, list):
            raise ExperimentalWebHostError("session.history returned invalid events")
        events: list[dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("event"), dict):
                events.append(dict(entry["event"]))
        return events[-limit:]

    def list_sessions(self) -> list[dict[str, Any]]:
        value = self._call("session.list", {})
        items = value.get("items")
        if not isinstance(items, list):
            raise ExperimentalWebHostError("session.list returned invalid items")
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("sessionId"), str):
                continue
            session_id = item["sessionId"]
            running = item.get("running") is True
            with self._attached_lock:
                known_attached = session_id in self._attached_sessions
            live = running or known_attached
            result.append(
                {
                    "session_id": session_id,
                    "state": "live" if live else "persisted",
                    "status": "running" if running else ("idle" if known_attached else "not-running"),
                    "updated_at": item.get("updatedAt"),
                    "cwd": item.get("cwd"),
                    "blank": item.get("blank"),
                    "agent_preset": item.get("agentPreset"),
                }
            )
        return result

    def cancel(self, session_id: str) -> dict[str, Any]:
        try:
            value = self._call("session.cancel", {"sessionId": session_id})
        except ExperimentalWebHostError as exc:
            return {"session_id": session_id, "canceled": False, "reason": str(exc)}
        return {
            "session_id": session_id,
            "canceled": value.get("accepted") is True,
        }

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        _rpc_id, value = self._call_with_id(method, payload)
        return value

    def _call_with_id(self, method: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        rpc_id = str(uuid.uuid4())
        body = json.dumps(
            {
                "type": "client-request",
                "rpcId": rpc_id,
                "method": method,
                "payload": payload,
            },
            separators=(",", ":"),
        ).encode()
        request = Request(
            f"{self.base_url}/api/{method}",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise ExperimentalWebHostError(
                f"{method} transport failed with HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except URLError as exc:
            raise ExperimentalWebHostError(f"{method} transport failed: {exc.reason}") from exc
        except (HTTPException, OSError) as exc:
            raise ExperimentalWebHostError(f"{method} transport failed: {exc}") from exc
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExperimentalWebHostError(f"{method} returned invalid JSON") from exc
        if not isinstance(envelope, dict) or envelope.get("type") != "server-response":
            raise ExperimentalWebHostError(f"{method} returned invalid response envelope")
        if envelope.get("rpcId") != rpc_id:
            raise ExperimentalWebHostError(f"{method} rpcId mismatch")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ExperimentalWebHostError(f"{method} returned invalid result")
        if result.get("ok") is not True:
            error = result.get("error")
            if isinstance(error, dict):
                code = error.get("code", "unknown")
                message = error.get("message", "unknown DSH Web Host error")
                raise ExperimentalWebHostError(f"{method} [{code}]: {message}")
            raise ExperimentalWebHostError(f"{method} failed")
        value = result.get("value")
        if not isinstance(value, dict):
            raise ExperimentalWebHostError(f"{method} returned a non-object value")
        return rpc_id, value


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
            except Exception as exc:  # noqa: BLE001 - SDK subscription is an external transport boundary.
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
