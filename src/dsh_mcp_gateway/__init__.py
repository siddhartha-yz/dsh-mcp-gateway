"""dsh-mcp-gateway package."""

from __future__ import annotations

from dataclasses import dataclass
from os import PathLike
from typing import Any

from .backend import PublicSdkBridge, PublicSdkClient, SessionCatalog
from .routing import GatewayService

__version__ = "0.0.1.dev0"


def build_mcp_server(
    service: GatewayService,
    *,
    auth_server_provider: Any | None = None,
    auth: Any | None = None,
    _server_cls: Any | None = None,
) -> Any:
    """Build the MCP v2 tool surface around an injected gateway service."""
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
    mcp = server_cls(
        "dsh-mcp-gateway",
        version=__version__,
        description="Control long-lived DeepSeek Harness agent sessions.",
        instructions=(
            "Use dsh_start for a new task and keep its session_id. "
            "When reconnecting, use dsh_status and dsh_messages for a compact transcript; use dsh_history or "
            "dsh_history_page only when raw event detail is needed. Use dsh_search to recover an older session "
            "from remembered message text, then dsh_continue to steer it later."
        ),
        auth_server_provider=auth_server_provider,
        auth=auth,
    )

    @mcp.tool(name="dsh_start", annotations=consequential_control)
    def dsh_start(prompt: str, session_id: str | None = None) -> dict[str, str]:
        """Start work in a new or explicitly named DSH session and return a receipt."""
        return service.start(prompt, session_id=session_id).as_dict()

    @mcp.tool(name="dsh_continue", annotations=consequential_control)
    def dsh_continue(session_id: str, prompt: str) -> dict[str, str]:
        """Send another instruction to the same durable DSH session."""
        return service.continue_session(session_id, prompt).as_dict()

    @mcp.tool(name="dsh_status", annotations=read_only)
    def dsh_status(session_id: str) -> dict[str, Any]:
        """Read the current state of one DSH session."""
        return service.status(session_id)

    @mcp.tool(name="dsh_history", annotations=read_only)
    def dsh_history(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Read the newest durable or projected events for one DSH session."""
        return service.history(session_id, limit=limit)

    @mcp.tool(name="dsh_history_page", annotations=read_only)
    def dsh_history_page(
        session_id: str,
        before_seq: int | None = None,
        max_messages: int = 50,
    ) -> dict[str, Any]:
        """Read one durable history page backwards; use next_before_seq to load older pages."""
        return service.history_page(
            session_id,
            before_seq=before_seq,
            max_messages=max_messages,
        )

    @mcp.tool(name="dsh_messages", annotations=read_only)
    def dsh_messages(
        session_id: str,
        before_seq: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Read a compact human/model transcript without raw tool or reasoning events."""
        return service.messages(
            session_id,
            before_seq=before_seq,
            limit=limit,
        )

    @mcp.tool(name="dsh_list", annotations=read_only)
    def dsh_list() -> list[dict[str, Any]]:
        """List sessions visible to the configured DSH backend."""
        return service.list_sessions()

    @mcp.tool(name="dsh_search", annotations=read_only)
    def dsh_search(query: str) -> dict[str, Any]:
        """Search durable user/assistant/steering messages and return up to 20 matching sessions."""
        return service.search_sessions(query)

    @mcp.tool(name="dsh_cancel", annotations=consequential_control)
    def dsh_cancel(session_id: str) -> dict[str, Any]:
        """Cancel active work in a DSH session without replacing that session."""
        return service.cancel(session_id)

    @mcp.tool(name="dsh_goal_status", annotations=read_only)
    def dsh_goal_status(session_id: str) -> dict[str, Any]:
        """Read the durable current goal projection for one DSH session."""
        return service.goal_status(session_id)

    @mcp.tool(name="dsh_goal_create", annotations=consequential_control)
    def dsh_goal_create(
        session_id: str,
        objective: str,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        """Create and arm a durable DSH goal for an existing session."""
        return service.goal_create(
            session_id,
            objective,
            max_goal_rounds=max_goal_rounds,
        )

    @mcp.tool(name="dsh_goal_edit", annotations=consequential_control)
    def dsh_goal_edit(
        session_id: str,
        objective: str | None = None,
        max_goal_rounds: int | None = None,
    ) -> dict[str, Any]:
        """Edit the current durable goal objective and/or round cap using its latest CAS revision."""
        return service.goal_edit(
            session_id,
            objective=objective,
            max_goal_rounds=max_goal_rounds,
        )

    @mcp.tool(name="dsh_goal_resume", annotations=consequential_control)
    def dsh_goal_resume(session_id: str) -> dict[str, Any]:
        """Explicitly re-arm/resume the current goal using its latest durable CAS revision."""
        return service.goal_resume(session_id)

    @mcp.tool(name="dsh_goal_pause", annotations=consequential_control)
    def dsh_goal_pause(session_id: str) -> dict[str, Any]:
        """Pause the current goal using its latest durable CAS revision."""
        return service.goal_pause(session_id)

    @mcp.tool(name="dsh_goal_complete", annotations=consequential_control)
    def dsh_goal_complete(session_id: str) -> dict[str, Any]:
        """Mark the current non-complete durable goal complete using its latest CAS revision."""
        return service.goal_complete(session_id)

    @mcp.tool(name="dsh_goal_clear", annotations=consequential_control)
    def dsh_goal_clear(session_id: str) -> dict[str, Any]:
        """Clear the current durable goal while retaining DSH's durable tombstone/history."""
        return service.goal_clear(session_id)

    return mcp


@dataclass(slots=True)
class PublicSdkGateway:
    """Composed MCP facade over one caller-owned public DSH SDK client."""

    server: Any
    service: GatewayService
    bridge: PublicSdkBridge
    oauth_provider: Any | None = None

    def start(self) -> None:
        self.bridge.start()

    def close(self) -> None:
        self.bridge.close()


def build_embedded_oauth_server(service: GatewayService, config: Any) -> tuple[Any, Any]:
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
        install_approval_route,
    )

    class EmbeddedOAuthMCPServer(MCPServer):
        def streamable_http_app(self, *args: Any, **kwargs: Any) -> Any:
            app = super().streamable_http_app(*args, **kwargs)
            advertise_public_client_auth_methods(app, self.settings.auth)
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
        service,
        auth_server_provider=provider,
        auth=auth,
        _server_cls=EmbeddedOAuthMCPServer,
    )
    install_approval_route(server, provider)
    return server, provider


