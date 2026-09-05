from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class HarnessBridgeError(RuntimeError):
    """The local DSH ChatGPT bridge could not serve a capability request."""


MAX_BRIDGE_REQUEST_BYTES = 1_000_000
MAX_BRIDGE_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TOOL_CALL_TRANSPORT_TIMEOUT_S = 125.0


def _http_error_detail(error: HTTPError, *, limit: int) -> str:
    """Return only the bridge's small structured public error surface."""
    try:
        try:
            raw = error.read(limit + 1)
        except (OSError, HTTPException, ValueError) as exc:
            return f"<error body unavailable: {type(exc).__name__}>"
        if len(raw) > limit:
            return "<error body too large>"
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "<non-JSON error body>"
        if not isinstance(decoded, dict):
            return "<invalid error body>"
        code = decoded.get("error")
        message = decoded.get("message")
        if not isinstance(code, str) or code not in {
            "bridge_error",
            "invalid_request",
            "method_not_allowed",
            "request_too_large",
            "skill_unavailable",
            "tool_unavailable",
        }:
            return "<unrecognized bridge error>"
        if code == "bridge_error":
            return code
        if isinstance(message, str) and 0 < len(message) <= 512:
            return f"{code}: {message}"
        return code
    finally:
        try:
            error.close()
        except (OSError, HTTPException, ValueError):
            pass


def _read_bounded(response, *, limit: int = MAX_BRIDGE_RESPONSE_BYTES) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise HarnessBridgeError(f"DSH bridge response exceeds {limit} bytes")
    return body


def _validated_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    name = schema.get("name")
    description = schema.get("description")
    parameters = schema.get("parameters")
    if not isinstance(name, str) or not name.strip():
        raise HarnessBridgeError("DSH bridge returned a tool without a valid name")
    if description is not None and not isinstance(description, str):
        raise HarnessBridgeError(f"DSH tool {name!r} returned a non-string description")
    if not isinstance(parameters, dict):
        raise HarnessBridgeError(f"DSH tool {name!r} returned a non-object parameter schema")
    return schema


def _projected_tool(schema: dict[str, Any]):
    """Convert one validated DSH ToolRuntime schema into an MCP first-class tool schema."""
    schema = _validated_tool_schema(schema)
    try:
        from mcp.types import Tool as MCPTool
    except ImportError as exc:  # pragma: no cover - optional server dependency boundary
        raise RuntimeError("MCP dependencies are unavailable; install dsh-mcp-gateway[server]") from exc

    name = schema["name"]
    description = schema.get("description")
    parameters = schema["parameters"]
    return MCPTool(
        name=name,
        description=description or "DSH Harness capability",
        input_schema=dict(parameters),
        meta={"dsh/projected": True},
    )


def _validated_skill(skill: dict[str, Any], *, require_content: bool) -> dict[str, Any]:
    for field in ("name", "source", "provider"):
        value = skill.get(field)
        if not isinstance(value, str) or not value.strip():
            raise HarnessBridgeError(f"DSH bridge returned a skill without a valid {field}")
    if not isinstance(skill.get("description"), str):
        raise HarnessBridgeError("DSH bridge returned a skill without a string description")
    if "whenToUse" in skill and not isinstance(skill["whenToUse"], str):
        raise HarnessBridgeError("DSH bridge returned a skill with invalid whenToUse")
    if "resourceBase" in skill and not isinstance(skill["resourceBase"], dict):
        raise HarnessBridgeError("DSH bridge returned a skill with invalid resourceBase")
    if require_content and not isinstance(skill.get("content"), str):
        raise HarnessBridgeError("DSH bridge returned an invalid skill definition")
    return skill


def _validated_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    is_error = result.get("isError")
    if not isinstance(is_error, bool):
        raise HarnessBridgeError("DSH bridge returned a tool result without a boolean isError")
    content = result.get("content")
    if not isinstance(content, list):
        raise HarnessBridgeError("DSH bridge returned a tool result without a content list")
    if "additionalContexts" in result and not isinstance(result["additionalContexts"], list):
        raise HarnessBridgeError("DSH bridge returned invalid additionalContexts")
    if is_error:
        error = result.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            raise HarnessBridgeError("DSH bridge returned an invalid failed tool result")
        if "value" in result:
            raise HarnessBridgeError("DSH bridge returned a failed tool result with a value")
    else:
        if "value" not in result:
            raise HarnessBridgeError("DSH bridge returned a successful tool result without a value")
        if "error" in result:
            raise HarnessBridgeError("DSH bridge returned a successful tool result with an error")
    return result


