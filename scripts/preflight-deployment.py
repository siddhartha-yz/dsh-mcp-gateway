#!/usr/bin/env python3
"""Unprivileged preflight for the documented self-hosted deployment layout."""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

TESTED_DSH_VERSION = "0.1.0-rc.6"
TESTED_NODE_VERSION = "24.19.0"
MIN_PYTHON = (3, 11)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


class Preflight:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail))

    def require_path(self, name: str, path: Path, *, kind: str) -> bool:
        if kind == "dir":
            ok = path.is_dir()
        elif kind == "file":
            ok = path.is_file()
        else:  # pragma: no cover - internal programming error
            raise ValueError(f"unknown path kind: {kind}")
        self.add(name, ok, f"{path} ({'present' if ok else 'missing'})")
        return ok

    def require_user(self, name: str, username: str) -> pwd.struct_passwd | None:
        try:
            record = pwd.getpwnam(username)
        except KeyError:
            self.add(name, False, f"user {username!r} does not exist")
            return None
        self.add(name, True, f"user {username!r} exists (uid={record.pw_uid})")
        return record

    def require_group(self, name: str, groupname: str) -> grp.struct_group | None:
        try:
            record = grp.getgrnam(groupname)
        except KeyError:
            self.add(name, False, f"group {groupname!r} does not exist")
            return None
        self.add(name, True, f"group {groupname!r} exists (gid={record.gr_gid})")
        return record

    def check_owner_mode(
        self,
        name: str,
        path: Path,
        *,
        uid: int | None,
        gid: int | None,
        expected_mode: int,
    ) -> None:
        if not path.exists():
            self.add(name, False, f"{path} is missing")
            return
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
        owner_ok = uid is not None and info.st_uid == uid
        group_ok = gid is not None and info.st_gid == gid
        mode_ok = mode == expected_mode
        self.add(
            name,
            owner_ok and group_ok and mode_ok,
            f"{path}: uid={info.st_uid} gid={info.st_gid} mode={mode:04o}; expected uid={uid} gid={gid} mode={expected_mode:04o}",
        )


def parse_env_file(path: Path) -> tuple[dict[str, str], str | None]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return {}, f"cannot read environment file: {type(exc).__name__}"
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            return {}, f"invalid assignment syntax at line {line_number}"
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            return {}, f"empty key at line {line_number}"
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values, None


def is_https_origin(value: str) -> bool:
    parsed = urlparse(value.rstrip("/"))
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    return True


