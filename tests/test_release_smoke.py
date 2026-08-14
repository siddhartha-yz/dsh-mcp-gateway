from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "scripts" / "smoke-public-oauth.py"

spec = importlib.util.spec_from_file_location("dsh_public_smoke", SMOKE_SCRIPT)
assert spec is not None and spec.loader is not None
smoke_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke_module
spec.loader.exec_module(smoke_module)
EXPECTED_TOOLS = sorted(smoke_module.EXPECTED_TOOLS)


class ReleaseSmokeHandler(BaseHTTPRequestHandler):
    server_version = "dsh-release-smoke-fixture"

    @property
    def state(self):
        return self.server.state

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def log_message(self, _format: str, *_args) -> None:
        return

    def read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def send_json(self, status: int, payload: dict, *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_json(200, {"ok": True, "service": "dsh-mcp-gateway"})
            return
        if parsed.path == "/readyz":
            self.send_json(200, {"ok": True, "dependency": "dsh-web-host"})
            return
        if parsed.path == "/.well-known/oauth-authorization-server":
            self.send_json(
                200,
                {
                    "issuer": f"{self.base_url}/",
                    "authorization_endpoint": f"{self.base_url}/authorize",
                    "token_endpoint": f"{self.base_url}/token",
                    "registration_endpoint": f"{self.base_url}/register",
                    "scopes_supported": ["dsh:control", "offline_access"],
                    "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
                },
            )
            return
        if parsed.path == "/.well-known/oauth-protected-resource/mcp":
            self.send_json(
                200,
                {"resource": f"{self.base_url}/mcp", "scopes_supported": ["dsh:control"]},
            )
            return
        if parsed.path == "/authorize":
            query = parse_qs(parsed.query)
            self.state["authorize"] = query
            if query.get("client_id") != ["smoke-client"] or query.get("code_challenge_method") != ["S256"]:
                self.send_json(400, {"error": "invalid_request"})
                return
            self.send_redirect(f"{self.base_url}/approve?request=approval-1")
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self.read_body()
        if parsed.path == "/register":
            payload = json.loads(body)
            self.state["registration"] = payload
            self.state["redirect_uri"] = payload["redirect_uris"][0]
            self.send_json(
                201,
                {
                    **payload,
                    "client_id": "smoke-client",
                    "client_id_issued_at": 1,
                },
            )
            return
        if parsed.path == "/approve":
            form = parse_qs(body.decode("utf-8"))
            self.state["approval"] = form
            if form.get("request") != ["approval-1"] or form.get("pin") != [self.state["pin"]]:
                self.send_json(403, {"error": "access_denied"})
                return
            authorize = self.state["authorize"]
            callback_query = {
                "code": "code-1",
                "state": authorize["state"][0],
                "iss": f"{self.base_url}/",
            }
            callback = f"{self.state['redirect_uri']}?{urlencode(callback_query)}"
            self.send_redirect(callback)
            return
        if parsed.path == "/token":
            form = parse_qs(body.decode("utf-8"))
            grant_type = form.get("grant_type", [""])[0]
            if grant_type == "authorization_code":
                authorize = self.state["authorize"]
                verifier = form.get("code_verifier", [""])[0]
                actual_challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
                    .rstrip(b"=")
                    .decode("ascii")
                )
                if (
                    form.get("code") != ["code-1"]
                    or actual_challenge != authorize["code_challenge"][0]
                    or form.get("resource") != [f"{self.base_url}/mcp"]
                ):
                    self.send_json(400, {"error": "invalid_grant"})
                    return
                self.state["token_exchange"] = True
                self.send_json(
                    200,
                    {
                        "access_token": self.state["access_token"],
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "refresh_token": self.state["refresh_token"],
                        "scope": "dsh:control offline_access",
                    },
                )
                return
            if grant_type == "refresh_token":
                presented = form.get("refresh_token", [""])[0]
                if presented == self.state["refresh_token"] and not self.state.get("refresh_used"):
                    self.state["refresh_used"] = True
                    self.send_json(
                        200,
                        {
                            "access_token": "rotated-access-marker",
                            "token_type": "Bearer",
                            "expires_in": 3600,
                            "refresh_token": "rotated-refresh-marker",
                            "scope": "dsh:control offline_access",
                        },
                    )
                    return
                self.state["refresh_replay"] = True
                self.send_json(400, {"error": "invalid_grant"})
                return
            self.send_json(400, {"error": "unsupported_grant_type"})
            return
        if parsed.path == "/mcp":
            if self.headers.get("Authorization") != f"Bearer {self.state['access_token']}":
                self.send_json(401, {"error": "invalid_token"})
                return
            payload = json.loads(body)
            method = payload.get("method")
            if method == "initialize":
                self.state["initialized"] = True
                self.state["requested_protocol"] = payload.get("params", {}).get("protocolVersion")
                self.send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "dsh-mcp-gateway", "version": "test"},
                        },
                    },
                    headers={"mcp-session-id": "mcp-session-1"},
                )
                return
            if (
                self.headers.get("mcp-session-id") != "mcp-session-1"
                or self.headers.get("mcp-protocol-version") != "2025-11-25"
            ):
                self.send_json(400, {"error": "bad_session_headers"})
                return
            if method == "notifications/initialized":
                self.state["initialized_notification"] = True
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "tools/list":
                self.state["tools_list"] = True
                self.send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"tools": [{"name": name} for name in EXPECTED_TOOLS]},
                    },
                )
                return
            if method == "tools/call" and payload.get("params", {}).get("name") == "dsh_list":
                self.state["dsh_list"] = True
                self.send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "content": [],
                            "structuredContent": {
                                "items": [],
                                "total": 0,
                                "has_more": False,
                                "next_offset": None,
                            },
                            "isError": False,
                        },
                    },
                )
                return
            self.send_json(400, {"error": "unknown_mcp_request"})
            return
        self.send_json(404, {"error": "not_found"})


