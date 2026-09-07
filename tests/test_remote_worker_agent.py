from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from dsh_mcp_gateway.remote_worker_agent import (
    WorkerError,
    atomic_json,
    edit_text,
    glob_files,
    identity_path,
    load_identity,
    normalize_server,
    read_text,
    run_shell,
    write_text,
)
from dsh_mcp_gateway.remote_worker_edge import build_join_script


class RemoteWorkerAgentTests(unittest.TestCase):
    def test_server_must_be_exact_https_origin(self) -> None:
        self.assertEqual(normalize_server("https://dsh.example.com/"), "https://dsh.example.com")
        for value in (
            "http://dsh.example.com",
            "https://user@dsh.example.com",
            "https://dsh.example.com/path",
            "https://dsh.example.com/?query=1",
            "not-a-url",
        ):
            with self.subTest(value=value), self.assertRaises(WorkerError):
                normalize_server(value)

    def test_identity_is_private_and_rejects_permissive_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": directory}, clear=False
        ):
            path = identity_path()
            value = {
                "server": "https://dsh.example.com",
                "name": "desktop",
                "token": "secret-token",
                "workdir": directory,
            }
            atomic_json(path, value)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_identity()["name"], "desktop")
            path.chmod(0o644)
            with self.assertRaisesRegex(WorkerError, "permissions must be 0600"):
                load_identity()

    def test_shell_captures_exit_output_and_bounds_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_shell(
                {
                    "command": "printf 'out'; printf 'err' >&2; exit 7",
                    "max_output_chars": 16,
                },
                directory,
                threading.Event(),
                time.time() + 5,
            )
            self.assertEqual(result["exit_code"], 7)
            self.assertEqual(result["stdout"], "out")
            self.assertEqual(result["stderr"], "err")
            self.assertFalse(result["truncated"])
            self.assertEqual(result["cwd"], str(Path(directory).resolve()))

            bounded = run_shell(
                {"command": "python3 -c \"print('x' * 200)\"", "max_output_chars": 32},
                directory,
                threading.Event(),
                time.time() + 5,
            )
            self.assertLessEqual(len(bounded["stdout"]) + len(bounded["stderr"]), 32)
            self.assertTrue(bounded["truncated"])

    def test_shell_honors_job_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            with self.assertRaisesRegex(WorkerError, "expired|timeout"):
                run_shell(
                    {"command": "sleep 30"},
                    directory,
                    threading.Event(),
                    time.time() + 0.2,
                )
            self.assertLess(time.monotonic() - started, 4.0)

    def test_file_operations_preserve_existing_mode_and_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sample.txt"
            path.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
            path.chmod(0o640)
            event = threading.Event()
            deadline = time.time() + 5

            read = read_text(
                {"file_path": "sample.txt", "offset": 2, "limit": 1},
                directory,
                event,
                deadline,
            )
            self.assertEqual(read["content"], "beta\n")

            with self.assertRaisesRegex(WorkerError, "occurs 2 times"):
                edit_text(
                    {
                        "file_path": "sample.txt",
                        "old_string": "alpha",
                        "new_string": "gamma",
                    },
                    directory,
                    event,
                    deadline,
                )

            edited = edit_text(
                {
                    "file_path": "sample.txt",
                    "old_string": "alpha",
                    "new_string": "gamma",
                    "replace_all": True,
                },
                directory,
                event,
                deadline,
            )
            self.assertEqual(edited["replacements"], 2)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(path.read_text(encoding="utf-8"), "gamma\nbeta\ngamma\n")

            written = write_text(
                {"file_path": "new.txt", "content": "created"},
                directory,
                event,
                deadline,
            )
            self.assertEqual(written["chars_written"], 7)
            self.assertEqual((root / "new.txt").read_text(encoding="utf-8"), "created")

    def test_glob_is_rooted_at_worker_workdir_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "b.py").write_text("", encoding="utf-8")
            result = glob_files(
                {"pattern": "**/*.py"},
                directory,
                threading.Event(),
                time.time() + 5,
            )
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["matches"], sorted(result["matches"]))

    def test_join_script_pins_worker_bytes_and_has_foreground_or_persistent_mode(self) -> None:
        source = b"print('worker')\n"
        script = build_join_script("https://dsh.example.com", source)
        self.assertIn("https://dsh.example.com", script)
        self.assertIn("sha256sum -c -", script)
        self.assertIn("--invite is required", script)
        self.assertIn("install-service --start", script)
        self.assertIn("exec python3 \"$worker\" run", script)
        self.assertNotIn(source.decode(), script)


if __name__ == "__main__":
    unittest.main()
