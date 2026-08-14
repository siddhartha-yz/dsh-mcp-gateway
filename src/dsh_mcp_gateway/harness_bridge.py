from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class HarnessBridgeError(RuntimeError):
    """The local DSH ChatGPT bridge could not serve a capability request."""


@dataclass(slots=True)
class HarnessBridgeClient:
    """Thin HTTP client for the DSH-resident ToolRuntime bridge plugin.

    The bridge is intentionally loopback-only by default because DSH's Web Host
    is an internal runtime. Public OAuth terminates at this gateway instead.
    """

    base_url: str
    timeout_s: float = 30.0
    allow_non_loopback: bool = False

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute http(s) origin")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain user info")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an origin without path, query, or fragment")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
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

    def _request(self, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                body = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise HarnessBridgeError(f"DSH bridge HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HarnessBridgeError(f"DSH bridge unavailable: {exc}") from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HarnessBridgeError("DSH bridge returned non-JSON data") from exc
        if not isinstance(decoded, dict):
            raise HarnessBridgeError("DSH bridge returned a non-object response")
        return decoded

    def tools(self) -> list[dict[str, Any]]:
        payload = self._request("/api/chatgpt-bridge/tools")
        tools = payload.get("tools")
        if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
            raise HarnessBridgeError("DSH bridge returned an invalid tool catalog")
        return [dict(item) for item in tools]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("name must be non-empty")
        return self._request(
            "/api/chatgpt-bridge/call",
            payload={"name": name, "arguments": arguments or {}},
        )
