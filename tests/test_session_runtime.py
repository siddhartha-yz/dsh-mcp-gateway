from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dsh_mcp_gateway import build_mcp_server
from dsh_mcp_gateway.session_runtime import DurableSessionRuntime, SessionRuntimeError


class DurableSessionRuntimeTests(unittest.TestCase):
    def make_runtime(self, root: str) -> DurableSessionRuntime:
        return DurableSessionRuntime(Path(root) / "sessions.sqlite3")

    def test_start_report_restart_resume_preserves_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = self.make_runtime(tmp)
            started = first.manage(action="start", label="repo work", objective="finish runtime")
            session_id = started["session_id"]
            run_id = started["active_run"]["run_id"]

            reported = first.manage(
                action="report",
                session_id=session_id,
                run_id=run_id,
                summary="OAuth is already working",
                findings=["DSH session core can run without a model provider"],
                next="add ChatGPT continuation",
                blockers=[],
            )
            self.assertEqual(reported["progress"]["summary"], "OAuth is already working")

            restarted = self.make_runtime(tmp)
            recovered = restarted.manage(action="get", session_id=session_id)
            self.assertEqual(recovered["progress"]["next"], "add ChatGPT continuation")
            self.assertEqual(recovered["active_run"]["run_id"], run_id)

            resumed = restarted.manage(action="resume", session_id=session_id, takeover=True)
            new_run_id = resumed["active_run"]["run_id"]
            self.assertNotEqual(new_run_id, run_id)
            statuses = {run["run_id"]: run["status"] for run in resumed["runs"]}
            self.assertEqual(statuses[run_id], "superseded")
            self.assertEqual(statuses[new_run_id], "active")

    def test_get_fetches_only_bounded_recent_run_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp)
            state = runtime.manage(action="start", objective="bounded history")
            session_id = state["session_id"]
            first_run_id = state["active_run"]["run_id"]
            for _ in range(29):
                state = runtime.manage(action="resume", session_id=session_id, takeover=True)

            with runtime._connection() as connection:
                fetched = runtime._run_rows(connection, session_id)

            self.assertEqual(len(fetched), 20)
            self.assertNotIn(first_run_id, {row["run_id"] for row in fetched})
            public = runtime.manage(action="get", session_id=session_id)
            self.assertEqual([row["run_id"] for row in fetched], [run["run_id"] for run in public["runs"]])
            self.assertEqual(
                public["active_run"]["run_id"],
                state["active_run"]["run_id"],
            )

    def test_resume_requires_explicit_takeover_of_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp)
            started = runtime.manage(action="start", objective="test")
            with self.assertRaisesRegex(SessionRuntimeError, "takeover=true"):
                runtime.manage(action="resume", session_id=started["session_id"])

    def test_superseded_run_cannot_mutate_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp)
            started = runtime.manage(action="start", objective="test")
            session_id = started["session_id"]
            old_run_id = started["active_run"]["run_id"]
            resumed = runtime.manage(action="resume", session_id=session_id, takeover=True)
            new_run_id = resumed["active_run"]["run_id"]

            with self.assertRaisesRegex(SessionRuntimeError, "no longer owns"):
                runtime.manage(
                    action="report",
                    session_id=session_id,
                    run_id=old_run_id,
                    summary="stale write",
                )

            current = runtime.manage(
                action="report",
                session_id=session_id,
                run_id=new_run_id,
                summary="fresh write",
            )
            self.assertEqual(current["progress"]["summary"], "fresh write")

    def test_finish_and_cancel_are_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp)
            for action in ("finish", "cancel"):
                started = runtime.manage(action="start", objective=action)
                session_id = started["session_id"]
                run_id = started["active_run"]["run_id"]
                terminal = runtime.manage(action=action, session_id=session_id, run_id=run_id)
                expected = "completed" if action == "finish" else "cancelled"
                self.assertEqual(terminal["status"], expected)
                self.assertIsNone(terminal["active_run"])
                with self.assertRaisesRegex(SessionRuntimeError, f"Cannot resume a {expected} session"):
                    runtime.manage(action="resume", session_id=session_id, takeover=True)

    def test_list_is_newest_first_and_compact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp)
            first = runtime.manage(action="start", label="first", objective="a" * 1000)
            first_run = first["active_run"]["run_id"]
            runtime.manage(
                action="report",
                session_id=first["session_id"],
                run_id=first_run,
                summary="s" * 1000,
                findings=["one", "two"],
                blockers=["blocked"],
            )
            second = runtime.manage(action="start", label="second", objective="newest")

            listed = runtime.manage(action="list")["sessions"]
            self.assertEqual(listed[0]["session_id"], second["session_id"])
            first_listed = next(item for item in listed if item["session_id"] == first["session_id"])
            self.assertEqual(len(first_listed["objective"]), 500)
            self.assertEqual(len(first_listed["progress"]["summary"]), 500)
            self.assertEqual(first_listed["progress"]["finding_count"], 2)
            self.assertEqual(first_listed["progress"]["blocker_count"], 1)

    def test_mutation_requires_current_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp)
            started = runtime.manage(action="start")
            with self.assertRaisesRegex(SessionRuntimeError, "run_id is required"):
                runtime.manage(action="report", session_id=started["session_id"], summary="missing lease")

    def test_manage_closes_sqlite_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp)
            real_connect = sqlite3.connect
            closed: list[bool] = []

            class TrackingConnection:
                def __init__(self, connection):
                    object.__setattr__(self, "connection", connection)

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                def __setattr__(self, name, value):
                    setattr(self.connection, name, value)

                def __enter__(self):
                    self.connection.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.connection.__exit__(*args)

                def close(self):
                    closed.append(True)
                    self.connection.close()

            def tracking_connect(*args, **kwargs):
                return TrackingConnection(real_connect(*args, **kwargs))

            with patch("dsh_mcp_gateway.session_runtime.sqlite3.connect", side_effect=tracking_connect):
                runtime.manage(action="list")

            self.assertEqual(closed, [True])

    def test_connect_closes_sqlite_connection_when_post_connect_setup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = DurableSessionRuntime.__new__(DurableSessionRuntime)
            runtime.database = Path(tmp) / "sessions.sqlite3"
            real_connect = sqlite3.connect
            closed: list[bool] = []

            class TrackingConnection:
                def __init__(self, connection):
                    object.__setattr__(self, "connection", connection)

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                def __setattr__(self, name, value):
                    setattr(self.connection, name, value)

                def close(self):
                    closed.append(True)
                    self.connection.close()

            def tracking_connect(*args, **kwargs):
                return TrackingConnection(real_connect(*args, **kwargs))

            with (
                patch.object(runtime, "_secure_sidecars", side_effect=[None, OSError("sidecar race")]),
                patch("dsh_mcp_gateway.session_runtime.sqlite3.connect", side_effect=tracking_connect),
                self.assertRaisesRegex(OSError, "sidecar race"),
            ):
                runtime._connect()

            self.assertEqual(closed, [True])

    def test_sqlite_database_is_private_before_connect_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            real_connect = sqlite3.connect
            observed_modes: list[int] = []

            def inspect_connect(database, *args, **kwargs):
                connection = real_connect(database, *args, **kwargs)
                observed_modes.append(stat.S_IMODE(path.stat().st_mode))
                return connection

            previous_umask = os.umask(0o022)
            try:
                with patch("dsh_mcp_gateway.session_runtime.sqlite3.connect", side_effect=inspect_connect):
                    DurableSessionRuntime(path)
            finally:
                os.umask(previous_umask)

            self.assertTrue(observed_modes)
            self.assertEqual(observed_modes[0], 0o600)

    def test_sqlite_database_rejects_symlinked_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.sqlite3"
            target.write_text("not a session database\n", encoding="utf-8")
            target.chmod(0o644)
            path = root / "sessions.sqlite3"
            path.symlink_to(target)

            with self.assertRaises(OSError):
                DurableSessionRuntime(path)

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_sqlite_database_rejects_hard_linked_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.sqlite3"
            target.write_text("sentinel\n", encoding="utf-8")
            target.chmod(0o640)
            path = root / "sessions.sqlite3"
            os.link(target, path)

            with self.assertRaisesRegex(OSError, "unexpected hard links"):
                DurableSessionRuntime(path)

            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_sqlite_database_rejects_non_regular_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            os.mkfifo(path, mode=0o600)

            with self.assertRaisesRegex(OSError, "not a regular file"):
                DurableSessionRuntime(path)

    def test_sqlite_database_rejects_symlinked_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            alias = root / "alias"
            alias.symlink_to(target, target_is_directory=True)

            with self.assertRaises(OSError):
                DurableSessionRuntime(alias / "sessions.sqlite3")

            self.assertEqual(list(target.iterdir()), [])

    def test_sqlite_database_rejects_symlinked_sidecar_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.txt"
            target.write_text("sentinel\n", encoding="utf-8")
            target.chmod(0o644)
            path = root / "sessions.sqlite3"
            Path(f"{path}-wal").symlink_to(target)

            with self.assertRaises(OSError):
                DurableSessionRuntime(path)

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")

    def test_sqlite_database_rejects_hard_linked_sidecar_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.txt"
            target.write_text("sentinel\n", encoding="utf-8")
            target.chmod(0o640)
            path = root / "sessions.sqlite3"
            os.link(target, Path(f"{path}-wal"))

            with self.assertRaisesRegex(OSError, "sidecar path has unexpected hard links"):
                DurableSessionRuntime(path)

            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_sqlite_database_rejects_non_regular_sidecar_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            os.mkfifo(Path(f"{path}-wal"), mode=0o600)

            with self.assertRaisesRegex(OSError, "sidecar path is not a regular file"):
                DurableSessionRuntime(path)

    def test_sqlite_parent_creation_tolerates_concurrent_creator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state" / "sessions.sqlite3"
            real_mkdir = os.mkdir
            raced = False

            def raced_mkdir(*args, **kwargs):
                nonlocal raced
                real_mkdir(*args, **kwargs)
                if not raced:
                    raced = True
                    raise FileExistsError("directory was concurrently created")

            with patch("dsh_mcp_gateway.session_runtime.os.mkdir", side_effect=raced_mkdir):
                runtime = DurableSessionRuntime(path)

            self.assertTrue(raced)
            self.assertEqual(runtime.database, path)
            self.assertTrue(path.exists())

    def test_sqlite_database_cannot_be_swapped_to_symlink_before_connect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sessions.sqlite3"
            target = root / "target.sqlite3"
            with sqlite3.connect(target):
                pass
            real_connect = sqlite3.connect

            def swap_before_connect(database, *args, **kwargs):
                if path.exists() or path.is_symlink():
                    path.unlink()
                path.symlink_to(target)
                return real_connect(database, *args, **kwargs)

            with (
                patch("dsh_mcp_gateway.session_runtime.sqlite3.connect", side_effect=swap_before_connect),
                self.assertRaises(sqlite3.OperationalError),
            ):
                DurableSessionRuntime(path)

            with real_connect(target) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertNotIn("logical_sessions", tables)

    def test_sqlite_state_files_are_private_under_permissive_umask(self) -> None:
        previous_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                runtime = self.make_runtime(tmp)
                started = runtime.manage(action="start", objective="private task text")
                runtime.manage(
                    action="report",
                    session_id=started["session_id"],
                    run_id=started["active_run"]["run_id"],
                    summary="private checkpoint",
                )

                state_files = sorted(Path(tmp).glob("sessions.sqlite3*"))
                self.assertGreaterEqual(len(state_files), 1)
                self.assertEqual(
                    {path.name: stat.S_IMODE(path.stat().st_mode) for path in state_files},
                    {path.name: 0o600 for path in state_files},
                )
        finally:
            os.umask(previous_umask)


class SessionManageMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_manage_is_exposed_without_model_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = DurableSessionRuntime(Path(tmp) / "sessions.sqlite3")
            server = build_mcp_server(None, session_runtime=runtime)

            started = await server.call_tool(
                "session_manage",
                {"action": "start", "objective": "continue ChatGPT work"},
            )
            payload = started.structured_content
            session_id = payload["session_id"]
            run_id = payload["active_run"]["run_id"]

            reported = await server.call_tool(
                "session_manage",
                {
                    "action": "report",
                    "session_id": session_id,
                    "session_run_id": run_id,
                    "summary": "checkpoint",
                },
            )
            self.assertEqual(reported.structured_content["progress"]["summary"], "checkpoint")

    async def test_runtime_only_server_hides_legacy_dsh_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = DurableSessionRuntime(Path(tmp) / "sessions.sqlite3")
            server = build_mcp_server(None, session_runtime=runtime)
            names = {tool.name for tool in await server.list_tools()}
            self.assertEqual(names, {"session_manage"})


if __name__ == "__main__":
    unittest.main()
