#!/usr/bin/env python3
"""Validate one public HTTPS origin for deployment shell scripts."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from urllib.parse import urlparse


def is_https_origin(value: str) -> bool:
    parsed = urlparse(value)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("origin")
    args = parser.parse_args(argv)
    return 0 if is_https_origin(args.origin) else 1


if __name__ == "__main__":
    raise SystemExit(main())
