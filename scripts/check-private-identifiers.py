#!/usr/bin/env python3
"""Fail when production infrastructure identifiers are tracked by Git.

The check is intentionally value-redacting: it reports only identifier classes and
file paths, never the identifier values themselves. On a production host it derives
private identifiers from local config/metadata. In CI, where that config is absent,
it still rejects literal public IPv4 addresses in tracked text files.
"""

from __future__ import annotations

import argparse
import ipaddress
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

IPV4_RE = re.compile(rb"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
DEFAULT_CONFIGS = (
    pathlib.Path("/home/ubuntu/workspace/.dsh-cloudflared/config.yml"),
    pathlib.Path("/etc/dsh-cloudflared/config.yml"),
)


def tracked_files(root: pathlib.Path) -> list[pathlib.Path]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    return [root / item.decode(errors="surrogateescape") for item in raw.split(b"\0") if item]


def read_production_config(paths: tuple[pathlib.Path, ...]) -> tuple[str, str]:
    text = ""
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if text:
            break
    hostname = ""
    tunnel_id = ""
    match = re.search(r"(?m)^\s*-?\s*hostname:\s*([^\s#]+)", text)
    if match:
        hostname = match.group(1).strip()
    match = re.search(r"(?m)^\s*tunnel:\s*([^\s#]+)", text)
    if match:
        tunnel_id = match.group(1).strip()
    return hostname, tunnel_id


def detect_public_ipv4() -> str:
    for url in (
        "http://metadata.tencentyun.com/latest/meta-data/public-ipv4",
        "https://api.ipify.org",
    ):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "dsh-private-audit/1.0"})
            value = urllib.request.urlopen(request, timeout=3).read().decode().strip()
            address = ipaddress.ip_address(value)
            if address.version == 4 and address.is_global:
                return value
        except (OSError, UnicodeError, ValueError, urllib.error.URLError):
            continue
    return ""


def is_public_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and address.is_global


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Git worktree root")
    parser.add_argument(
        "--no-network-ip-detection",
        action="store_true",
        help="Do not query local metadata/external IP reflection endpoints",
    )
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    files = tracked_files(root)
    hostname, tunnel_id = read_production_config(DEFAULT_CONFIGS)
    public_ipv4 = "" if args.no_network_ip_detection else detect_public_ipv4()

    private_values = {
        "production-domain": hostname,
        "production-public-ipv4": public_ipv4,
        "production-tunnel-id": tunnel_id,
    }
    findings: dict[str, set[str]] = {name: set() for name in private_values}
    findings["literal-public-ipv4"] = set()

    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        for name, value in private_values.items():
            if value and value.encode() in data:
                findings[name].add(rel)
        for match in IPV4_RE.finditer(data):
            value = match.group().decode()
            if is_public_ipv4(value):
                findings["literal-public-ipv4"].add(rel)

    failed = False
    for name, paths in findings.items():
        if not paths:
            continue
        failed = True
        print(f"PRIVATE_IDENTIFIER {name}: {len(paths)} tracked file(s)", file=sys.stderr)
        for path in sorted(paths):
            print(f"  {path}", file=sys.stderr)

    derived = [name for name, value in private_values.items() if value]
    if failed:
        return 1
    print(
        "private-identifiers-ok: "
        f"tracked_files={len(files)} derived_checks={len(derived)} public_ipv4_literals=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
