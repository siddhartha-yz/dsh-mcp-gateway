from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

MCP_PRIVATE_API_VERSION = "2.0.0"


def disable_modern_subscriptions(server: Any) -> None:
    """Disable MCP 2026-07-28 subscriptions for the fixed meta-only surface.

    MCP 2.0.0 has no public constructor switch for omitting
    ``subscriptions/listen``: ``MCPServer`` always installs a listen handler.
    The modern handshake derives ``tools.listChanged`` from the presence of
    that handler, so meta-only mode must remove it to keep the public tool
    surface explicitly non-dynamic.

    This is the only place allowed to depend on the pinned MCP 2.0.0 private
    handler table. Fail closed if that compatibility seam changes.
    """
    try:
        installed_version = version("mcp")
    except PackageNotFoundError as exc:  # pragma: no cover - server import fails first in normal use.
        raise RuntimeError("MCP SDK package metadata is unavailable") from exc
    if installed_version != MCP_PRIVATE_API_VERSION:
        raise RuntimeError(
            "meta-only MCP compatibility seam supports "
            f"mcp=={MCP_PRIVATE_API_VERSION}, found {installed_version}"
        )

    lowlevel = getattr(server, "_lowlevel_server", None)
    get_handler = getattr(lowlevel, "get_request_handler", None)
    handlers = getattr(lowlevel, "_request_handlers", None)
    if not callable(get_handler) or not isinstance(handlers, dict):
        raise RuntimeError(  # noqa: TRY004 - incompatible SDK internals, not caller input.
            "MCP SDK subscription internals changed; cannot guarantee meta-only tool surface"
        )
    if get_handler("subscriptions/listen") is None:
        raise RuntimeError("MCP SDK subscription internals changed; cannot guarantee meta-only tool surface")
    try:
        del handlers["subscriptions/listen"]
    except KeyError as exc:
        raise RuntimeError(
            "MCP SDK subscription internals changed; cannot guarantee meta-only tool surface"
        ) from exc
    if get_handler("subscriptions/listen") is not None:
        raise RuntimeError("failed to disable MCP subscriptions for meta-only tool surface")