def tool_result_to_mcp(result: dict[str, Any]):
    """Translate a validated DSH ToolRuntime execution result into an MCP call result."""
    result = _validated_tool_result(result)
    try:
        from mcp.types import CallToolResult, ImageContent, TextContent
    except ImportError as exc:  # pragma: no cover - optional server dependency boundary
        raise RuntimeError("MCP dependencies are unavailable; install dsh-mcp-gateway[server]") from exc

    is_error = bool(result.get("isError"))
    content = []
    raw_content = result.get("content")
    if isinstance(raw_content, list):
        for block in raw_content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                content.append(TextContent(type="text", text=block["text"]))
                continue
            if (
                block.get("type") == "image"
                and isinstance(block.get("data"), str)
                and isinstance(block.get("mediaType"), str)
            ):
                content.append(
                    ImageContent(
                        type="image",
                        data=block["data"],
                        mime_type=block["mediaType"],
                    )
                )

    structured: dict[str, Any] = {}
    if "value" in result:
        structured["value"] = result["value"]
    if "meta" in result:
        structured["meta"] = result["meta"]
    additional_contexts = result.get("additionalContexts")
    if isinstance(additional_contexts, list):
        for item in additional_contexts:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            source_label = "DSH harness context"
            if isinstance(source, dict):
                kind = source.get("kind")
                plugin = source.get("plugin")
                if isinstance(kind, str) and kind:
                    source_label += f" from {kind}"
                if isinstance(plugin, str) and plugin:
                    source_label += f" {plugin}"
            blocks = item.get("content")
            if not isinstance(blocks, list):
                continue
            visible_blocks = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    visible_blocks.append(TextContent(type="text", text=block["text"]))
                    continue
                if (
                    block.get("type") == "image"
                    and isinstance(block.get("data"), str)
                    and isinstance(block.get("mediaType"), str)
                ):
                    visible_blocks.append(
                        ImageContent(
                            type="image",
                            data=block["data"],
                            mime_type=block["mediaType"],
                        )
                    )
            if visible_blocks:
                content.append(TextContent(type="text", text=f"[{source_label}]"))
                content.extend(visible_blocks)
    if is_error and isinstance(result.get("error"), dict):
        structured["error"] = result["error"]
        message = result["error"].get("message")
        if isinstance(message, str) and not content:
            content.append(TextContent(type="text", text=message))
    if not content:
        fallback = structured if structured else result
        content.append(
            TextContent(type="text", text=json.dumps(fallback, ensure_ascii=False, separators=(",", ":")))
        )

    return CallToolResult(
        content=content,
        structured_content=structured or None,
        is_error=is_error,
    )


async def watch_tool_catalog(
    bridge: HarnessBridgeClient,
    publish_changed: Callable[[], Awaitable[None]],
    *,
    interval_s: float = 2.0,
) -> None:
    """Poll the DSH registry revision and publish MCP tool-list invalidations.

    DSH owns the live tool registry and emits ``tools/change`` when community
    plugins register, unregister, or alter scoped restrictions. The loopback
    bridge exposes only the resulting monotonic process-local revision. This
    watcher keeps MCP's already-connected catalog coherent without copying the
    DSH registry into the gateway.
    """
    if interval_s <= 0:
        raise ValueError("interval_s must be positive")
    previous: tuple[str, int] | None = None
    while True:
        try:
            current = await asyncio.to_thread(bridge.tool_revision_token)
            if previous is not None and current != previous:
                await publish_changed()
            previous = current
        except HarnessBridgeError:
            # DSH readiness is already exposed separately. A transient bridge
            # outage must not kill the MCP server lifespan; the next successful
            # observation re-baselines or publishes if the process-local
            # revision differs from the last good value.
            pass
        await asyncio.sleep(interval_s)


class HarnessProjectionMixin:
    """Project DSH ToolRuntime schemas directly into the MCP tool surface.

    The catalog is read on every tools/list request from the DSH-side external
    capability projection. A newly approved DSH community tool can appear
    without adding a gateway wrapper or restarting the gateway. Eligibility
    remains owned by the DSH bridge and execution still goes through ToolRuntime.
    """

    _dsh_harness_bridge: HarnessBridgeClient | None = None

    async def list_tools(self):
        base_tools = await super().list_tools()
        bridge = self._dsh_harness_bridge
        if bridge is None:
            return base_tools
        schemas = await asyncio.to_thread(bridge.tools)
        reserved = {tool.name for tool in base_tools}
        projected = []
        for schema in schemas:
            tool = _projected_tool(schema)
            if tool.name in reserved:
                continue
            projected.append(tool)
        return [*base_tools, *projected]

    async def call_tool(self, name, arguments, context=None):
        bridge = self._dsh_harness_bridge
        if bridge is None or self._tool_manager.get_tool(name) is not None:
            return await super().call_tool(name, arguments, context)
        result = await asyncio.to_thread(bridge.call, name, arguments)
        return tool_result_to_mcp(result)


