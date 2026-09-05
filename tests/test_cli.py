from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dsh_mcp_gateway import build_mcp_server
from dsh_mcp_gateway.cli import build_transport_security, install_health_routes, main


class CliTests(unittest.TestCase):
    def harness_args(self, *extra: str) -> list[str]:
        return [
            "--public-base-url",
            "https://gateway.example.com",
            "--dsh-harness-url",
            "http://127.0.0.1:3080",
            *extra,
        ]

    def test_rejects_non_https_public_origin(self) -> None:
        with self.assertRaisesRegex(SystemExit, "https:// origin"):
            main([
                "--public-base-url",
                "http://gateway.example.com",
                "--dsh-harness-url",
                "http://127.0.0.1:3080",
            ])

    def test_rejects_public_origin_with_path(self) -> None:
        with self.assertRaisesRegex(SystemExit, "origin without a path"):
            main([
                "--public-base-url",
                "https://gateway.example.com/prefix",
                "--dsh-harness-url",
                "http://127.0.0.1:3080",
            ])

    def test_rejects_public_origin_with_user_info(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must not contain user info"):
            main([
                "--public-base-url",
                "https://user:pass@gateway.example.com",
                "--dsh-harness-url",
                "http://127.0.0.1:3080",
            ])

    def test_rejects_public_origin_with_invalid_port_as_operator_error(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must contain a valid port"):
            main([
                "--public-base-url",
                "https://gateway.example.com:notaport",
                "--dsh-harness-url",
                "http://127.0.0.1:3080",
            ])

    def test_transport_security_rejects_non_origin_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "without path, params"):
            build_transport_security("https://gateway.example.com/;tenant=bad")

    def test_transport_security_accepts_public_origin(self) -> None:
        security = build_transport_security("https://Gateway.Example.com:443/")
        self.assertIn("Gateway.Example.com:443", security.allowed_hosts)
        self.assertIn("gateway.example.com", security.allowed_hosts)
        self.assertIn("https://gateway.example.com", security.allowed_origins)

    def test_non_loopback_bind_requires_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must be loopback"):
            main(self.harness_args("--bind-host", "0.0.0.0"))

    def test_admin_pin_must_come_from_environment(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(SystemExit, "DSH_MCP_GATEWAY_ADMIN_PIN is required"),
        ):
            main(self.harness_args())

    def test_harness_url_is_required(self) -> None:
        with (
            patch.dict(os.environ, {"DSH_MCP_GATEWAY_ADMIN_PIN": "test-admin-pin"}, clear=False),
            self.assertRaisesRegex(SystemExit, "--dsh-harness-url is required"),
        ):
            main(["--public-base-url", "https://gateway.example.com"])

    def test_rejects_invalid_harness_origin(self) -> None:
        with (
            patch.dict(os.environ, {"DSH_MCP_GATEWAY_ADMIN_PIN": "test-admin-pin"}, clear=False),
            self.assertRaisesRegex(SystemExit, "invalid --dsh-harness-url"),
        ):
            main(self.harness_args("--dsh-harness-url", "http://127.0.0.1:3080/api"))

    def test_public_transport_security_accepts_declared_origin_and_rejects_mismatch(self) -> None:
        from starlette.testclient import TestClient

        security = build_transport_security("https://gateway.example.com")
        server = build_mcp_server(None)
        app = server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            host="127.0.0.1",
            transport_security=security,
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "transport-security-test", "version": "1"},
            },
        }
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        with TestClient(app, base_url="https://gateway.example.com") as client:
            accepted = client.post(
                "/mcp",
                headers={**headers, "Origin": "https://gateway.example.com"},
                json=initialize,
            )
            bad_host = client.post(
                "/mcp",
                headers={
                    **headers,
                    "Host": "evil.example.com",
                    "Origin": "https://gateway.example.com",
                },
                json=initialize,
            )
            bad_origin = client.post(
                "/mcp",
                headers={**headers, "Origin": "https://evil.example.com"},
                json=initialize,
            )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(bad_host.status_code, 421)
        self.assertEqual(bad_origin.status_code, 403)

    def test_harness_mode_wires_generic_bridge_and_oauth(self) -> None:
        fake_bridge = Mock()
        fake_bridge.base_url = "http://127.0.0.1:3080"
        fake_server = Mock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DSH_MCP_GATEWAY_ADMIN_PIN": "test-admin-pin"}, clear=False),
            patch("dsh_mcp_gateway.cli.HarnessBridgeClient", return_value=fake_bridge) as bridge_cls,
            patch(
                "dsh_mcp_gateway.cli.build_embedded_oauth_server",
                return_value=(fake_server, Mock()),
            ) as build_server,
        ):
            result = main(self.harness_args("--state-dir", tmp, "--port", "9876"))

        self.assertEqual(result, 0)
        bridge_cls.assert_called_once_with("http://127.0.0.1:3080")
        self.assertIs(build_server.call_args.args[0], fake_bridge)
        config = build_server.call_args.args[1]
        self.assertEqual(config.issuer_url, "https://gateway.example.com/")
        self.assertEqual(config.resource_url, "https://gateway.example.com/mcp")
        self.assertEqual(config.state_db, Path(tmp).absolute() / "oauth.sqlite3")
        self.assertFalse(build_server.call_args.kwargs["project_dsh_tools"])
        fake_server.run.assert_called_once()
        self.assertEqual(fake_server.run.call_args.kwargs["port"], 9876)

    def test_projected_tool_surface_is_explicit_opt_in(self) -> None:
        fake_bridge = Mock()
        fake_bridge.base_url = "http://127.0.0.1:3080"
        fake_server = Mock()
        with (
            patch.dict(os.environ, {"DSH_MCP_GATEWAY_ADMIN_PIN": "test-admin-pin"}, clear=False),
            patch("dsh_mcp_gateway.cli.HarnessBridgeClient", return_value=fake_bridge),
            patch(
                "dsh_mcp_gateway.cli.build_embedded_oauth_server",
                return_value=(fake_server, Mock()),
            ) as build_server,
        ):
            result = main(self.harness_args("--tool-surface", "projected"))

        self.assertEqual(result, 0)
        self.assertTrue(build_server.call_args.kwargs["project_dsh_tools"])


class HealthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_harness_bridge_readiness_checks_dsh_catalog(self) -> None:
        from mcp.server import MCPServer

        server = MCPServer("health-test")
        bridge = Mock()
        bridge.tools.return_value = [{"name": "echo"}]
        install_health_routes(server, bridge)
        routes = {route.path: route for route in server._custom_starlette_routes}

        health = await routes["/healthz"].endpoint(Mock())
        ready = await routes["/readyz"].endpoint(Mock())

        bridge.tools.assert_called_once_with(timeout_s=1.0)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(json.loads(health.body), {"ok": True, "service": "dsh-mcp-gateway"})
        self.assertEqual(health.headers["cache-control"], "no-store")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(json.loads(ready.body), {"ok": True, "dependency": "dsh-harness-bridge"})

    async def test_ready_route_collapses_bridge_errors_to_503(self) -> None:
        from mcp.server import MCPServer

        server = MCPServer("health-test")
        bridge = Mock()
        bridge.tools.side_effect = RuntimeError("private transport detail")
        install_health_routes(server, bridge)
        routes = {route.path: route for route in server._custom_starlette_routes}

        ready = await routes["/readyz"].endpoint(Mock())
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(json.loads(ready.body), {"ok": False, "dependency": "dsh-harness-bridge"})
        self.assertNotIn("private transport detail", ready.body.decode())


if __name__ == "__main__":
    unittest.main()
