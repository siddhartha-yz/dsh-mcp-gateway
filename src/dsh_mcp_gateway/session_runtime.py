"""Durable ChatGPT-run session state independent of any model provider.

This control-plane runtime deliberately does not execute prompts. ChatGPT remains
the only reasoning agent; the runtime only persists task state and arbitrates
which ChatGPT run currently owns mutation authority for a logical session.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SESSION_TEXT_LIMIT = 20_000
SESSION_LIST_TEXT_LIMIT = 500
SESSION_REPORT_LIST_LIMIT = 50
SESSION_LIST_LIMIT = 100


class SessionRuntimeError(ValueError):
    """Raised when a logical-session lifecycle operation is invalid."""


class DurableSessionRuntime:
    """SQLite-backed logical task sessions with explicit ChatGPT run takeover."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _secure_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            path = Path(f"{self.database}{suffix}")
            try:
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        self._secure_sidecars()
        fd = os.open(self.database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            connection = sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=rw", uri=True, timeout=10.0)
        finally:
            os.close(fd)
        self._secure_sidecars()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS logical_sessions (
                    session_id TEXT PRIMARY KEY,
                    label TEXT,
                    objective TEXT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    active_run_id TEXT,
                    summary TEXT,
                    findings_json TEXT NOT NULL DEFAULT '[]',
                    next_text TEXT,
                    blockers_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES logical_sessions(session_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS agent_runs_session_created
                    ON agent_runs(session_id, created_at);
                CREATE INDEX IF NOT EXISTS logical_sessions_updated
                    ON logical_sessions(updated_at DESC);
                """
            )

    @staticmethod
    def _new_session_id() -> str:
        return f"s_{secrets.token_hex(12)}"

    @staticmethod
    def _new_run_id() -> str:
        return f"r_{secrets.token_hex(5)}"

    @staticmethod
    def _bounded_text(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        return normalized[:SESSION_TEXT_LIMIT]

    @classmethod
    def _bounded_list(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [item for item in (cls._bounded_text(str(value)) for value in values) if item]
        return normalized[:SESSION_REPORT_LIST_LIMIT]

    @staticmethod
    def _decode_list(value: str) -> list[str]:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
        return [str(item) for item in decoded] if isinstance(decoded, list) else []

    @staticmethod
    def _encode_list(value: list[str]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _session_row(self, connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM logical_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionRuntimeError(f"Unknown logical session: {session_id}")
        return row

    def _run_rows(self, connection: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                "SELECT run_id, status, created_at, updated_at FROM agent_runs "
                "WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        )

    def _public_state(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        runs = self._run_rows(connection, str(row["session_id"]))
        active_run_id = row["active_run_id"]
        active_run = next((run for run in runs if run["run_id"] == active_run_id), None)
        return {
            "session_id": row["session_id"],
            "label": row["label"],
            "objective": row["objective"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "active_run": (
                {
                    "run_id": active_run["run_id"],
                    "status": active_run["status"],
                    "created_at": active_run["created_at"],
                    "updated_at": active_run["updated_at"],
                }
                if active_run is not None
                else None
            ),
            "runs": [
                {
                    "run_id": run["run_id"],
                    "status": run["status"],
                    "created_at": run["created_at"],
                    "updated_at": run["updated_at"],
                }
                for run in runs[-20:]
            ],
            "progress": {
                "summary": row["summary"],
                "findings": self._decode_list(row["findings_json"]),
                "next": row["next_text"],
                "blockers": self._decode_list(row["blockers_json"]),
            },
        }

    def _begin(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def _create_run(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        takeover: bool,
    ) -> str:
        row = self._session_row(connection, session_id)
        current_id = row["active_run_id"]
        if current_id:
            current = connection.execute(
                "SELECT status FROM agent_runs WHERE run_id = ? AND session_id = ?",
                (current_id, session_id),
            ).fetchone()
            if current is not None and current["status"] == "active":
                if not takeover:
                    raise SessionRuntimeError(
                        "Session already has an active ChatGPT run; resume with takeover=true to supersede it"
                    )
                now = time.time()
                connection.execute(
                    "UPDATE agent_runs SET status = 'superseded', updated_at = ? WHERE run_id = ?",
                    (now, current_id),
                )
        now = time.time()
        run_id = self._new_run_id()
        connection.execute(
            "INSERT INTO agent_runs(run_id, session_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (run_id, session_id, now, now),
        )
        connection.execute(
            "UPDATE logical_sessions SET active_run_id = ?, updated_at = ? WHERE session_id = ?",
            (run_id, now, session_id),
        )
        return run_id

    def _assert_active_run(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        run_id: str | None,
    ) -> sqlite3.Row:
        if not run_id:
            raise SessionRuntimeError("run_id is required for session mutation")
        session = self._session_row(connection, session_id)
        if session["active_run_id"] != run_id:
            raise SessionRuntimeError("run_id no longer owns this logical session")
        run = connection.execute(
            "SELECT * FROM agent_runs WHERE run_id = ? AND session_id = ?",
            (run_id, session_id),
        ).fetchone()
        if run is None or run["status"] != "active":
            raise SessionRuntimeError("run_id is not active")
        return session

    def manage(
        self,
        *,
        action: str,
        session_id: str | None = None,
        run_id: str | None = None,
        label: str | None = None,
        objective: str | None = None,
        summary: str | None = None,
        findings: list[str] | None = None,
        next: str | None = None,
        blockers: list[str] | None = None,
        takeover: bool = False,
    ) -> dict[str, Any]:
        """Manage one durable logical task session.

        `start` and `resume` mint a new run lease. Mutating actions require that
        lease's `run_id`, so a superseded ChatGPT run cannot continue writing.
        """
        normalized = str(action).strip().lower()
        if normalized not in {"start", "list", "get", "resume", "report", "finish", "cancel"}:
            raise SessionRuntimeError(f"Unsupported session action: {action}")

        with self._lock, self._connect() as connection:
            if normalized == "list":
                rows = connection.execute(
                    "SELECT * FROM logical_sessions ORDER BY updated_at DESC LIMIT ?",
                    (SESSION_LIST_LIMIT,),
                ).fetchall()
                return {
                    "sessions": [
                        {
                            "session_id": row["session_id"],
                            "label": row["label"][:SESSION_LIST_TEXT_LIMIT] if row["label"] else None,
                            "objective": (
                                row["objective"][:SESSION_LIST_TEXT_LIMIT] if row["objective"] else None
                            ),
                            "status": row["status"],
                            "updated_at": row["updated_at"],
                            "active_run_id": row["active_run_id"],
                            "progress": {
                                "summary": (
                                    row["summary"][:SESSION_LIST_TEXT_LIMIT] if row["summary"] else None
                                ),
                                "next": (
                                    row["next_text"][:SESSION_LIST_TEXT_LIMIT]
                                    if row["next_text"]
                                    else None
                                ),
                                "finding_count": len(self._decode_list(row["findings_json"])),
                                "blocker_count": len(self._decode_list(row["blockers_json"])),
                            },
                        }
                        for row in rows
                    ]
                }

            if normalized == "start":
                self._begin(connection)
                now = time.time()
                new_session_id = self._new_session_id()
                connection.execute(
                    "INSERT INTO logical_sessions("
                    "session_id, label, objective, status, created_at, updated_at"
                    ") VALUES (?, ?, ?, 'active', ?, ?)",
                    (
                        new_session_id,
                        self._bounded_text(label),
                        self._bounded_text(objective),
                        now,
                        now,
                    ),
                )
                self._create_run(connection, new_session_id, takeover=True)
                row = self._session_row(connection, new_session_id)
                connection.commit()
                return self._public_state(connection, row)

            if not session_id:
                raise SessionRuntimeError("session_id is required for this action")

            if normalized == "get":
                return self._public_state(connection, self._session_row(connection, session_id))

            self._begin(connection)
            row = self._session_row(connection, session_id)

            if normalized == "resume":
                if row["status"] != "active":
                    raise SessionRuntimeError(f"Cannot resume a {row['status']} session")
                self._create_run(connection, session_id, takeover=takeover)
                row = self._session_row(connection, session_id)
                connection.commit()
                return self._public_state(connection, row)

            self._assert_active_run(connection, session_id, run_id)

            if normalized == "report":
                updates: dict[str, Any] = {}
                if label is not None:
                    updates["label"] = self._bounded_text(label)
                if objective is not None:
                    updates["objective"] = self._bounded_text(objective)
                if summary is not None:
                    updates["summary"] = self._bounded_text(summary)
                bounded_findings = self._bounded_list(findings)
                if bounded_findings is not None:
                    updates["findings_json"] = self._encode_list(bounded_findings)
                if next is not None:
                    updates["next_text"] = self._bounded_text(next)
                bounded_blockers = self._bounded_list(blockers)
                if bounded_blockers is not None:
                    updates["blockers_json"] = self._encode_list(bounded_blockers)
                if not updates:
                    raise SessionRuntimeError(
                        "action=report requires label, objective, summary, findings, next, or blockers"
                    )
                now = time.time()
                assignments = ", ".join(f"{column} = ?" for column in updates)
                connection.execute(
                    f"UPDATE logical_sessions SET {assignments}, updated_at = ? WHERE session_id = ?",
                    (*updates.values(), now, session_id),
                )
                connection.execute(
                    "UPDATE agent_runs SET updated_at = ? WHERE run_id = ?",
                    (now, run_id),
                )
            else:
                terminal_status = "completed" if normalized == "finish" else "cancelled"
                now = time.time()
                connection.execute(
                    "UPDATE agent_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    (terminal_status, now, run_id),
                )
                connection.execute(
                    "UPDATE logical_sessions SET status = ?, active_run_id = NULL, updated_at = ? "
                    "WHERE session_id = ?",
                    (terminal_status, now, session_id),
                )

            row = self._session_row(connection, session_id)
            connection.commit()
            return self._public_state(connection, row)