class PublicReleaseSmokeTests(unittest.TestCase):
    @staticmethod
    def secret_marker(kind: str) -> str:
        return f"fixture-{kind}-never-print"

    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ReleaseSmokeHandler)
        self.server.state = {
            "pin": self.secret_marker("pin"),
            "access_token": self.secret_marker("access"),
            "refresh_token": self.secret_marker("refresh"),
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_public_smoke_walks_oauth_mcp_and_refresh_without_printing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pin_file = Path(tmp) / "gateway.env"
            pin_file.write_text(
                f"DSH_MCP_GATEWAY_ADMIN_PIN={self.server.state['pin']}\n",
                encoding="utf-8",
            )
            pin_file.chmod(0o600)
            host, port = self.server.server_address[:2]
            base_url = f"http://{host}:{port}"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SMOKE_SCRIPT),
                    "--base-url",
                    base_url,
                    "--allow-http-loopback",
                    "--pin-file",
                    str(pin_file),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PUBLIC_SMOKE PASS", result.stdout)
            self.assertIn("mcp-initialize=200/2025-11-25", result.stdout)
            for marker in (
                self.server.state["pin"],
                self.server.state["access_token"],
                self.server.state["refresh_token"],
                "rotated-access-marker",
                "rotated-refresh-marker",
            ):
                self.assertNotIn(marker, result.stdout + result.stderr)
            for key in (
                "registration",
                "authorize",
                "approval",
                "token_exchange",
                "initialized",
                "initialized_notification",
                "tools_list",
                "dsh_list",
                "refresh_used",
                "refresh_replay",
            ):
                self.assertTrue(self.server.state.get(key), key)
            self.assertEqual(self.server.state["requested_protocol"], "2026-07-28")

    def test_public_smoke_rejects_non_https_non_loopback_before_reading_pin(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SMOKE_SCRIPT),
                "--base-url",
                "http://example.com",
                "--allow-http-loopback",
                "--pin-file",
                "/definitely/not/read.env",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("PUBLIC_SMOKE FAIL", result.stderr)
        self.assertIn("HTTPS", result.stderr)
        self.assertNotIn("cannot stat PIN file", result.stderr)


if __name__ == "__main__":
    unittest.main()
