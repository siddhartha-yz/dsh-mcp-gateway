from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import glob as glob_module
import json
import os
import platform
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
CAPABILITIES = ["shell", "files"]
MAX_RESULT_CHARS = 48_000
MAX_FILE_CONTENT_CHARS = 1_000_000
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_HEARTBEAT_S = 10.0
DEFAULT_POLL_TIMEOUT_S = 25.0


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


class WorkerHTTPError(WorkerError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(code, message)
        self.status = status


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "dsh-remote-worker"


def identity_path() -> Path:
    return state_dir() / "identity.json"


def install_dir() -> Path:
    return Path.home() / ".local" / "lib" / "dsh-remote-worker"


def service_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "dsh-remote-worker.service"


def normalize_server(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise WorkerError("invalid_server", "server must be an absolute HTTPS origin")
    return value.removesuffix("/")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_identity() -> dict[str, Any]:
    path = identity_path()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise WorkerError("unsafe_identity", f"identity file permissions must be 0600, got {mode:04o}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkerError("not_enrolled", f"remote worker is not enrolled; missing {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkerError("invalid_identity", f"identity file is invalid JSON: {path}") from exc
    required = {"server", "name", "token", "workdir"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise WorkerError("invalid_identity", "identity file has an unexpected shape")
    value["server"] = normalize_server(str(value["server"]))
    value["name"] = str(value["name"])
    value["token"] = str(value["token"])
    value["workdir"] = str(value["workdir"])
    return value


def worker_info() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "user": getpass.getuser(),
        "platform": platform.system().lower(),
        "platform_release": platform.release(),
        "python": platform.python_version(),
    }


def _read_limited(response, limit: int = MAX_HTTP_RESPONSE_BYTES) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise WorkerError("response_too_large", f"server response exceeds {limit} bytes")
    return data


def post_json(
    server: str,
    action: str,
    payload: dict[str, Any],
    *,
    token: str | None = None,
    timeout_s: float = 35.0,
) -> Any:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "dsh-remote-worker/1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{server}/remote/v1/{action}", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = _read_limited(response)
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read(128 * 1024)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {}
        raise WorkerHTTPError(
            exc.code,
            str(parsed.get("error") or "http_error"),
            str(parsed.get("message") or f"server returned HTTP {exc.code}"),
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerError("invalid_response", f"server returned invalid JSON (HTTP {status})") from exc
    if not isinstance(parsed, dict) or parsed.get("ok") is not True or "value" not in parsed:
        raise WorkerError("invalid_response", "server returned an unexpected response shape")
    return parsed["value"]


def enroll(server: str, invite: str, name: str | None, workdir: str | None) -> dict[str, Any]:
    server = normalize_server(server)
    cwd = str(Path(workdir or os.getcwd()).expanduser().resolve())
    value = post_json(
        server,
        "register",
        {
            "protocol_version": PROTOCOL_VERSION,
            "invite": invite,
            "name": name,
            "workdir": cwd,
            "capabilities": CAPABILITIES,
            "info": worker_info(),
        },
    )
    identity = {
        "server": server,
        "name": str(value["name"]),
        "token": str(value["token"]),
        "workdir": cwd,
        "poll_timeout_s": float(value.get("poll_timeout_s", DEFAULT_POLL_TIMEOUT_S)),
        "heartbeat_interval_s": float(value.get("heartbeat_interval_s", DEFAULT_HEARTBEAT_S)),
    }
    atomic_json(identity_path(), identity)
    return identity


def resolve_path(raw: str, workdir: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(workdir) / path
    return path.resolve(strict=False)


def ensure_live(cancel_event: threading.Event, deadline: float) -> None:
    if cancel_event.is_set():
        raise WorkerError("cancelled", "remote job was cancelled")
    if time.time() >= deadline:
        raise WorkerError("timeout", "remote job expired before completion")


def parse_deadline(job: dict[str, Any]) -> float:
    raw = job.get("expires_at")
    if not isinstance(raw, str):
        return time.time() + 60.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return time.time() + 60.0


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def run_shell(args: dict[str, Any], workdir: str, cancel_event: threading.Event, deadline: float) -> dict[str, Any]:
    command = str(args.get("command", ""))
    if not command or len(command) > 65_536:
        raise WorkerError("invalid_request", "shell command must contain 1..65536 characters")
    cwd = str(resolve_path(str(args.get("cwd") or workdir), workdir))
    max_chars = int(args.get("max_output_chars", 32_000))
    if max_chars < 1 or max_chars > MAX_RESULT_CHARS:
        raise WorkerError("invalid_request", f"max_output_chars must be within 1..{MAX_RESULT_CHARS}")
    ensure_live(cancel_event, deadline)
    try:
        process = subprocess.Popen(
            ["/bin/bash", "-lc", command],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise WorkerError("spawn_failed", str(exc)) from exc

    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    byte_budget = max_chars * 4
    truncated = False
    try:
        while selector.get_map():
            if cancel_event.is_set() or time.time() >= deadline:
                _terminate_process_group(process)
                ensure_live(cancel_event, deadline)
            events = selector.select(timeout=0.1)
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                used = len(captured["stdout"]) + len(captured["stderr"])
                remaining = max(0, byte_budget - used)
                if remaining:
                    captured[key.data].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
            if process.poll() is not None and not events:
                for key in list(selector.get_map().values()):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if chunk:
                        used = len(captured["stdout"]) + len(captured["stderr"])
                        remaining = max(0, byte_budget - used)
                        if remaining:
                            captured[key.data].extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            truncated = True
                    else:
                        selector.unregister(key.fileobj)
        exit_code = process.wait(timeout=1.0)
    finally:
        selector.close()
        if process.poll() is None:
            _terminate_process_group(process)
        process.stdout.close()
        process.stderr.close()

    stdout = captured["stdout"].decode("utf-8", errors="replace")
    stderr = captured["stderr"].decode("utf-8", errors="replace")
    if len(stdout) + len(stderr) > max_chars:
        remaining = max_chars
        stdout = stdout[:remaining]
        remaining -= len(stdout)
        stderr = stderr[:remaining]
        truncated = True
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "cwd": cwd,
    }


def read_text(args: dict[str, Any], workdir: str, cancel_event: threading.Event, deadline: float) -> dict[str, Any]:
    ensure_live(cancel_event, deadline)
    path = resolve_path(str(args.get("file_path", "")), workdir)
    offset = int(args.get("offset", 1))
    limit = int(args.get("limit", 200))
    if offset < 1 or limit < 1 or limit > 2000:
        raise WorkerError("invalid_request", "read offset/limit are outside supported bounds")
    lines: list[str] = []
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index in range(1, offset + limit):
            ensure_live(cancel_event, deadline)
            line = handle.readline(MAX_RESULT_CHARS + 1)
            if line == "":
                break
            if len(line) > MAX_RESULT_CHARS and not line.endswith("\n"):
                raise WorkerError("resource_limit", "encountered a line larger than the remote result limit")
            if index < offset:
                continue
            if total + len(line) > MAX_RESULT_CHARS:
                lines.append(line[: max(0, MAX_RESULT_CHARS - total)])
                return {
                    "path": str(path),
                    "offset": offset,
                    "content": "".join(lines),
                    "truncated": True,
                }
            lines.append(line)
            total += len(line)
    return {"path": str(path), "offset": offset, "content": "".join(lines), "truncated": False}


def _read_bounded_file(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        content = handle.read(MAX_FILE_CONTENT_CHARS + 1)
    if len(content) > MAX_FILE_CONTENT_CHARS:
        raise WorkerError("resource_limit", f"file exceeds {MAX_FILE_CONTENT_CHARS} characters")
    return content


def write_text(args: dict[str, Any], workdir: str, cancel_event: threading.Event, deadline: float) -> dict[str, Any]:
    ensure_live(cancel_event, deadline)
    path = resolve_path(str(args.get("file_path", "")), workdir)
    content = args.get("content")
    if not isinstance(content, str) or len(content) > MAX_FILE_CONTENT_CHARS:
        raise WorkerError("invalid_request", f"content must be at most {MAX_FILE_CONTENT_CHARS} characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if existing_mode is not None:
            os.fchmod(fd, existing_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_live(cancel_event, deadline)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return {"path": str(path), "chars_written": len(content)}


def edit_text(args: dict[str, Any], workdir: str, cancel_event: threading.Event, deadline: float) -> dict[str, Any]:
    ensure_live(cancel_event, deadline)
    path = resolve_path(str(args.get("file_path", "")), workdir)
    old = args.get("old_string")
    new = args.get("new_string")
    replace_all = bool(args.get("replace_all", False))
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise WorkerError("invalid_request", "edit requires non-empty old_string and string new_string")
    content = _read_bounded_file(path)
    count = content.count(old)
    if count == 0:
        raise WorkerError("not_found", "old_string was not found in file")
    if not replace_all and count != 1:
        raise WorkerError("ambiguous_match", f"old_string occurs {count} times; use replace_all or a more specific match")
    next_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    if len(next_content) > MAX_FILE_CONTENT_CHARS:
        raise WorkerError("resource_limit", "edited file would exceed the remote file-content limit")
    return write_text({"file_path": str(path), "content": next_content}, workdir, cancel_event, deadline) | {
        "replacements": count if replace_all else 1
    }


def glob_files(args: dict[str, Any], workdir: str, cancel_event: threading.Event, deadline: float) -> dict[str, Any]:
    ensure_live(cancel_event, deadline)
    pattern = str(args.get("pattern", ""))
    if not pattern:
        raise WorkerError("invalid_request", "glob pattern is required")
    base = resolve_path(str(args.get("path") or workdir), workdir)
    raw_pattern = pattern if os.path.isabs(pattern) else str(base / pattern)
    results: list[str] = []
    chars = 0
    truncated = False
    for match in glob_module.iglob(raw_pattern, recursive=True):
        ensure_live(cancel_event, deadline)
        value = str(Path(match))
        if chars + len(value) + 1 > MAX_RESULT_CHARS or len(results) >= 1000:
            truncated = True
            break
        results.append(value)
        chars += len(value) + 1
    results.sort()
    return {"matches": results, "count": len(results), "truncated": truncated}


def _python_literal_grep(pattern: str, base: Path, include: str | None, limit: int, cancel_event: threading.Event, deadline: float) -> tuple[list[dict[str, Any]], bool]:
    matches: list[dict[str, Any]] = []
    chars = 0
    candidates = base.rglob(include or "*") if base.is_dir() else [base]
    for path in candidates:
        ensure_live(cancel_event, deadline)
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    if pattern not in line:
                        continue
                    text = line.rstrip("\n")[:4096]
                    entry = {"path": str(path), "line": line_number, "text": text}
                    size = len(str(path)) + len(text) + 32
                    if len(matches) >= limit or chars + size > MAX_RESULT_CHARS:
                        return matches, True
                    matches.append(entry)
                    chars += size
        except (OSError, UnicodeError):
            continue
    return matches, False


def grep_files(args: dict[str, Any], workdir: str, cancel_event: threading.Event, deadline: float) -> dict[str, Any]:
    ensure_live(cancel_event, deadline)
    pattern = str(args.get("pattern", ""))
    if not pattern:
        raise WorkerError("invalid_request", "grep pattern is required")
    base = resolve_path(str(args.get("path") or workdir), workdir)
    include = args.get("include")
    include = str(include) if include else None
    literal = bool(args.get("literal", False))
    limit = int(args.get("limit", 200))
    if limit < 1 or limit > 2000:
        raise WorkerError("invalid_request", "grep limit must be within 1..2000")
    rg = shutil.which("rg")
    if rg:
        command = [rg, "--line-number", "--no-heading", "--color", "never", "--max-columns", "4096"]
        if literal:
            command.append("--fixed-strings")
        if include:
            command += ["--glob", include]
        command += [pattern, str(base)]
        result = run_shell(
            {"command": " ".join(shlex_quote(part) for part in command), "cwd": workdir, "max_output_chars": MAX_RESULT_CHARS},
            workdir,
            cancel_event,
            deadline,
        )
        if result["exit_code"] not in {0, 1}:
            raise WorkerError("grep_failed", result["stderr"][:4096] or f"rg exited {result['exit_code']}")
        lines = result["stdout"].splitlines()
        truncated = result["truncated"] or len(lines) > limit
        return {"matches": lines[:limit], "count": min(len(lines), limit), "truncated": truncated, "engine": "rg"}
    if not literal:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise WorkerError("invalid_pattern", str(exc)) from exc
        raise WorkerError("dependency_missing", "regex grep requires ripgrep (rg); use literal=true for stdlib fallback")
    matches, truncated = _python_literal_grep(pattern, base, include, limit, cancel_event, deadline)
    return {"matches": matches, "count": len(matches), "truncated": truncated, "engine": "python-literal"}


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def execute_job(job: dict[str, Any], workdir: str, cancel_event: threading.Event) -> Any:
    action = job.get("action")
    args = job.get("arguments")
    if not isinstance(args, dict):
        raise WorkerError("invalid_job", "job.arguments must be an object")
    deadline = parse_deadline(job)
    handlers = {
        "shell": run_shell,
        "read": read_text,
        "write": write_text,
        "edit": edit_text,
        "glob": glob_files,
        "grep": grep_files,
    }
    handler = handlers.get(str(action))
    if handler is None:
        raise WorkerError("unsupported_action", f"unsupported remote action: {action!r}")
    return handler(args, workdir, cancel_event, deadline)


def error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, WorkerError):
        return {"code": exc.code, "message": str(exc), **({"details": exc.details} if exc.details is not None else {})}
    return {"code": "worker_error", "message": str(exc)[:4096] or exc.__class__.__name__}


def resume(identity: dict[str, Any]) -> dict[str, Any]:
    value = post_json(
        identity["server"],
        "resume",
        {
            "protocol_version": PROTOCOL_VERSION,
            "workdir": identity["workdir"],
            "capabilities": CAPABILITIES,
            "info": worker_info(),
        },
        token=identity["token"],
    )
    if value.get("name") != identity["name"]:
        identity["name"] = str(value["name"])
        atomic_json(identity_path(), identity)
    return value


def submit_result_with_retry(identity: dict[str, Any], result: dict[str, Any]) -> Any:
    backoff = 1.0
    deadline = time.monotonic() + 30.0
    while True:
        try:
            return post_json(
                identity["server"],
                "result",
                result,
                token=identity["token"],
                timeout_s=20.0,
            )
        except WorkerHTTPError as exc:
            if exc.status in {401, 403}:
                raise WorkerError("revoked", "remote worker identity is revoked or unauthorized") from exc
            if 400 <= exc.status < 500 and exc.status != 429:
                raise
            if time.monotonic() >= deadline:
                raise
            print(f"dsh-remote-worker: result submission error: {exc}; retrying", file=sys.stderr, flush=True)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if time.monotonic() >= deadline:
                raise
            print(f"dsh-remote-worker: result submission connection error: {exc}; retrying", file=sys.stderr, flush=True)
        sleep_s = min(backoff, max(0.0, deadline - time.monotonic()))
        if sleep_s <= 0:
            raise WorkerError("result_delivery_failed", "remote job result could not be delivered")
        time.sleep(sleep_s)
        backoff = min(backoff * 2.0, 8.0)


def run_loop(identity: dict[str, Any]) -> None:
    backoff = 1.0
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dsh-remote-job")
    try:
        while True:
            try:
                connected = resume(identity)
                poll_timeout = float(connected.get("poll_timeout_s", identity.get("poll_timeout_s", DEFAULT_POLL_TIMEOUT_S)))
                heartbeat_s = float(connected.get("heartbeat_interval_s", identity.get("heartbeat_interval_s", DEFAULT_HEARTBEAT_S)))
                backoff = 1.0
                while True:
                    polled = post_json(
                        identity["server"],
                        "poll",
                        {"protocol_version": PROTOCOL_VERSION},
                        token=identity["token"],
                        timeout_s=max(35.0, poll_timeout + 10.0),
                    )
                    job = polled.get("job") if isinstance(polled, dict) else None
                    if not job:
                        continue
                    job_id = str(job.get("id", ""))
                    if not job_id:
                        continue
                    cancel_event = threading.Event()
                    future = executor.submit(execute_job, job, identity["workdir"], cancel_event)
                    while True:
                        try:
                            value = future.result(timeout=max(1.0, heartbeat_s))
                            result = {"job_id": job_id, "ok": True, "value": value}
                            break
                        except concurrent.futures.TimeoutError:
                            try:
                                heartbeat = post_json(
                                    identity["server"],
                                    "heartbeat",
                                    {"job_id": job_id},
                                    token=identity["token"],
                                    timeout_s=15.0,
                                )
                            except WorkerHTTPError as exc:
                                if exc.status in {401, 403} or (400 <= exc.status < 500 and exc.status != 429):
                                    cancel_event.set()
                                    raise
                                print(f"dsh-remote-worker: heartbeat server error: {exc}; continuing job", file=sys.stderr, flush=True)
                                continue
                            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                                print(f"dsh-remote-worker: heartbeat connection error: {exc}; continuing job", file=sys.stderr, flush=True)
                                continue
                            if isinstance(heartbeat, dict) and heartbeat.get("cancelled"):
                                cancel_event.set()
                            continue
                        except Exception as exc:  # noqa: BLE001 - job failures are protocol results, not worker failures.
                            result = {"job_id": job_id, "ok": False, "error": error_payload(exc)}
                            break
                    submit_result_with_retry(identity, result)
            except WorkerHTTPError as exc:
                if exc.status in {401, 403}:
                    raise WorkerError("revoked", "remote worker identity is revoked or unauthorized") from exc
                if 400 <= exc.status < 500 and exc.status != 429:
                    raise
                print(f"dsh-remote-worker: server error: {exc}; retrying", file=sys.stderr, flush=True)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"dsh-remote-worker: connection error: {exc}; retrying", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def install_service(*, start: bool) -> None:
    source = Path(__file__).resolve()
    target_dir = install_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "worker.py"
    if source != target:
        shutil.copy2(source, target)
    service = service_path()
    service.parent.mkdir(parents=True, exist_ok=True)
    unit = "\n".join(
        [
            "[Unit]",
            "Description=DSH remote worker",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={shlex_quote(sys.executable)} {shlex_quote(str(target))} run",
            "Restart=on-failure",
            "RestartSec=5",
            "NoNewPrivileges=true",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    service.write_text(unit, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "dsh-remote-worker.service"], check=True)
    if start:
        subprocess.run(["systemctl", "--user", "restart", "dsh-remote-worker.service"], check=True)


def service_action(action: str) -> int:
    command = ["systemctl", "--user"]
    if action == "status":
        command += ["status", "--no-pager", "dsh-remote-worker.service"]
    else:
        command += [action, "dsh-remote-worker.service"]
    return subprocess.run(command, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dsh-remote-worker")
    sub = parser.add_subparsers(dest="command", required=True)

    enroll_parser = sub.add_parser("enroll")
    enroll_parser.add_argument("--server", required=True)
    enroll_parser.add_argument("--invite", required=True)
    enroll_parser.add_argument("--name")
    enroll_parser.add_argument("--workdir")

    sub.add_parser("run")
    install_parser = sub.add_parser("install-service")
    install_parser.add_argument("--start", action="store_true")
    for action in ("start", "stop", "restart", "status"):
        sub.add_parser(action)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "enroll":
            identity = enroll(args.server, args.invite, args.name, args.workdir)
            print(json.dumps({"ok": True, "name": identity["name"], "workdir": identity["workdir"]}))
            return 0
        if args.command == "run":
            identity = load_identity()
            print(f"dsh-remote-worker: connected identity {identity['name']}", file=sys.stderr, flush=True)
            run_loop(identity)
            return 0
        if args.command == "install-service":
            load_identity()
            install_service(start=args.start)
            print("dsh-remote-worker: systemd user service installed")
            return 0
        return service_action(args.command)
    except WorkerError as exc:
        print(f"dsh-remote-worker: {exc.code}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