def build_public_sdk_oauth_gateway(
    client: PublicSdkClient,
    catalog_path: str | PathLike[str],
    oauth_config: Any,
    *,
    poll_interval_s: float = 0.05,
    event_buffer_size: int = 2000,
) -> PublicSdkGateway:
    """Compose an OAuth-protected MCP facade around an initialized DSH SDK client."""
    bridge = PublicSdkBridge(
        client,
        SessionCatalog(catalog_path),
        poll_interval_s=poll_interval_s,
        event_buffer_size=event_buffer_size,
    )
    service = GatewayService(bridge.backend)
    server, oauth_provider = build_embedded_oauth_server(service, oauth_config)
    return PublicSdkGateway(
        server=server,
        service=service,
        bridge=bridge,
        oauth_provider=oauth_provider,
    )


def build_public_sdk_gateway(
    client: PublicSdkClient,
    catalog_path: str | PathLike[str],
    *,
    poll_interval_s: float = 0.05,
    event_buffer_size: int = 2000,
) -> PublicSdkGateway:
    """Compose MCP tools and event projection around an initialized DSH SDK client.

    The caller retains ownership of the SDK client/runtime. Closing the returned
    gateway stops only its notification subscription.
    """
    bridge = PublicSdkBridge(
        client,
        SessionCatalog(catalog_path),
        poll_interval_s=poll_interval_s,
        event_buffer_size=event_buffer_size,
    )
    service = GatewayService(bridge.backend)
    return PublicSdkGateway(
        server=build_mcp_server(service),
        service=service,
        bridge=bridge,
    )
