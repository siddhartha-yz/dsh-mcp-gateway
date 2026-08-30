#!/usr/bin/env python3
"""One-shot public HTTPS OAuth -> MCP release smoke for dsh-mcp-gateway."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import ipaddress
import json
import secrets
import stat
import sys
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_PROTOCOL_VERSION = "2026-07-28"
EXPECTED_TOOLS = {
    "dsh_tool_catalog",
    "dsh_tool_call",
    "dsh_skill_catalog",
    "dsh_skill_load",
}


class SmokeError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SmokeError("response was not valid JSON") from exc
        if not isinstance(value, dict):
            raise SmokeError("response JSON was not an object")
        return value


class HttpClient:
    def __init__(self, *, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        self.opener = build_opener(NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        merged_headers = {
            "User-Agent": "Mozilla/5.0 dsh-mcp-gateway-release-smoke/1",
            **(headers or {}),
        }
        request = Request(url, data=body, headers=merged_headers, method=method)
        try:
            raw = self.opener.open(request, timeout=self.timeout_s)
        except HTTPError as exc:
            raw = exc
        except URLError as exc:
            raise SmokeError(f"transport failed for {urlparse(url).path or '/'}: {type(exc.reason).__name__}") from exc
        try:
            try:
                payload = raw.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, HTTPException, ValueError) as exc:
                raise SmokeError(
                    f"transport failed while reading response for {urlparse(url).path or '/'}: {type(exc).__name__}"
                ) from exc
            if len(payload) > MAX_RESPONSE_BYTES:
                raise SmokeError(f"response too large for {urlparse(url).path or '/'}")
            return HttpResponse(
                status=int(raw.status),
                headers={key.lower(): value for key, value in raw.headers.items()},
                body=payload,
            )
        finally:
            raw.close()

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> HttpResponse:
        return self.request("GET", url, headers=headers)

    def post_json(self, url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> HttpResponse:
        merged = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
        return self.request(
            "POST",
            url,
            headers=merged,
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )

    def post_form(self, url: str, payload: dict[str, str]) -> HttpResponse:
        return self.request(
            "POST",
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            body=urlencode(payload).encode("utf-8"),
        )


def require_status(response: HttpResponse, expected: int, label: str) -> None:
    if response.status != expected:
        raise SmokeError(f"{label} returned HTTP {response.status}; expected {expected}")


def required_string(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SmokeError(f"{label} did not return a non-empty {key}")
    return value


def normalize_origin(value: str, *, allow_http_loopback: bool) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SmokeError("base URL must be an absolute HTTP(S) origin")
    if parsed.username is not None or parsed.password is not None:
        raise SmokeError("base URL must not contain user info")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise SmokeError("base URL must be an origin without path, params, query, or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise SmokeError("base URL contains an invalid port") from exc
    if parsed.scheme != "https":
        loopback = parsed.hostname == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = False
        if not allow_http_loopback or not loopback:
            raise SmokeError("public release smoke requires HTTPS; HTTP is allowed only for explicit loopback tests")
    return value[:-1] if parsed.path == "/" else value


def read_env_value(path: Path, key: str) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SmokeError(f"cannot stat PIN file: {type(exc).__name__}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SmokeError("PIN file must be a single-link regular file")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077:
        raise SmokeError("PIN file must not be readable or writable by group/other")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SmokeError(f"cannot read PIN file: {type(exc).__name__}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value:
            break
        return value
    raise SmokeError(f"PIN file does not contain a non-empty {key}")


def load_pin(pin_file: Path | None) -> str:
    pin = read_env_value(pin_file, "DSH_MCP_GATEWAY_ADMIN_PIN") if pin_file else getpass.getpass("Gateway owner PIN: ")
    if len(pin) < 12:
        raise SmokeError("owner PIN is shorter than the gateway minimum")
    return pin


def same_origin(url: str, base_url: str) -> bool:
    target = urlparse(url)
    base = urlparse(base_url)
    try:
        return (
            target.scheme == base.scheme
            and target.hostname == base.hostname
            and (target.port or (443 if target.scheme == "https" else 80))
            == (base.port or (443 if base.scheme == "https" else 80))
        )
    except ValueError:
        return False


def parse_redirect(location: str, base_url: str) -> tuple[str, dict[str, list[str]]]:
    absolute = urljoin(f"{base_url}/", location)
    return absolute, parse_qs(urlparse(absolute).query)


def run_smoke(
    *,
    base_url: str,
    pin: str,
    redirect_uri: str,
    protocol_version: str,
    timeout_s: float,
) -> list[str]:
    client = HttpClient(timeout_s=timeout_s)
    evidence: list[str] = []
    resource_url = f"{base_url}/mcp"

    health = client.get(f"{base_url}/healthz")
    require_status(health, 200, "healthz")
    if health.json() != {"ok": True, "service": "dsh-mcp-gateway"}:
        raise SmokeError("healthz returned an unexpected payload")
    evidence.append("healthz=200")

    ready = client.get(f"{base_url}/readyz")
    require_status(ready, 200, "readyz")
    if ready.json() != {"ok": True, "dependency": "dsh-harness-bridge"}:
        raise SmokeError("readyz returned an unexpected payload")
    evidence.append("readyz=200")

    as_metadata_response = client.get(f"{base_url}/.well-known/oauth-authorization-server")
    require_status(as_metadata_response, 200, "authorization-server metadata")
    as_metadata = as_metadata_response.json()
    issuer = required_string(as_metadata, "issuer", "authorization-server metadata")
    if issuer.rstrip("/") != base_url:
        raise SmokeError("authorization-server issuer does not match the requested public origin")
    scopes = as_metadata.get("scopes_supported")
    if not isinstance(scopes, list) or "offline_access" not in scopes or "dsh:control" not in scopes:
        raise SmokeError("authorization-server metadata does not advertise required scopes")
    methods = as_metadata.get("token_endpoint_auth_methods_supported")
    if not isinstance(methods, list) or "none" not in methods:
        raise SmokeError("authorization-server metadata does not advertise public clients")
    authorization_endpoint = required_string(as_metadata, "authorization_endpoint", "authorization-server metadata")
    token_endpoint = required_string(as_metadata, "token_endpoint", "authorization-server metadata")
    registration_endpoint = required_string(as_metadata, "registration_endpoint", "authorization-server metadata")
    for name, endpoint in (
        ("authorization", authorization_endpoint),
        ("token", token_endpoint),
        ("registration", registration_endpoint),
    ):
        if not same_origin(endpoint, base_url):
            raise SmokeError(f"{name} endpoint is not on the declared public origin")
    evidence.append("oauth-metadata=ok")

    resource_metadata_response = client.get(f"{base_url}/.well-known/oauth-protected-resource/mcp")
    require_status(resource_metadata_response, 200, "protected-resource metadata")
    resource_metadata = resource_metadata_response.json()
    if resource_metadata.get("resource") != resource_url:
        raise SmokeError("protected-resource metadata resource does not match public MCP URL")
    resource_scopes = resource_metadata.get("scopes_supported")
    if resource_scopes != ["dsh:control"]:
        raise SmokeError("protected-resource metadata scopes are unexpected")
    evidence.append("resource-metadata=ok")

    registration = client.post_json(
        registration_endpoint,
        {
            "client_name": "dsh-mcp-gateway-release-smoke",
            "redirect_uris": [redirect_uri],
            "response_types": ["code"],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
            "scope": "dsh:control offline_access",
        },
    )
    require_status(registration, 201, "dynamic client registration")
    registered = registration.json()
    client_id = required_string(registered, "client_id", "dynamic client registration")
    if "client_secret" in registered:
        raise SmokeError("public client registration unexpectedly returned a client secret")
    evidence.append("dcr=201")

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(18)
    authorization_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "dsh:control offline_access",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource_url,
    }
    authorization_url = f"{authorization_endpoint}?{urlencode(authorization_params)}"
    authorization = client.get(authorization_url)
    require_status(authorization, 302, "authorization request")
    approval_location = authorization.headers.get("location")
    if not approval_location:
        raise SmokeError("authorization response did not provide approval location")
    approval_url, approval_query = parse_redirect(approval_location, base_url)
    if not same_origin(approval_url, base_url):
        raise SmokeError("authorization response moved owner approval off the declared public origin")
    request_ids = approval_query.get("request")
    if not request_ids or not request_ids[0]:
        raise SmokeError("authorization response did not provide an approval request id")
    evidence.append("authorize=302")

    approval = client.post_form(
        f"{base_url}/approve",
        {"request": request_ids[0], "pin": pin, "action": "approve"},
    )
    require_status(approval, 302, "owner approval")
    callback_location = approval.headers.get("location")
    if not callback_location:
        raise SmokeError("owner approval did not provide callback location")
    _callback_url, callback_query = parse_redirect(callback_location, base_url)
    if callback_query.get("state") != [state]:
        raise SmokeError("authorization callback state mismatch")
    codes = callback_query.get("code")
    if not codes or not codes[0]:
        raise SmokeError("authorization callback did not include a code")
    evidence.append("approval=302")

    token = client.post_form(
        token_endpoint,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": codes[0],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": resource_url,
        },
    )
    require_status(token, 200, "authorization-code token exchange")
    token_payload = token.json()
    access_token = required_string(token_payload, "access_token", "token exchange")
    refresh_token = required_string(token_payload, "refresh_token", "token exchange")
    if set(str(token_payload.get("scope", "")).split()) != {"dsh:control", "offline_access"}:
        raise SmokeError("token response scopes are unexpected")
    evidence.append("token=200")

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "dsh-release-smoke", "version": "1"},
        },
    }
    unauth = client.post_json(resource_url, initialize, headers={"Origin": base_url})
    require_status(unauth, 401, "unauthenticated MCP initialize")
    evidence.append("mcp-unauth=401")

    auth_headers = {"Authorization": f"Bearer {access_token}", "Origin": base_url}
    initialized = client.post_json(resource_url, initialize, headers=auth_headers)
    require_status(initialized, 200, "authenticated MCP initialize")
    initialized_payload = initialized.json()
    result = initialized_payload.get("result")
    if not isinstance(result, dict):
        raise SmokeError("MCP initialize did not return a result object")
    capabilities = result.get("capabilities")
    tools_capability = capabilities.get("tools") if isinstance(capabilities, dict) else None
    if not isinstance(tools_capability, dict) or tools_capability.get("listChanged") is not False:
        raise SmokeError("MCP initialize did not advertise the required meta-only tools capability")
    negotiated = required_string(result, "protocolVersion", "MCP initialize")
    mcp_session_id = initialized.headers.get("mcp-session-id")
    if not mcp_session_id:
        raise SmokeError("MCP initialize did not return mcp-session-id")
    evidence.append(f"mcp-initialize=200/{negotiated}")

    session_headers = {
        **auth_headers,
        "mcp-session-id": mcp_session_id,
        "mcp-protocol-version": negotiated,
    }
    notification = client.post_json(
        resource_url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=session_headers,
    )
    require_status(notification, 202, "MCP initialized notification")

    tools = client.post_json(
        resource_url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=session_headers,
    )
    require_status(tools, 200, "MCP tools/list")
    tools_result = tools.json().get("result")
    if not isinstance(tools_result, dict) or not isinstance(tools_result.get("tools"), list):
        raise SmokeError("MCP tools/list returned an unexpected payload")
    names = {tool.get("name") for tool in tools_result["tools"] if isinstance(tool, dict)}
    if names != EXPECTED_TOOLS:
        raise SmokeError(
            "MCP tool catalog does not match the exact four-tool meta-only surface: "
            f"expected={','.join(sorted(EXPECTED_TOOLS))} actual={','.join(sorted(str(name) for name in names))}"
        )
    evidence.append(f"tools={len(names)}")

    catalog_call = client.post_json(
        resource_url,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "dsh_tool_catalog", "arguments": {}},
        },
        headers=session_headers,
    )
    require_status(catalog_call, 200, "protected dsh_tool_catalog tool call")
    call_result = catalog_call.json().get("result")
    if not isinstance(call_result, dict) or call_result.get("isError") is True:
        raise SmokeError("protected dsh_tool_catalog tool call returned an MCP error")
    structured = call_result.get("structuredContent")
    catalog = structured.get("tools") if isinstance(structured, dict) else None
    if not isinstance(catalog, list) or not catalog:
        raise SmokeError("protected dsh_tool_catalog tool call did not return a non-empty DSH catalog")
    if structured.get("count") != len(catalog):
        raise SmokeError("protected dsh_tool_catalog count does not match its tool list")
    evidence.append(f"dsh_tool_catalog={len(catalog)}")

    rotated = client.post_form(
        token_endpoint,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": "dsh:control offline_access",
            "resource": resource_url,
        },
    )
    require_status(rotated, 200, "refresh-token rotation")
    rotated_payload = rotated.json()
    rotated_refresh = required_string(rotated_payload, "refresh_token", "refresh-token rotation")
    if rotated_refresh == refresh_token:
        raise SmokeError("refresh-token rotation returned the same refresh token")
    replay = client.post_form(
        token_endpoint,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": "dsh:control offline_access",
            "resource": resource_url,
        },
    )
    require_status(replay, 400, "old refresh-token replay")
    if replay.json().get("error") != "invalid_grant":
        raise SmokeError("old refresh-token replay did not return invalid_grant")
    evidence.append("refresh-rotation=single-use")
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a one-shot public OAuth -> MCP release smoke. The test registers one public OAuth client "
            "and leaves that registration in the gateway database."
        )
    )
    parser.add_argument("--base-url", required=True, help="Exact public gateway origin, normally https://...")
    parser.add_argument("--pin-file", type=Path, help="0600 env file containing DSH_MCP_GATEWAY_ADMIN_PIN; otherwise prompt securely.")
    parser.add_argument(
        "--redirect-uri",
        default="https://example.com/dsh-mcp-gateway-release-smoke",
        help="OAuth callback URI to register. The script does not follow the final callback redirect.",
    )
    parser.add_argument("--protocol-version", default=DEFAULT_PROTOCOL_VERSION)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--allow-http-loopback",
        action="store_true",
        help="Permit http://localhost/loopback only for local regression tests; never for a public release drill.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    try:
        base_url = normalize_origin(args.base_url, allow_http_loopback=args.allow_http_loopback)
        pin = load_pin(args.pin_file)
        evidence = run_smoke(
            base_url=base_url,
            pin=pin,
            redirect_uri=args.redirect_uri,
            protocol_version=args.protocol_version,
            timeout_s=args.timeout,
        )
    except SmokeError as exc:
        print(f"PUBLIC_SMOKE FAIL: {exc}", file=sys.stderr)
        return 1
    print("PUBLIC_SMOKE PASS")
    for item in evidence:
        print(f"  {item}")
    print("  note=one dynamic OAuth client registration remains persisted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