def run_command(executable: str | Path, *args: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(executable), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"cannot execute: {type(exc).__name__}"
    text = (result.stdout or result.stderr).strip().splitlines()
    rendered = text[0] if text else f"exit={result.returncode}"
    return result.returncode == 0, rendered


def run_version(executable: Path) -> tuple[tuple[int, int] | None, str]:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"cannot execute: {type(exc).__name__}"
    text = (result.stdout or result.stderr).strip().splitlines()
    rendered = text[0] if text else f"exit={result.returncode}"
    if result.returncode != 0:
        return None, rendered
    parts = rendered.removeprefix("Python ").split(".")
    try:
        version = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        return None, rendered
    return version, rendered


def check_executable(preflight: Preflight, name: str, path: Path) -> None:
    ok = path.is_file() and os.access(path, os.X_OK)
    preflight.add(name, ok, f"{path} ({'executable' if ok else 'missing or not executable'})")


def check_file_matches(preflight: Preflight, name: str, installed: Path, template: Path) -> None:
    if not installed.is_file() or not template.is_file():
        preflight.add(name, False, f"installed={installed.is_file()} template={template.is_file()}")
        return
    try:
        ok = installed.read_bytes() == template.read_bytes()
    except OSError as exc:
        preflight.add(name, False, f"cannot compare files: {type(exc).__name__}")
        return
    preflight.add(name, ok, "installed file matches checked-in source" if ok else "installed file differs from checked-in source")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check the documented dsh-mcp-gateway deployment layout without root.")
    parser.add_argument("--dsh-runtime", type=Path, default=Path("/opt/dsh-runtime"))
    parser.add_argument("--gateway-root", type=Path, default=Path("/srv/dsh-mcp-gateway"))
    parser.add_argument("--workspace", type=Path, default=Path("/srv/dsh-workspace"))
    parser.add_argument(
        "--workspace-mode",
        type=lambda value: int(value, 8),
        default=0o750,
        help="Expected workspace directory mode in octal (default: 0750).",
    )
    parser.add_argument("--dsh-home", type=Path, default=Path("/var/lib/dsh-harness"))
    parser.add_argument("--gateway-state", type=Path, default=Path("/var/lib/dsh-mcp-gateway"))
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/dsh-mcp-gateway"))
    parser.add_argument("--systemd-dir", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument("--dsh-user", default="dsh-agent")
    parser.add_argument("--dsh-group", default="dsh-agent")
    parser.add_argument("--gateway-user", default="dsh-gateway")
    parser.add_argument("--gateway-group", default="dsh-gateway")
    parser.add_argument("--config-owner", default="root")
    parser.add_argument("--config-group", default="root")
    parser.add_argument("--expected-dsh-version", default=TESTED_DSH_VERSION)
    parser.add_argument("--expected-node-version", default=TESTED_NODE_VERSION)
    parser.add_argument(
        "--node-executable",
        type=Path,
        help="Node executable used by the DSH service; defaults to <dsh-runtime>/node/bin/node.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable results without secret values.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    p = Preflight()

    dsh_user = p.require_user("dsh service user", args.dsh_user)
    dsh_group = p.require_group("dsh service group", args.dsh_group)
    gateway_user = p.require_user("gateway service user", args.gateway_user)
    gateway_group = p.require_group("gateway service group", args.gateway_group)
    config_owner = p.require_user("config owner", args.config_owner)
    config_group = p.require_group("config group", args.config_group)

    for name, path in (
        ("DSH runtime directory", args.dsh_runtime),
        ("gateway root directory", args.gateway_root),
        ("workspace directory", args.workspace),
        ("DSH_HOME directory", args.dsh_home),
        ("gateway state directory", args.gateway_state),
        ("config directory", args.config_dir),
    ):
        p.require_path(name, path, kind="dir")

    p.check_owner_mode(
        "workspace ownership/mode",
        args.workspace,
        uid=dsh_user.pw_uid if dsh_user else None,
        gid=dsh_group.gr_gid if dsh_group else None,
        expected_mode=args.workspace_mode,
    )
    p.check_owner_mode(
        "DSH_HOME ownership/mode",
        args.dsh_home,
        uid=dsh_user.pw_uid if dsh_user else None,
        gid=dsh_group.gr_gid if dsh_group else None,
        expected_mode=0o700,
    )
    p.check_owner_mode(
        "gateway state ownership/mode",
        args.gateway_state,
        uid=gateway_user.pw_uid if gateway_user else None,
        gid=gateway_group.gr_gid if gateway_group else None,
        expected_mode=0o700,
    )
    p.check_owner_mode(
        "config directory ownership/mode",
        args.config_dir,
        uid=config_owner.pw_uid if config_owner else None,
        gid=config_group.gr_gid if config_group else None,
        expected_mode=0o700,
    )

    node_executable = args.node_executable or args.dsh_runtime / "node" / "bin" / "node"
    node_ok = node_executable.is_file() and os.access(node_executable, os.X_OK)
    p.add(
        "Node executable",
        node_ok,
        f"{node_executable} (executable)" if node_ok else f"{node_executable} is missing or not executable",
    )
    if node_ok:
        command_ok, rendered = run_command(node_executable, "--version")
        expected_rendered = f"v{args.expected_node_version}"
        p.add(
            "Node pinned version",
            command_ok and rendered == expected_rendered,
            f"version={rendered!r}; expected={expected_rendered!r}",
        )

    repo_dsh_runtime = args.gateway_root / "deploy" / "dsh-runtime"
    for filename in ("package.json", "package-lock.json"):
        check_file_matches(
            p,
            f"installed DSH {filename}",
            args.dsh_runtime / filename,
            repo_dsh_runtime / filename,
        )

    dsh_bin = args.dsh_runtime / "node_modules" / ".bin" / "dsh"
    dsh_package = args.dsh_runtime / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    check_executable(p, "DSH executable", dsh_bin)
    if p.require_path("DSH package metadata", dsh_package, kind="file"):
        try:
            package_data = json.loads(dsh_package.read_text(encoding="utf-8"))
            actual = package_data.get("version")
            ok = actual == args.expected_dsh_version
            p.add("DSH pinned version", ok, f"version={actual!r}; expected={args.expected_dsh_version!r}")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            p.add("DSH pinned version", False, f"cannot parse package metadata: {type(exc).__name__}")

    gateway_python = args.gateway_root / ".venv" / "bin" / "python"
    gateway_cli = args.gateway_root / ".venv" / "bin" / "dsh-mcp-gateway"
    check_executable(p, "gateway Python", gateway_python)
    check_executable(p, "gateway console script", gateway_cli)
    if gateway_cli.is_file() and os.access(gateway_cli, os.X_OK):
        ok, rendered = run_command(gateway_cli, "--help")
        p.add("gateway CLI import/help", ok, rendered)
    p.require_path("gateway pyproject", args.gateway_root / "pyproject.toml", kind="file")
    p.require_path("server constraints", args.gateway_root / "deploy" / "server-constraints.txt", kind="file")
    if gateway_python.is_file() and os.access(gateway_python, os.X_OK):
        version, rendered = run_version(gateway_python)
        p.add("gateway Python version", version is not None and version >= MIN_PYTHON, rendered)

    dsh_env_path = args.config_dir / "dsh.env"
    gateway_env_path = args.config_dir / "gateway.env"
    for name, path in (("DSH env file", dsh_env_path), ("gateway env file", gateway_env_path)):
        if p.require_path(name, path, kind="file"):
            p.check_owner_mode(
                f"{name} ownership/mode",
                path,
                uid=config_owner.pw_uid if config_owner else None,
                gid=config_group.gr_gid if config_group else None,
                expected_mode=0o600,
            )

    dsh_env, dsh_env_error = parse_env_file(dsh_env_path) if dsh_env_path.is_file() else ({}, "file missing")
    p.add("DSH env parse", dsh_env_error is None, dsh_env_error or "parsed without exposing values")
    if dsh_env_error is None:
        for key in ("DSH_HOME",):
            p.add(f"DSH env {key}", bool(dsh_env.get(key)), f"{key} is {'set' if dsh_env.get(key) else 'missing/empty'}")
        for obsolete_key in ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"):
            p.add(
                f"DSH env excludes {obsolete_key}",
                not bool(dsh_env.get(obsolete_key)),
                f"{obsolete_key} is absent/empty as required by the model-provider-free harness deployment"
                if not dsh_env.get(obsolete_key)
                else f"{obsolete_key} should not be configured for the primary harness deployment",
            )
        p.add(
            "DSH env DSH_HOME matches layout",
            dsh_env.get("DSH_HOME") == str(args.dsh_home),
            "DSH_HOME matches configured preflight path" if dsh_env.get("DSH_HOME") == str(args.dsh_home) else "DSH_HOME does not match configured preflight path",
        )

    gateway_env, gateway_env_error = (
        parse_env_file(gateway_env_path) if gateway_env_path.is_file() else ({}, "file missing")
    )
    p.add("gateway env parse", gateway_env_error is None, gateway_env_error or "parsed without exposing values")
    if gateway_env_error is None:
        for key in ("DSH_MCP_PUBLIC_BASE_URL", "DSH_MCP_GATEWAY_ADMIN_PIN"):
            p.add(
                f"gateway env {key}",
                bool(gateway_env.get(key)),
                f"{key} is {'set' if gateway_env.get(key) else 'missing/empty'}",
            )
        p.add(
            "gateway env excludes legacy DSH_WORKSPACE",
            not bool(gateway_env.get("DSH_WORKSPACE")),
            "DSH_WORKSPACE is absent/empty; workspace ownership stays with the DSH Host"
            if not gateway_env.get("DSH_WORKSPACE")
            else "DSH_WORKSPACE should not be configured on the thin OAuth/MCP gateway",
        )
        pin = gateway_env.get("DSH_MCP_GATEWAY_ADMIN_PIN", "")
        p.add(
            "gateway admin PIN length",
            len(pin) >= 12,
            "admin PIN has at least 12 characters" if len(pin) >= 12 else "admin PIN is missing or shorter than 12 characters",
        )
        public_base = gateway_env.get("DSH_MCP_PUBLIC_BASE_URL", "")
        p.add(
            "gateway public base is HTTPS origin",
            is_https_origin(public_base),
            "public base is a valid HTTPS origin" if is_https_origin(public_base) else "public base is not a valid HTTPS origin",
        )

    repo_systemd = args.gateway_root / "deploy" / "systemd"
    for filename in ("dsh-web-host.service", "dsh-mcp-gateway.service"):
        check_file_matches(
            p,
            f"installed {filename}",
            args.systemd_dir / filename,
            repo_systemd / filename,
        )

    failures = [check for check in p.checks if not check.ok]
    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failures,
                    "checks": [asdict(check) for check in p.checks],
                    "passed": len(p.checks) - len(failures),
                    "failed": len(failures),
                },
                indent=2,
            )
        )
    else:
        for check in p.checks:
            print(f"[{'PASS' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        print(f"preflight: {'PASS' if not failures else 'FAIL'} ({len(p.checks) - len(failures)}/{len(p.checks)} checks passed)")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
