from __future__ import annotations

import argparse
import ipaddress
import os
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from . import build_embedded_oauth_server
from .backend import ExperimentalWebHostBackend
from .routing import GatewayService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh-mcp-gateway",
        description="Expose a loopback DeepSeek Harness Web Host as an OAuth-protected MCP server.",
    )
    parser.add_argument(
        "--dsh-web-url",
        default="http://127.0.0.1:3080",
        help="DSH Web Host base URL; loopback is required by default.",
    )
    parser.add_argument(
        "--dsh-cwd",
        default=os.getcwd(),
        help="Working directory used when the gateway creates a new DSH session.",
    )
    parser.add_argument(
        "--public-base-url",
        required=True,
        help="Public HTTPS origin used as OAuth issuer, for example https://gateway.example.com.",
    )
    parser.add_argument(
        "--state-dir",
        default=".dsh-mcp-gateway",
        help="Directory for persistent OAuth state.",
    )
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument(
        "--allow-non-loopback-bind",
        action="store_true",
        help="Explicitly allow the plain-HTTP gateway listener to bind beyond loopback.",
    )
    parser.add_argument("--port", type=int, default=8766)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.port <= 0 or args.port > 65535:
        raise SystemExit("--port must be in [1, 65535]")
    if not args.allow_non_loopback_bind:
        try:
            bind_is_loopback = args.bind_host == "localhost" or ipaddress.ip_address(args.bind_host).is_loopback
        except ValueError:
            bind_is_loopback = False
        if not bind_is_loopback:
            raise SystemExit(
                "--bind-host must be loopback unless --allow-non-loopback-bind is explicitly supplied"
            )
    public_base = args.public_base_url.rstrip("/")
    parsed_public = urlparse(public_base)
    if parsed_public.scheme != "https" or not parsed_public.hostname:
        raise SystemExit("--public-base-url must be an absolute https:// origin")
    if parsed_public.path not in {"", "/"} or parsed_public.query or parsed_public.fragment:
        raise SystemExit("--public-base-url must be an origin without a path, query, or fragment")
    admin_pin = os.environ.get("DSH_MCP_GATEWAY_ADMIN_PIN", "")
    if not admin_pin:
        raise SystemExit("DSH_MCP_GATEWAY_ADMIN_PIN is required")

    try:
        from .oauth import EmbeddedOAuthConfig
    except ImportError as exc:
        raise SystemExit("install dsh-mcp-gateway[server] to run the HTTP/OAuth gateway") from exc

    backend = ExperimentalWebHostBackend(
        args.dsh_web_url,
        cwd=args.dsh_cwd,
    )
    host_descriptor = backend.describe_host()
    state_dir = Path(args.state_dir).resolve()
    oauth = EmbeddedOAuthConfig(
        issuer_url=public_base,
        resource_url=f"{public_base}/mcp",
        state_db=state_dir / "oauth.sqlite3",
        admin_pin=admin_pin,
    )
    server, _provider = build_embedded_oauth_server(GatewayService(backend), oauth)
    print(
        f"DSH Web Host reachable at {backend.base_url} "
        f"(reported version {host_descriptor.get('version', 'unknown')}; diagnostic only)"
    )
    print(f"MCP gateway listening on http://{args.bind_host}:{args.port}/mcp")
    print(f"OAuth issuer: {oauth.issuer_url}")
    server.run(
        transport="streamable-http",
        host=args.bind_host,
        port=args.port,
        streamable_http_path="/mcp",
        json_response=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
