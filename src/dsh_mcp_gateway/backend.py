from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import threading
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
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

    def history_page(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        max_messages: int = 50,
    ) -> dict[str, Any]: ...

    def messages(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...

    def search_sessions(self, query: str) -> dict[str, Any]: ...

    def cancel(self, session_id: str) -> dict[str, Any]: ...

    def goal_status(self, session_id: str) -> dict[str, Any]: ...

    def goal_create(
        self,
        session_id: str,
        objective: str,
        *,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]: ...

    def goal_edit(
        self,
        session_id: str,
        *,
        objective: str | None = None,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]: ...

    def goal_resume(self, session_id: str) -> dict[str, Any]: ...

    def goal_pause(self, session_id: str) -> dict[str, Any]: ...

    def goal_complete(self, session_id: str) -> dict[str, Any]: ...

    def goal_clear(self, session_id: str) -> dict[str, Any]: ...


class PublicSdkSubscription(Protocol):
    def drain(self, on_notification: Any) -> None: ...

    def close(self) -> None: ...


class PublicSdkClient(Protocol):
    def session_prompt(self, session_id: str, content_blocks: list[dict[str, Any]]) -> str: ...

    def subscribe_notifications(self, notification_filter: Any = None) -> PublicSdkSubscription: ...


class ColdResumeUnavailable(RuntimeError):
    """The current public DSH SDK cannot reopen an on-disk session."""


class HistoryPaginationUnavailable(RuntimeError):
    """The selected backend cannot provide authoritative durable history pages."""


class MessageHistoryUnavailable(RuntimeError):
    """The selected backend cannot provide an authoritative compact transcript."""


class SessionSearchUnavailable(RuntimeError):
    """The selected backend cannot search authoritative durable session messages."""


class GoalControlUnavailable(RuntimeError):
    """The selected DSH transport does not expose durable goal controls."""


_SESSION_CATALOG_DISK_LOCK = threading.RLock()


class SessionCatalog:
    """Small gateway-owned index used to distinguish persisted from absent ids."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ids = self._load()

    def contains(self, session_id: str) -> bool:
        with self._lock, _SESSION_CATALOG_DISK_LOCK:
            self._ids = self._load()
            return session_id in self._ids

    def ids(self) -> list[str]:
        with self._lock, _SESSION_CATALOG_DISK_LOCK:
            self._ids = self._load()
            return sorted(self._ids)

    def add(self, session_id: str) -> None:
        with self._lock, _SESSION_CATALOG_DISK_LOCK, self._process_disk_lock():
            persisted = self._load()
            if session_id in persisted:
                self._ids = persisted
                return
            self._ids = persisted | {session_id}
            try:
                self._save()
            except (OSError, UnicodeError):
                self._ids = persisted
                raise

    def remove(self, session_id: str) -> None:
        with self._lock, _SESSION_CATALOG_DISK_LOCK, self._process_disk_lock():
            persisted = self._load()
            if session_id not in persisted:
                self._ids = persisted
                return
            self._ids = persisted - {session_id}
            try:
                self._save()
            except (OSError, UnicodeError):
                self._ids = persisted
                raise

    def _open_parent_dir(self) -> int:
        parent = self.path.parent.absolute()
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parent.parts[1:]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _process_disk_lock(self) -> Iterator[None]:
        parent_fd = self._open_parent_dir()
        lock_name = f".{self.path.name}.lock"
        try:
            descriptor = os.open(
                lock_name,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _load(self) -> set[str]:
        parent_fd = self._open_parent_dir()
        try:
            try:
                descriptor = os.open(
                    self.path.name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return set()
        finally:
            os.close(parent_fd)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, encoding="utf-8") as file:
                descriptor = -1
                raw = json.load(file)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("sessions"), list):
            raise ValueError(f"invalid session catalog: {self.path}")
        sessions = raw["sessions"]
        if not all(isinstance(item, str) and item for item in sessions):
            raise ValueError(f"invalid session catalog ids: {self.path}")
        return set(sessions)

    def _save(self) -> None:
        parent_fd = self._open_parent_dir()
        tmp_name = f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                tmp_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, 0o600)
            payload = (
                json.dumps({"version": 1, "sessions": sorted(self._ids)}, indent=2) + "\n"
            ).encode("utf-8")
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("failed to write session catalog temp file")
                offset += written
            opened = os.fstat(descriptor)
            linked = os.stat(tmp_name, dir_fd=parent_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
                raise OSError("session catalog temp file changed during save")
            os.replace(
                tmp_name,
                self.path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            published = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (published.st_dev, published.st_ino):
                os.unlink(self.path.name, dir_fd=parent_fd)
                raise OSError("session catalog changed during publication")
        except (OSError, UnicodeError):
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)


class ExperimentalWebHostError(RuntimeError):
    """DSH developer-preview Web Host API transport or business failure."""


MAX_WEB_HOST_RESPONSE_BYTES = 16 * 1024 * 1024


def _http_error_detail(error: HTTPError, *, limit: int) -> str:
    try:
        try:
            return error.read(limit + 1).decode("utf-8", "replace")[:limit]
        except (OSError, HTTPException, ValueError) as exc:
            return f"<error body unavailable: {type(exc).__name__}>"
    finally:
        try:
            error.close()
        except (OSError, HTTPException, ValueError):
            pass


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
            raise ValueError("base_url must be an absolute http(s) origin")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain user info")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an origin without a path, params, query, or fragment")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("base_url contains an invalid port") from exc
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

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def describe_host(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Return the Host diagnostic descriptor.

        `version` is currently a DSH placeholder, not a protocol compatibility
        marker, so this method must not be used as a schema-version gate.
        A caller may supply a shorter timeout for liveness/readiness probes
        without changing the normal control-RPC timeout.
        """
        return self._call("host.describe", {}, timeout_s=timeout_s)

    def presence(self, session_id: str) -> SessionPresence:
        for item in self.list_sessions():
            if item.get("session_id") != session_id:
                continue
            return SessionPresence.LIVE if item.get("state") == "live" else SessionPresence.PERSISTED
        return SessionPresence.ABSENT

    def reuse(self, session_id: str) -> SessionHandle:
        if self.presence(session_id) is not SessionPresence.LIVE:
            raise KeyError(session_id)
        return SessionHandle(session_id)

    def resume(self, session_id: str) -> SessionHandle:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        # session.models is intentionally turn-free but resolves through the
        # Host's shared agent resolver. DSH does not expose a Host boot id or a
        # reliable live-idle attachment bit, so this probe is deliberately used
        # for every existing non-running session: it reuses a live-idle Agent or
        # cold-resumes a persisted Agent after an independent Host restart.
        self._call("session.models", {"sessionId": session_id})
        return SessionHandle(session_id)

    def create(self, session_id: str | None = None) -> SessionHandle:
        payload: dict[str, Any] = {"cwd": self.cwd}
        if session_id is not None:
            payload["sessionId"] = session_id
        value = self._call("session.create", payload)
        created = value.get("sessionId")
        if not isinstance(created, str) or not created:
            raise ExperimentalWebHostError("session.create returned no sessionId")
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
        for item in self.list_sessions():
            if item.get("session_id") == session_id:
                return item
        return {"session_id": session_id, "state": "absent"}

    def history(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or limit > 1000:
            raise ValueError("limit must be an integer in [1, 1000]")
        value = self._history_page(session_id, limit=limit)
        entries = value.get("events")
        if not isinstance(entries, list):
            raise ExperimentalWebHostError("session.history returned invalid events")
        events: list[dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("event"), dict):
                events.append(dict(entry["event"]))
        return events[-limit:]

    def history_page(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        max_messages: int = 50,
    ) -> dict[str, Any]:
        if before_seq is not None and (
            not isinstance(before_seq, int) or isinstance(before_seq, bool) or before_seq < 0
        ):
            raise ValueError("before_seq must be a non-negative integer or None")
        if not isinstance(max_messages, int) or isinstance(max_messages, bool) or not 1 <= max_messages <= 1000:
            raise ValueError("max_messages must be an integer in [1, 1000]")
        value = self._history_page(session_id, limit=max_messages, before_seq=before_seq)
        entries = value.get("events")
        has_more = value.get("hasMore")
        if not isinstance(entries, list) or not isinstance(has_more, bool):
            raise ExperimentalWebHostError("session.history returned invalid pagination data")
        events: list[dict[str, Any]] = []
        seqs: list[int] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("event"), dict):
                continue
            event = dict(entry["event"])
            events.append(event)
            seq = event.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
                seqs.append(seq)
        return {
            "session_id": session_id,
            "events": events,
            "has_more": has_more,
            "next_before_seq": min(seqs) if has_more and seqs else None,
        }

    def messages(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if before_seq is not None and (
            not isinstance(before_seq, int) or isinstance(before_seq, bool) or before_seq < 0
        ):
            raise ValueError("before_seq must be a non-negative integer or None")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer in [1, 100]")
        messages: list[dict[str, Any]] = []
        cursor = before_seq
        page_has_more = True
        consumed_cursor: int | None = None

        while len(messages) < limit and page_has_more:
            remaining = limit - len(messages)
            value = self._history_page(session_id, limit=remaining, before_seq=cursor)
            entries = value.get("events")
            page_has_more = value.get("hasMore")
            if not isinstance(entries, list) or not isinstance(page_has_more, bool):
                raise ExperimentalWebHostError("session.history returned invalid transcript pagination data")

            seqs: list[int] = []
            page_messages: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("event"), dict):
                    continue
                event = entry["event"]
                seq = event.get("seq")
                if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
                    seqs.append(seq)
                projected = self._project_transcript_message(event)
                if projected is not None:
                    page_messages.append(projected)
            messages = page_messages + messages

            if page_has_more:
                if not seqs:
                    raise ExperimentalWebHostError("session.history pagination made no cursor progress")
                next_cursor = min(seqs)
                if cursor is not None and next_cursor >= cursor:
                    raise ExperimentalWebHostError("session.history pagination cursor did not decrease")
                cursor = next_cursor
                consumed_cursor = next_cursor
            else:
                consumed_cursor = None

        next_before_seq: int | None = None
        if len(messages) >= limit and page_has_more and consumed_cursor is not None:
            next_before_seq = self._next_transcript_cursor(session_id, before_seq=consumed_cursor)

        return {
            "session_id": session_id,
            "messages": messages,
            "has_more": next_before_seq is not None,
            "next_before_seq": next_before_seq,
        }

    def goal_status(self, session_id: str) -> dict[str, Any]:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        projection = self._goal_projection(session_id)
        return {
            "session_id": session_id,
            "goal": projection,
            "activation": "not-exposed-by-durable-projection" if projection is not None else None,
        }

    def goal_create(
        self,
        session_id: str,
        objective: str,
        *,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        objective = objective.strip()
        if not objective:
            raise ValueError("objective must be non-empty")
        if max_goal_rounds is not None and (
            not isinstance(max_goal_rounds, int)
            or isinstance(max_goal_rounds, bool)
            or max_goal_rounds <= 0
        ):
            raise ValueError("max_goal_rounds must be a positive integer")
        self._ensure_attached(session_id)
        payload: dict[str, Any] = {"sessionId": session_id, "objective": objective}
        if max_goal_rounds is not None:
            payload["maxGoalRounds"] = max_goal_rounds
        value = self._call("goal.create", payload)
        ref = value.get("ref")
        if not isinstance(ref, dict):
            raise ExperimentalWebHostError("goal.create returned invalid ref")
        return {
            "session_id": session_id,
            "action": "created",
            "ref": ref,
        }

    def goal_edit(
        self,
        session_id: str,
        *,
        objective: str | None = None,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        if objective is not None:
            objective = objective.strip()
            if not objective:
                raise ValueError("objective must be non-empty when supplied")
        if max_goal_rounds is not None and (
            not isinstance(max_goal_rounds, int)
            or isinstance(max_goal_rounds, bool)
            or max_goal_rounds <= 0
        ):
            raise ValueError("max_goal_rounds must be a positive integer")
        if objective is None and max_goal_rounds is None:
            raise ValueError("goal edit requires objective and/or max_goal_rounds")
        self._ensure_attached(session_id)
        projection = self._goal_projection(session_id)
        if projection is None:
            raise ExperimentalWebHostError(f"session {session_id!r} has no current goal")
        ref = self._goal_ref(projection)
        payload: dict[str, Any] = {"sessionId": session_id, "ref": ref}
        if objective is not None:
            payload["objective"] = objective
        if max_goal_rounds is not None:
            payload["maxGoalRounds"] = max_goal_rounds
        value = self._call("goal.edit", payload)
        new_ref = value.get("ref")
        if not isinstance(new_ref, dict):
            raise ExperimentalWebHostError("goal.edit returned invalid ref")
        return {
            "session_id": session_id,
            "action": "edited",
            "previous_ref": ref,
            "ref": new_ref,
        }

    def goal_resume(self, session_id: str) -> dict[str, Any]:
        self._ensure_attached(session_id)
        projection = self._goal_projection(session_id)
        if projection is None:
            raise ExperimentalWebHostError(f"session {session_id!r} has no current goal")
        ref = self._goal_ref(projection)
        value = self._call("goal.resume", {"sessionId": session_id, "ref": ref})
        return {
            "session_id": session_id,
            "action": "resumed",
            "previous_ref": ref,
            "ref": value.get("ref"),
        }

    def goal_pause(self, session_id: str) -> dict[str, Any]:
        self._ensure_attached(session_id)
        projection = self._goal_projection(session_id)
        if projection is None:
            raise ExperimentalWebHostError(f"session {session_id!r} has no current goal")
        ref = self._goal_ref(projection)
        value = self._call("goal.pause", {"sessionId": session_id, "ref": ref})
        return {
            "session_id": session_id,
            "action": "paused",
            "previous_ref": ref,
            "ref": value.get("ref"),
        }

    def goal_complete(self, session_id: str) -> dict[str, Any]:
        self._ensure_attached(session_id)
        projection = self._goal_projection(session_id)
        if projection is None:
            raise ExperimentalWebHostError(f"session {session_id!r} has no current goal")
        ref = self._goal_ref(projection)
        value = self._call("goal.complete", {"sessionId": session_id, "ref": ref})
        new_ref = value.get("ref")
        if not isinstance(new_ref, dict):
            raise ExperimentalWebHostError("goal.complete returned invalid ref")
        return {
            "session_id": session_id,
            "action": "completed",
            "previous_ref": ref,
            "ref": new_ref,
        }

    def goal_clear(self, session_id: str) -> dict[str, Any]:
        self._ensure_attached(session_id)
        projection = self._goal_projection(session_id)
        if projection is None:
            raise ExperimentalWebHostError(f"session {session_id!r} has no current goal")
        ref = self._goal_ref(projection)
        value = self._call("goal.clear", {"sessionId": session_id, "ref": ref})
        if value.get("cleared") is not True:
            raise ExperimentalWebHostError("goal.clear did not acknowledge the clear")
        return {
            "session_id": session_id,
            "action": "cleared",
            "previous_ref": ref,
            "cleared": True,
        }

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
            result.append(
                {
                    "session_id": session_id,
                    "state": "live" if running else "persisted",
                    "status": "running" if running else "not-running",
                    "attachment_state": "running" if running else "ambiguous-idle-or-cold",
                    "write_attach_probe_required": not running,
                    "updated_at": item.get("updatedAt"),
                    "cwd": item.get("cwd"),
                    "blank": item.get("blank"),
                    "agent_preset": item.get("agentPreset"),
                }
            )
        return result

    def search_sessions(self, query: str) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query must be non-empty")
        if len(query) > 500:
            raise ValueError("query must contain at most 500 characters")
        if "\0" in query:
            raise ValueError("query must not contain NUL")
        try:
            value = self._call("session.search", {"query": query})
        except ExperimentalWebHostError as exc:
            detail = str(exc)
            if "session search is disabled:" in detail or "session search is unavailable:" in detail:
                raise SessionSearchUnavailable(
                    "DSH full-text session search is not enabled for this deployment"
                ) from exc
            raise
        items = value.get("items")
        has_more = value.get("hasMore")
        if not isinstance(items, list) or not isinstance(has_more, bool):
            raise ExperimentalWebHostError("session.search returned invalid results")
        results: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            session_id = item.get("sessionId")
            snippet = item.get("snippet")
            if isinstance(session_id, str) and session_id and isinstance(snippet, str):
                results.append({"session_id": session_id, "snippet": snippet})
        return {"query": query, "items": results, "has_more": has_more}

    def cancel(self, session_id: str) -> dict[str, Any]:
        try:
            value = self._call("session.cancel", {"sessionId": session_id})
        except ExperimentalWebHostError as exc:
            return {"session_id": session_id, "canceled": False, "reason": str(exc)}
        return {
            "session_id": session_id,
            "canceled": value.get("accepted") is True,
        }

    def _history_page(
        self,
        session_id: str,
        *,
        limit: int = 100,
        before_seq: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"sessionId": session_id, "maxMessages": limit}
        if before_seq is not None:
            payload["beforeSeq"] = before_seq
        return self._call("session.history", payload)

    def _goal_projection(self, session_id: str) -> dict[str, Any] | None:
        value = self._history_page(session_id, limit=1)
        projections = value.get("projections")
        if not isinstance(projections, dict):
            return None
        values = projections.get("values")
        if not isinstance(values, dict):
            return None
        goal = values.get("goal")
        if goal is None:
            return None
        if not isinstance(goal, dict):
            raise ExperimentalWebHostError("session.history returned invalid goal projection")
        return dict(goal)

    @staticmethod
    def _goal_ref(projection: dict[str, Any]) -> dict[str, Any]:
        goal = projection.get("goal")
        if not isinstance(goal, dict):
            raise ExperimentalWebHostError("goal projection has no goal snapshot")
        goal_id = goal.get("id")
        revision = goal.get("revision")
        if not isinstance(goal_id, str) or not goal_id or not isinstance(revision, int) or isinstance(revision, bool):
            raise ExperimentalWebHostError("goal projection has invalid CAS ref")
        return {"id": goal_id, "revision": revision}

    def _next_transcript_cursor(self, session_id: str, *, before_seq: int) -> int | None:
        probe_cursor = before_seq
        while True:
            value = self._history_page(session_id, limit=16, before_seq=probe_cursor)
            entries = value.get("events")
            has_more = value.get("hasMore")
            if not isinstance(entries, list) or not isinstance(has_more, bool):
                raise ExperimentalWebHostError("session.history returned invalid transcript lookahead data")

            seqs: list[int] = []
            eligible_seqs: list[int] = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("event"), dict):
                    continue
                event = entry["event"]
                seq = event.get("seq")
                if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
                    seqs.append(seq)
                    if self._project_transcript_message(event) is not None:
                        eligible_seqs.append(seq)
            if eligible_seqs:
                # `beforeSeq` is exclusive. Jump just above the newest eligible
                # event in the probe window, skipping only filtered messages.
                return max(eligible_seqs) + 1
            if not has_more:
                return None
            if not seqs:
                raise ExperimentalWebHostError("session.history transcript lookahead made no cursor progress")
            next_cursor = min(seqs)
            if next_cursor >= probe_cursor:
                raise ExperimentalWebHostError("session.history transcript lookahead cursor did not decrease")
            probe_cursor = next_cursor

    @staticmethod
    def _project_transcript_message(event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("surfaceOp") != "append":
            return None
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            return None

        if event_type == "user/message":
            message = data
            expected_source_kind = "user"
            role = "user"
        elif event_type == "assistant/message":
            message = data.get("message")
            expected_source_kind = "model"
            role = "assistant"
            if not isinstance(message, dict):
                return None
        else:
            return None

        source = message.get("source")
        if not isinstance(source, dict) or source.get("kind") != expected_source_kind:
            return None
        content = message.get("content")
        if not isinstance(content, list):
            return None

        text_parts: list[str] = []
        omitted_types: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                omitted_types.append("unknown")
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif isinstance(block_type, str):
                omitted_types.append(block_type)
            else:
                omitted_types.append("unknown")

        return {
            "seq": event.get("seq"),
            "time": event.get("time"),
            "role": role,
            "message_id": message.get("id"),
            "source_kind": expected_source_kind,
            "text": "\n".join(text_parts),
            "omitted_block_types": list(dict.fromkeys(omitted_types)),
        }

    def _ensure_attached(self, session_id: str) -> None:
        presence = self.presence(session_id)
        if presence is SessionPresence.ABSENT:
            raise KeyError(session_id)
        if presence is SessionPresence.PERSISTED:
            self.resume(session_id)

    def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        _rpc_id, value = self._call_with_id(method, payload, timeout_s=timeout_s)
        return value

    def _call_with_id(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("timeout_s must be positive when supplied")
        effective_timeout = self.timeout_s if timeout_s is None else timeout_s
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
            with urlopen(request, timeout=effective_timeout) as response:
                raw = response.read(MAX_WEB_HOST_RESPONSE_BYTES + 1)
                if len(raw) > MAX_WEB_HOST_RESPONSE_BYTES:
                    raise ExperimentalWebHostError(
                        f"{method} response exceeds {MAX_WEB_HOST_RESPONSE_BYTES} bytes"
                    )
        except HTTPError as exc:
            detail = _http_error_detail(exc, limit=500)
            raise ExperimentalWebHostError(
                f"{method} transport failed with HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise ExperimentalWebHostError(f"{method} transport failed: {exc.reason}") from exc
        except (HTTPException, OSError) as exc:
            raise ExperimentalWebHostError(f"{method} transport failed: {exc}") from exc
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
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
        event_buffer_size: int = 2000,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.backend = PublicSdkBackend(client, catalog, event_buffer_size=event_buffer_size)
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

    def __init__(
        self,
        client: PublicSdkClient,
        catalog: SessionCatalog,
        *,
        event_buffer_size: int = 2000,
    ) -> None:
        if not isinstance(event_buffer_size, int) or isinstance(event_buffer_size, bool) or event_buffer_size <= 0:
            raise ValueError("event_buffer_size must be a positive integer")
        self._client = client
        self._catalog = catalog
        self._event_buffer_size = event_buffer_size
        self._lock = threading.RLock()
        self._allocated: set[str] = set()
        self._live: set[str] = set()
        self._prompting: dict[str, int] = {}
        self._statuses: dict[str, str] = {}
        self._events: dict[str, deque[dict[str, Any]]] = {}
        self._event_totals: dict[str, int] = {}

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
            try:
                self._catalog.add(session_id)
            except (OSError, UnicodeError, ValueError):
                self._allocated.discard(session_id)
                raise
        return SessionHandle(session_id)

    def prompt(self, session_id: str, text: str) -> str:
        with self._lock:
            if session_id not in self._allocated and session_id not in self._live:
                raise KeyError(session_id)
            newly_allocated = session_id in self._allocated
            self._prompting[session_id] = self._prompting.get(session_id, 0) + 1
        try:
            message_id = self._client.session_prompt(
                session_id,
                [{"type": "text", "text": text}],
            )
        except Exception as exc:
            with self._lock:
                remaining = self._prompting[session_id] - 1
                if remaining:
                    self._prompting[session_id] = remaining
                else:
                    self._prompting.pop(session_id, None)
                if (
                    newly_allocated
                    and remaining == 0
                    and session_id in self._allocated
                    and session_id not in self._live
                ):
                    try:
                        self._catalog.remove(session_id)
                    except (OSError, UnicodeError, ValueError) as rollback_exc:
                        exc.add_note(f"session catalog rollback failed: {rollback_exc}")
                    else:
                        self._allocated.discard(session_id)
            raise
        with self._lock:
            remaining = self._prompting[session_id] - 1
            if remaining:
                self._prompting[session_id] = remaining
            else:
                self._prompting.pop(session_id, None)
            self._allocated.discard(session_id)
            self._live.add(session_id)
        return message_id

    def observe_notification(self, method: str, payload: dict[str, Any]) -> None:
        if method == "session.status":
            status = payload.get("status")
            if not isinstance(status, str) or not status:
                return
        elif method == "session.event":
            event = payload.get("event")
            if not isinstance(event, dict):
                return
            event_type = event.get("type")
            event_seq = event.get("seq")
            if not isinstance(event_type, str) or not event_type:
                return
            if not isinstance(event_seq, int) or isinstance(event_seq, bool) or event_seq < 0:
                return
        else:
            return
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return
        with self._lock:
            self._catalog.add(session_id)
            self._allocated.discard(session_id)
            self._live.add(session_id)
            if method == "session.status":
                self._statuses[session_id] = status
            else:
                events = self._events.get(session_id)
                if events is None:
                    events = deque(maxlen=self._event_buffer_size)
                    self._events[session_id] = events
                events.append(dict(event))
                self._event_totals[session_id] = self._event_totals.get(session_id, 0) + 1

    def status(self, session_id: str) -> dict[str, Any]:
        presence = self.presence(session_id)
        with self._lock:
            if presence is SessionPresence.LIVE:
                state = "allocated" if session_id in self._allocated else "live"
                retained = len(self._events.get(session_id, ()))
                total = self._event_totals.get(session_id, 0)
                return {
                    "session_id": session_id,
                    "state": state,
                    "status": self._statuses.get(session_id, "unknown"),
                    "event_count": total,
                    "retained_event_count": retained,
                    "history_truncated": total > retained,
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
            events = list(self._events.get(session_id, ()))
            return [dict(event) for event in events[-limit:]]

    def history_page(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        max_messages: int = 50,
    ) -> dict[str, Any]:
        if before_seq is not None and (
            not isinstance(before_seq, int) or isinstance(before_seq, bool) or before_seq < 0
        ):
            raise ValueError("before_seq must be a non-negative integer or None")
        if not isinstance(max_messages, int) or isinstance(max_messages, bool) or not 1 <= max_messages <= 1000:
            raise ValueError("max_messages must be an integer in [1, 1000]")
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        raise HistoryPaginationUnavailable(
            "the public DSH SDK bridge only observes live notifications and cannot provide authoritative durable pages"
        )

    def messages(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if before_seq is not None and (
            not isinstance(before_seq, int) or isinstance(before_seq, bool) or before_seq < 0
        ):
            raise ValueError("before_seq must be a non-negative integer or None")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer in [1, 100]")
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        raise MessageHistoryUnavailable(
            "the public DSH SDK bridge cannot provide an authoritative durable transcript"
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self.status(session_id) for session_id in self._catalog.ids()]

    def search_sessions(self, query: str) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query must be non-empty")
        if len(query) > 500:
            raise ValueError("query must contain at most 500 characters")
        if "\0" in query:
            raise ValueError("query must not contain NUL")
        raise SessionSearchUnavailable(
            "the public DSH SDK bridge cannot search authoritative durable session messages"
        )

    def cancel(self, session_id: str) -> dict[str, Any]:
        presence = self.presence(session_id)
        return {
            "session_id": session_id,
            "canceled": False,
            "reason": "unsupported-by-public-sdk" if presence is SessionPresence.LIVE else "not-live",
        }

    def goal_status(self, session_id: str) -> dict[str, Any]:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        raise GoalControlUnavailable("the current public DSH SDK does not expose durable goal projection reads")

    def goal_create(
        self,
        session_id: str,
        objective: str,
        *,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        raise GoalControlUnavailable("the current public DSH SDK does not expose goal.create")

    def goal_edit(
        self,
        session_id: str,
        *,
        objective: str | None = None,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        raise GoalControlUnavailable("the current public DSH SDK does not expose goal.edit")

    def goal_resume(self, session_id: str) -> dict[str, Any]:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        raise GoalControlUnavailable("the current public DSH SDK does not expose goal.resume")

    def goal_pause(self, session_id: str) -> dict[str, Any]:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        raise GoalControlUnavailable("the current public DSH SDK does not expose goal.pause")

    def goal_complete(self, session_id: str) -> dict[str, Any]:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        raise GoalControlUnavailable("the current public DSH SDK does not expose goal.complete")

    def goal_clear(self, session_id: str) -> dict[str, Any]:
        if self.presence(session_id) is SessionPresence.ABSENT:
            raise KeyError(session_id)
        raise GoalControlUnavailable("the current public DSH SDK does not expose goal.clear")
