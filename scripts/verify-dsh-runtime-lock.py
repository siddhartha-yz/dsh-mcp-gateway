#!/usr/bin/env python3
"""Verify the checked-in known-good DSH npm deployment graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXPECTED_DSH_VERSION = "0.1.2-rc.1"
EXPECTED_PNPM_VERSION = "10.34.5"
EXPECTED_LOCKFILE_VERSION = 3


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid JSON from {path}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def verify(root: Path, *, expected_dsh_version: str, expected_pnpm_version: str) -> tuple[int, str]:
    package_path = root / "package.json"
    lock_path = root / "package-lock.json"
    package = load_object(package_path)
    lock = load_object(lock_path)

    if package.get("private") is not True:
        raise ValueError("DSH runtime package.json must be private")
    dependencies = package.get("dependencies")
    expected_dependencies = {
        "@deepseek-ai/dsh": expected_dsh_version,
        "pnpm": expected_pnpm_version,
    }
    if not isinstance(dependencies, dict) or dependencies != expected_dependencies:
        raise ValueError("DSH runtime package.json must contain the exact tested DSH and pnpm dependencies")
    if lock.get("lockfileVersion") != EXPECTED_LOCKFILE_VERSION:
        raise ValueError(f"package-lock.json must use lockfileVersion {EXPECTED_LOCKFILE_VERSION}")

    packages = lock.get("packages")
    if not isinstance(packages, dict) or "" not in packages:
        raise ValueError("package-lock.json has no packages/root entry")
    root_entry = packages[""]
    if not isinstance(root_entry, dict):
        raise TypeError("package-lock.json root entry is invalid")
    root_dependencies = root_entry.get("dependencies")
    if not isinstance(root_dependencies, dict) or root_dependencies != dependencies:
        raise ValueError("package-lock.json root dependencies do not exactly match package.json")

    dsh_entry = packages.get("node_modules/@deepseek-ai/dsh")
    if not isinstance(dsh_entry, dict) or dsh_entry.get("version") != expected_dsh_version:
        raise ValueError("package-lock.json does not resolve the exact tested DSH version")
    resolved = dsh_entry.get("resolved")
    if not isinstance(resolved, str) or not resolved.endswith(f"/dsh-{expected_dsh_version}.tgz"):
        raise ValueError("package-lock.json DSH tarball does not match the exact tested version")

    pnpm_entry = packages.get("node_modules/pnpm")
    if not isinstance(pnpm_entry, dict) or pnpm_entry.get("version") != expected_pnpm_version:
        raise ValueError("package-lock.json does not resolve the exact tested pnpm version")

    missing_integrity: list[str] = []
    missing_version: list[str] = []
    for package_name, entry in packages.items():
        if package_name == "":
            continue
        if not isinstance(entry, dict):
            raise TypeError(f"package-lock entry {package_name!r} is not an object")
        integrity = entry.get("integrity")
        if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
            missing_integrity.append(package_name)
        version = entry.get("version")
        if not isinstance(version, str) or not version:
            missing_version.append(package_name)
    if missing_integrity:
        sample = ", ".join(sorted(missing_integrity)[:5])
        raise ValueError(f"npm lock entries are missing sha512 integrity metadata: {sample}")
    if missing_version:
        sample = ", ".join(sorted(missing_version)[:5])
        raise ValueError(f"npm lock entries are missing versions: {sample}")

    return len(packages) - 1, str(dsh_entry["integrity"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify deploy/dsh-runtime package.json and package-lock.json.")
    parser.add_argument("--root", type=Path, default=Path("deploy/dsh-runtime"))
    parser.add_argument("--expected-dsh-version", default=EXPECTED_DSH_VERSION)
    parser.add_argument("--expected-pnpm-version", default=EXPECTED_PNPM_VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        package_count, integrity = verify(
            args.root,
            expected_dsh_version=args.expected_dsh_version,
            expected_pnpm_version=args.expected_pnpm_version,
        )
    except (TypeError, ValueError) as exc:
        print(f"dsh-runtime-lock-error: {exc}", file=sys.stderr)
        return 1
    print(
        "dsh-runtime-lock-ok: "
        f"dsh={args.expected_dsh_version} pnpm={args.expected_pnpm_version} "
        f"packages={package_count} integrity={integrity.split('-', 1)[0]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
