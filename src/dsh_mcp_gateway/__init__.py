"""dsh-mcp-gateway package."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Any

from .harness_bridge import (
    HarnessBridgeClient,
    HarnessProjectionMixin,
    tool_result_to_mcp,
    watch_tool_catalog,
)
from .mcp_compat import disable_modern_subscriptions

__version__ = "0.1.0"


def build_mcp_server(
    harness_bridge: HarnessBridgeClient | None,
    *,
    project_dsh_tools: bool = False,
    auth_server_provider: Any | None = None,
    auth: Any | None = None,
    _server_cls: Any | None = None,
) -> Any:
    """Build the ChatGPT-facing MCP surface over one DSH Harness bridge."""
    if project_dsh_tools and harness_bridge is None:
        raise ValueError("project_dsh_tools requires harness_bridge")

    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise RuntimeError("MCP dependencies are unavailable; install dsh-mcp-gateway[server]") from exc

    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    consequential_control = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )

    server_cls = _server_cls or MCPServer
    harness_lifespan = None
    if harness_bridge is not None and project_dsh_tools:
        try:
            from mcp.shared.subscriptions import ToolsListChanged
        except ImportError as exc:  # pragma: no cover - server dependency boundary
            raise RuntimeError("MCP subscription support is unavailable") from exc

        @asynccontextmanager
        async def harness_lifespan(app: Any):
            async def publish_changed() -> None:
                await app._subscriptions.publish(ToolsListChanged())

            watcher = asyncio.create_task(
                watch_tool_catalog(harness_bridge, publish_changed),
                name="dsh-harness-tool-catalog-watcher",
            )
            try:
                yield {}
            finally:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher

        server_cls = type(
            "HarnessProjectedMCPServer",
            (HarnessProjectionMixin, server_cls),
            {},
        )

    instructions = (
        "DSH is the harness authority and ChatGPT is the reasoning agent. The stable meta-tools "
        "dsh_tool_catalog/dsh_tool_call and dsh_skill_catalog/dsh_skill_load are the correctness path for DSH "
        "community extensions: use them to discover and invoke capabilities even if the ChatGPT client keeps a "
        "frozen MCP tool snapshot."
        + (
            " This server also projects compatible DSH ToolRuntime capabilities as first-class MCP tools as an "
            "explicit optional UX mode; extension availability must not depend on that projection."
            if project_dsh_tools
            else " This server is in meta-only mode, so DSH-internal tools are intentionally absent from the MCP tool list."
        )
    )
    mcp = server_cls(
        "dsh-mcp-gateway",
        version=__version__,
        description="Expose DSH Harness capabilities to ChatGPT Web.",
        instructions=instructions,
        auth_server_provider=auth_server_provider,
        auth=auth,
        lifespan=harness_lifespan,
    )

    if harness_bridge is not None and not project_dsh_tools:
        disable_modern_subscriptions(mcp)
    if harness_bridge is not None:
        mcp._dsh_harness_bridge = harness_bridge

        @mcp.tool(name="dsh_tool_catalog", annotations=read_only)
        def dsh_tool_catalog() -> dict[str, Any]:
            """List the current DSH ToolRuntime schemas exposed by installed DSH plugins."""
            tools = harness_bridge.tools()
            return {"tools": tools, "count": len(tools)}

        @mcp.tool(name="dsh_tool_call", annotations=consequential_control)
        def dsh_tool_call(name: str, arguments: dict[str, Any] | None = None) -> Any:
            """Execute one discovered DSH tool through DSH's guarded ToolRuntime pipeline."""
            return tool_result_to_mcp(harness_bridge.call(name, arguments))

        @mcp.tool(name="dsh_skill_catalog", annotations=read_only)
        def dsh_skill_catalog() -> dict[str, Any]:
            """List model-invocable skills from DSH's native SkillRegistry for the Harness workspace."""
            skills = harness_bridge.skills()
            return {"skills": skills, "count": len(skills)}

        @mcp.tool(name="dsh_skill_load", annotations=read_only)
        def dsh_skill_load(name: str) -> dict[str, Any]:
            """Load one DSH community skill's instructions through the native SkillRegistry."""
            return {"skill": harness_bridge.load_skill(name)}

    return mcp


def build_embedded_oauth_server(
    harness_bridge: HarnessBridgeClient | None,
    config: Any,
    *,
    project_dsh_tools: bool = False,
) -> tuple[Any, Any]:
    """Build a self-contained OAuth-protected MCP server plus its provider."""
    try:
        from mcp.server import MCPServer
        from mcp.server.auth.settings import (
            AuthSettings,
            ClientRegistrationOptions,
            RevocationOptions,
        )
        from pydantic import AnyHttpUrl
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise RuntimeError("MCP auth dependencies are unavailable; install dsh-mcp-gateway[server]") from exc

    from .oauth import (
        EmbeddedOAuthProvider,
        advertise_public_client_auth_methods,
        install_approval_body_limit,
        install_approval_route,
        install_oauth_form_body_limit,
        install_registration_body_limit,
    )

    class EmbeddedOAuthMCPServer(MCPServer):
        def streamable_http_app(self, *args: Any, **kwargs: Any) -> Any:
            app = super().streamable_http_app(*args, **kwargs)
            advertise_public_client_auth_methods(app, self.settings.auth)
            install_registration_body_limit(
                app,
                max_bytes=config.max_registration_request_bytes,
            )
            install_approval_body_limit(app)
            install_oauth_form_body_limit(app)
            return app

    provider = EmbeddedOAuthProvider(config)
    scopes = list(config.scopes)
    required_scopes = list(config.required_scopes)
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(config.issuer_url),
        resource_server_url=AnyHttpUrl(config.resource_url),
        required_scopes=required_scopes,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=scopes,
            default_scopes=scopes,
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    server = build_mcp_server(
        harness_bridge,
        project_dsh_tools=project_dsh_tools,
        auth_server_provider=provider,
        auth=auth,
        _server_cls=EmbeddedOAuthMCPServer,
    )
    install_approval_route(server, provider)
    return server, provider
