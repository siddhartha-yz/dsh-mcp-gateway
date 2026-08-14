#!/usr/bin/env python3
"""Verify that every direct server-extra dependency has an exact deployment pin."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^]]+\])?\s*==\s*([^;\s]+)")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct_server_names(pyproject_path: Path) -> list[str]:
    with pyproject_path.open("rb") as handle:
        document = tomllib.load(handle)
    try:
        requirements = document["project"]["optional-dependencies"]["server"]
    except (KeyError, TypeError) as exc:
        raise ValueError("pyproject.toml has no project.optional-dependencies.server list") from exc
    if not isinstance(requirements, list) or not all(isinstance(item, str) for item in requirements):
        raise ValueError("project.optional-dependencies.server must be a list of requirement strings")

    names: list[str] = []
    for requirement in requirements:
        match = _NAME_RE.match(requirement)
        if match is None:
            raise ValueError(f"cannot determine package name from server requirement: {requirement!r}")
        names.append(_normalize(match.group(1)))
    return names


def _exact_pins(constraints_path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in constraints_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN_RE.match(line)
        if match is None:
            continue
        name = _normalize(match.group(1))
        version = match.group(2)
        if name in pins and pins[name] != version:
            raise ValueError(f"constraints contain conflicting exact pins for {name}")
        pins[name] = version
    return pins


def verify(pyproject_path: Path, constraints_path: Path) -> dict[str, str]:
    direct_names = _direct_server_names(pyproject_path)
    pins = _exact_pins(constraints_path)
    missing = sorted({name for name in direct_names if name not in pins})
    if missing:
        raise ValueError(
            "server deployment constraints are missing exact pins for direct dependencies: "
            + ", ".join(missing)
        )
    return {name: pins[name] for name in direct_names}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--constraints", type=Path, default=Path("deploy/server-constraints.txt"))
    args = parser.parse_args()

    try:
        pins = verify(args.pyproject, args.constraints)
    except ValueError as exc:
        print(f"server-constraints-error: {exc}", file=sys.stderr)
        return 1
    rendered = ", ".join(f"{name}=={version}" for name, version in pins.items())
    print(f"server-direct-pins-ok: {rendered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
