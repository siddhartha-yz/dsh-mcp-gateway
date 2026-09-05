from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from . import build_embedded_oauth_server
from .harness_bridge import HarnessBridgeClient


def build_transport_security(public_base: str):
    """Build the DNS-rebinding allowlist for one validated public HTTPS origin."""
    from mcp.server.transport_security import TransportSecuritySettings

    parsed_public = urlparse(public_base)
    if parsed_public.scheme != "https" or not parsed_public.hostname:
        raise ValueError("public_base must be an absolute https:// origin")
    if parsed_public.username is not None or parsed_public.password is not None:
        raise ValueError("public_base must not contain user info")
    if (
        parsed_public.path not in {"", "/"}
        or parsed_public.params
        or parsed_public.query
        or parsed_public.fragment
    ):
        raise ValueError("public_base must be an origin without path, params, query, or fragment")
    try:
        public_port = parsed_public.port
    except ValueError as exc:
        raise ValueError("public_base must contain a valid port") from exc
    canonical_public_base = public_base[:-1] if parsed_public.path == "/" else public_base
    normalized_host = f"[{parsed_public.hostname}]" if ":" in parsed_public.hostname else parsed_public.hostname
    normalized_authority = normalized_host if public_port is None else f"{normalized_host}:{public_port}"
    allowed_hosts = [
        parsed_public.netloc,
        normalized_authority,
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    ]
    allowed_origins = [canonical_public_base, f"https://{normalized_authority}"]
    if public_port in {None, 443}:
        allowed_hosts.extend([normalized_host, f"{normalized_host}:443"])
        allowed_origins.extend([f"https://{normalized_host}", f"https://{normalized_host}:443"])
    allowed_origins.extend(
        [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ]
    )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(allowed_hosts)),
        allowed_origins=list(dict.fromkeys(allowed_origins)),
    )


def install_health_routes(server, harness_bridge) -> None:
    """Install minimal unauthenticated process and DSH readiness probes."""
    try:
        from starlette.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise RuntimeError("MCP server dependencies are unavailable") from exc

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_request):
        return JSONResponse(
            {"ok": True, "service": "dsh-mcp-gateway"},
            headers={"Cache-Control": "no-store"},
        )

    @server.custom_route("/readyz", methods=["GET"], include_in_schema=False)
    async def readyz(_request):
        try:
            await asyncio.to_thread(harness_bridge.tools, timeout_s=1.0)
        except Exception:  # noqa: BLE001 - dependency failures collapse to a non-sensitive readiness result.
            return JSONResponse(
                {"ok": False, "dependency": "dsh-harness-bridge"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"ok": True, "dependency": "dsh-harness-bridge"},
            headers={"Cache-Control": "no-store"},
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh-mcp-gateway",
        description="Expose DSH Harness capabilities as an OAuth-protected MCP server.",
    )
    parser.add_argument(
        "--dsh-harness-url",
        default=None,
        help="DSH Harness bridge origin, normally loopback (for example http://127.0.0.1:3080).",
    )
    parser.add_argument(
        "--tool-surface",
        choices=("meta-only", "projected"),
        default="meta-only",
        help=(
            "ChatGPT-facing DSH tool surface. meta-only (default) exposes only stable catalog/call meta-tools; "
            "projected additionally exposes individual DSH tools and publishes tools/list_changed."
        ),
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
    parser.add_argument(
        "--max-registered-clients",
        type=int,
        default=256,
        help="Maximum persisted dynamic OAuth clients before new registrations are rejected.",
    )
    parser.add_argument(
        "--max-client-metadata-bytes",
        type=int,
        default=32 * 1024,
        help="Maximum UTF-8 bytes persisted for one normalized dynamic OAuth client record.",
    )
    parser.add_argument(
        "--max-registration-request-bytes",
        type=int,
        default=64 * 1024,
        help="Maximum raw HTTP body bytes accepted by the anonymous dynamic-client registration endpoint.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.port <= 0 or args.port > 65535:
        raise SystemExit("--port must be in [1, 65535]")
    if args.max_registered_clients <= 0:
        raise SystemExit("--max-registered-clients must be positive")
    if args.max_client_metadata_bytes <= 0:
        raise SystemExit("--max-client-metadata-bytes must be positive")
    if args.max_registration_request_bytes <= 0:
        raise SystemExit("--max-registration-request-bytes must be positive")
    if not args.allow_non_loopback_bind:
        try:
            bind_is_loopback = args.bind_host == "localhost" or ipaddress.ip_address(args.bind_host).is_loopback
        except ValueError:
            bind_is_loopback = False
        if not bind_is_loopback:
            raise SystemExit(
                "--bind-host must be loopback unless --allow-non-loopback-bind is explicitly supplied"
            )

    public_base = args.public_base_url
    parsed_public = urlparse(public_base)
    if parsed_public.scheme != "https" or not parsed_public.hostname:
        raise SystemExit("--public-base-url must be an absolute https:// origin")
    try:
        _ = parsed_public.port
    except ValueError as exc:
        raise SystemExit("--public-base-url must contain a valid port") from exc
    if parsed_public.username is not None or parsed_public.password is not None:
        raise SystemExit("--public-base-url must not contain user info")
    if (
        parsed_public.path not in {"", "/"}
        or parsed_public.params
        or parsed_public.query
        or parsed_public.fragment
    ):
        raise SystemExit("--public-base-url must be an origin without a path, params, query, or fragment")
    public_base = public_base[:-1] if parsed_public.path == "/" else public_base

    admin_pin = os.environ.get("DSH_MCP_GATEWAY_ADMIN_PIN", "")
    if not admin_pin:
        raise SystemExit("DSH_MCP_GATEWAY_ADMIN_PIN is required")
    if not args.dsh_harness_url:
        raise SystemExit("--dsh-harness-url is required")

    try:
        from .oauth import EmbeddedOAuthConfig

        transport_security = build_transport_security(public_base)
    except ImportError as exc:
        raise SystemExit("install dsh-mcp-gateway[server] to run the HTTP/OAuth gateway") from exc

    try:
        harness_bridge = HarnessBridgeClient(args.dsh_harness_url)
    except ValueError as exc:
        raise SystemExit(f"invalid --dsh-harness-url: {exc}") from exc

    state_dir = Path(args.state_dir).absolute()
    oauth = EmbeddedOAuthConfig(
        issuer_url=public_base,
        resource_url=f"{public_base}/mcp",
        state_db=state_dir / "oauth.sqlite3",
        admin_pin=admin_pin,
        max_registered_clients=args.max_registered_clients,
        max_client_metadata_bytes=args.max_client_metadata_bytes,
        max_registration_request_bytes=args.max_registration_request_bytes,
    )
    server, _provider = build_embedded_oauth_server(
        harness_bridge,
        oauth,
        project_dsh_tools=args.tool_surface == "projected",
    )
    install_health_routes(server, harness_bridge)
    print(
        f"DSH Harness bridge configured at {harness_bridge.base_url}; ChatGPT remains the reasoning agent; "
        f"tool surface={args.tool_surface}"
    )
    print(f"MCP gateway listening on http://{args.bind_host}:{args.port}/mcp")
    print(f"OAuth issuer: {oauth.issuer_url}")
    server.run(
        transport="streamable-http",
        host=args.bind_host,
        port=args.port,
        streamable_http_path="/mcp",
        json_response=True,
        transport_security=transport_security,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
