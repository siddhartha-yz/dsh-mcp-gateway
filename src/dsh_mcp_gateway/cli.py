from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from . import build_embedded_oauth_server
from .backend import ExperimentalWebHostBackend
from .harness_bridge import HarnessBridgeClient
from .routing import GatewayService
from .session_runtime import DurableSessionRuntime


def build_transport_security(public_base: str):
    """Build the DNS-rebinding allowlist for one validated public HTTPS origin."""
    from mcp.server.transport_security import TransportSecuritySettings

    parsed_public = urlparse(public_base)
    if not parsed_public.hostname:
        raise ValueError("public_base must contain a hostname")
    public_host = parsed_public.netloc
    allowed_hosts = [public_host, "127.0.0.1:*", "localhost:*", "[::1]:*"]
    if parsed_public.port is None:
        host_for_port = f"[{parsed_public.hostname}]" if ":" in parsed_public.hostname else parsed_public.hostname
        allowed_hosts.append(f"{host_for_port}:443")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(allowed_hosts)),
        allowed_origins=[
            public_base,
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )


def install_health_routes(server, backend=None, harness_bridge=None) -> None:
    """Install minimal unauthenticated deployment probes.

    Runtime-only mode has no model-side dependency, so readiness means the
    gateway process and durable state initialized successfully. When the
    optional legacy DSH adapter is enabled, readiness also checks its Web Host.
    """
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
        if harness_bridge is not None:
            try:
                await asyncio.to_thread(harness_bridge.tools)
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
        if backend is None:
            return JSONResponse(
                {"ok": True, "dependency": "runtime-state"},
                headers={"Cache-Control": "no-store"},
            )
        try:
            await asyncio.to_thread(backend.describe_host, timeout_s=1.0)
        except Exception:  # noqa: BLE001 - dependency failures collapse to a non-sensitive readiness result.
            return JSONResponse(
                {"ok": False, "dependency": "dsh-web-host"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"ok": True, "dependency": "dsh-web-host"},
            headers={"Cache-Control": "no-store"},
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh-mcp-gateway",
        description="Expose a durable ChatGPT runtime as an OAuth-protected MCP server.",
    )
    parser.add_argument(
        "--dsh-harness-url",
        default=None,
        help="Primary DSH Harness bridge origin, normally loopback (for example http://127.0.0.1:3080).",
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
        "--dsh-web-url",
        default=None,
        help="Optional legacy DSH Web Host base URL. Omit for the default model-provider-free runtime.",
    )
    parser.add_argument(
        "--dsh-cwd",
        default=os.getcwd(),
        help="Working directory used only by the optional legacy DSH adapter.",
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
    public_base = args.public_base_url.rstrip("/")
    parsed_public = urlparse(public_base)
    if parsed_public.scheme != "https" or not parsed_public.hostname:
        raise SystemExit("--public-base-url must be an absolute https:// origin")
    if parsed_public.username is not None or parsed_public.password is not None:
        raise SystemExit("--public-base-url must not contain user info")
    if parsed_public.path not in {"", "/"} or parsed_public.query or parsed_public.fragment:
        raise SystemExit("--public-base-url must be an origin without a path, query, or fragment")
    admin_pin = os.environ.get("DSH_MCP_GATEWAY_ADMIN_PIN", "")
    if not admin_pin:
        raise SystemExit("DSH_MCP_GATEWAY_ADMIN_PIN is required")

    try:
        from .oauth import EmbeddedOAuthConfig

        transport_security = build_transport_security(public_base)
    except ImportError as exc:
        raise SystemExit("install dsh-mcp-gateway[server] to run the HTTP/OAuth gateway") from exc

    harness_bridge = None
    if args.dsh_harness_url:
        try:
            harness_bridge = HarnessBridgeClient(args.dsh_harness_url)
        except ValueError as exc:
            raise SystemExit(f"invalid --dsh-harness-url: {exc}") from exc

    backend = None
    service = None
    if args.dsh_web_url:
        try:
            backend = ExperimentalWebHostBackend(
                args.dsh_web_url,
                cwd=args.dsh_cwd,
            )
        except ValueError as exc:
            raise SystemExit(f"invalid --dsh-web-url: {exc}") from exc
        service = GatewayService(backend)
    state_dir = Path(args.state_dir).resolve()
    oauth = EmbeddedOAuthConfig(
        issuer_url=public_base,
        resource_url=f"{public_base}/mcp",
        state_db=state_dir / "oauth.sqlite3",
        admin_pin=admin_pin,
        max_registered_clients=args.max_registered_clients,
        max_client_metadata_bytes=args.max_client_metadata_bytes,
        max_registration_request_bytes=args.max_registration_request_bytes,
    )
    session_runtime = None if harness_bridge is not None else DurableSessionRuntime(state_dir / "sessions.sqlite3")
    server, _provider = build_embedded_oauth_server(
        service,
        oauth,
        session_runtime=session_runtime,
        harness_bridge=harness_bridge,
        project_dsh_tools=args.tool_surface == "projected",
    )
    install_health_routes(server, backend, harness_bridge)
    if harness_bridge is not None:
        print(
            f"DSH Harness bridge configured at {harness_bridge.base_url}; ChatGPT remains the reasoning agent; "
            f"tool surface={args.tool_surface}"
        )
    elif backend is None:
        print("Legacy standalone session runtime enabled; no DSH Harness bridge is configured")
    else:
        print(f"Legacy DSH Web Host configured at {backend.base_url}; readiness is checked via /readyz")
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
