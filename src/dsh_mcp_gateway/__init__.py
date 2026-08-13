"""dsh-mcp-gateway package."""

from __future__ import annotations

from typing import Any

from .routing import GatewayService

__version__ = "0.0.1.dev0"


def build_mcp_server(service: GatewayService) -> Any:
    """Build the MCP v2 tool surface around an injected gateway service."""
    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - installation boundary
        raise RuntimeError("MCP dependencies are unavailable; install dsh-mcp-gateway[server]") from exc

    mcp = MCPServer(
        "dsh-mcp-gateway",
        description="Control long-lived DeepSeek Harness agent sessions.",
        instructions=(
            "Use dsh_start for a new task and keep its session_id. "
            "Use dsh_status or dsh_history to observe it, then dsh_continue to steer it later."
        ),
    )

    @mcp.tool(name="dsh_start")
    def dsh_start(prompt: str, session_id: str | None = None) -> dict[str, str]:
        """Start work in a new or explicitly named DSH session and return a receipt."""
        return service.start(prompt, session_id=session_id).as_dict()

    @mcp.tool(name="dsh_continue")
    def dsh_continue(session_id: str, prompt: str) -> dict[str, str]:
        """Send another instruction to the same durable DSH session."""
        return service.continue_session(session_id, prompt).as_dict()

    @mcp.tool(name="dsh_status")
    def dsh_status(session_id: str) -> dict[str, Any]:
        """Read the current state of one DSH session."""
        return service.status(session_id)

    @mcp.tool(name="dsh_history")
    def dsh_history(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Read the newest durable or projected events for one DSH session."""
        return service.history(session_id, limit=limit)

    @mcp.tool(name="dsh_list")
    def dsh_list() -> list[dict[str, Any]]:
        """List sessions visible to the configured DSH backend."""
        return service.list_sessions()

    @mcp.tool(name="dsh_cancel")
    def dsh_cancel(session_id: str) -> dict[str, Any]:
        """Cancel active work in a DSH session without replacing that session."""
        return service.cancel(session_id)

    return mcp