@dataclass(slots=True)
class HarnessBridgeClient:
    """Thin HTTP client for the DSH-resident ToolRuntime bridge plugin.

    The bridge is intentionally loopback-only by default because DSH's Web Host
    is an internal runtime. Public OAuth terminates at this gateway instead.
    """

    base_url: str
    timeout_s: float = 30.0
    allow_non_loopback: bool = False
    tool_call_timeout_s: float = DEFAULT_TOOL_CALL_TRANSPORT_TIMEOUT_S

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute http(s) origin")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain user info")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an origin without path, params, query, or fragment")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("base_url contains an invalid port") from exc
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.tool_call_timeout_s <= 0:
            raise ValueError("tool_call_timeout_s must be positive")
        if not self.allow_non_loopback and not self._is_loopback(parsed.hostname):
            raise ValueError("DSH harness bridge must use loopback unless explicitly allowed")
        self.base_url = self.base_url.rstrip("/")

    @staticmethod
    def _is_loopback(host: str) -> bool:
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _request(
        self,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if data is not None and len(data) > MAX_BRIDGE_REQUEST_BYTES:
            raise HarnessBridgeError(f"DSH bridge request exceeds {MAX_BRIDGE_REQUEST_BYTES} bytes")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("timeout_s must be positive when supplied")
        effective_timeout = self.timeout_s if timeout_s is None else timeout_s
        try:
            with urlopen(request, timeout=effective_timeout) as response:
                body = _read_bounded(response)
        except HTTPError as exc:
            detail = _http_error_detail(exc, limit=2000)
            raise HarnessBridgeError(f"DSH bridge HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, HTTPException, OSError) as exc:
            raise HarnessBridgeError(f"DSH bridge unavailable: {exc}") from exc
        try:
            decoded = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HarnessBridgeError("DSH bridge returned non-JSON data") from exc
        if not isinstance(decoded, dict):
            raise HarnessBridgeError("DSH bridge returned a non-object response")
        return decoded

    def _revision_payload(self) -> dict[str, Any]:
        return self._request("/api/chatgpt-bridge/revision")

    @staticmethod
    def _validated_tool_revision(payload: dict[str, Any]) -> int:
        revision = payload.get("toolRevision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise HarnessBridgeError("DSH bridge returned an invalid tool revision")
        return revision

    def tool_revision(self) -> int:
        """Return the process-local numeric tool revision for diagnostics."""
        return self._validated_tool_revision(self._revision_payload())

    def tool_revision_token(self) -> tuple[str, int]:
        """Return a restart-safe token for projected MCP catalog invalidation."""
        payload = self._revision_payload()
        instance_id = payload.get("instanceId")
        if not isinstance(instance_id, str) or not instance_id:
            raise HarnessBridgeError("DSH bridge returned an invalid instance id")
        return instance_id, self._validated_tool_revision(payload)

    def tools(self, *, timeout_s: float | None = None) -> list[dict[str, Any]]:
        payload = self._request("/api/chatgpt-bridge/tools", timeout_s=timeout_s)
        tools = payload.get("tools")
        if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
            raise HarnessBridgeError("DSH bridge returned an invalid tool catalog")
        return [dict(_validated_tool_schema(item)) for item in tools]

    def skills(self) -> list[dict[str, Any]]:
        payload = self._request("/api/chatgpt-bridge/skills")
        skills = payload.get("skills")
        if not isinstance(skills, list) or not all(isinstance(item, dict) for item in skills):
            raise HarnessBridgeError("DSH bridge returned an invalid skill catalog")
        return [dict(_validated_skill(item, require_content=False)) for item in skills]

    def load_skill(self, name: str) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("name must be non-empty")
        payload = self._request("/api/chatgpt-bridge/skill", payload={"name": name})
        skill = payload.get("skill")
        if not isinstance(skill, dict):
            raise HarnessBridgeError("DSH bridge returned an invalid skill definition")
        return dict(_validated_skill(skill, require_content=True))

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("name must be non-empty")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("arguments must be an object when supplied")
        return _validated_tool_result(
            self._request(
                "/api/chatgpt-bridge/call",
                payload={"name": name, "arguments": arguments or {}},
                timeout_s=self.tool_call_timeout_s,
            )
        )
