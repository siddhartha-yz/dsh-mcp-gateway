from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dsh_mcp_gateway import build_mcp_server
from dsh_mcp_gateway.cli import build_transport_security, install_health_routes, main
from dsh_mcp_gateway.routing import GatewayService


class CliTests(unittest.TestCase):
    def test_rejects_non_https_public_origin(self) -> None:
        with self.assertRaisesRegex(SystemExit, "https:// origin"):
            main(["--public-base-url", "http://gateway.example.com"])

    def test_rejects_public_origin_with_path(self) -> None:
        with self.assertRaisesRegex(SystemExit, "origin without a path"):
            main(["--public-base-url", "https://gateway.example.com/prefix"])

    def test_rejects_public_origin_with_user_info(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must not contain user info"):
            main(["--public-base-url", "https://user:pass@gateway.example.com"])

    def test_rejects_public_origin_with_invalid_port_as_operator_error(self) -> None:
        for public_base in ("https://gateway.example.com:notaport", "https://gateway.example.com:99999"):
            with self.subTest(public_base=public_base), self.assertRaisesRegex(
                SystemExit,
                "must contain a valid port",
            ):
                main(["--public-base-url", public_base])

    def test_transport_security_rejects_invalid_port_cleanly(self) -> None:
        with self.assertRaisesRegex(ValueError, "public_base must contain a valid port"):
            build_transport_security("https://gateway.example.com:notaport")

    def test_non_loopback_bind_requires_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must be loopback"):
            main(
                [
                    "--public-base-url",
                    "https://gateway.example.com",
                    "--bind-host",
                    "0.0.0.0",
                ]
            )

    def test_admin_pin_must_come_from_environment(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(SystemExit, "DSH_MCP_GATEWAY_ADMIN_PIN is required"),
        ):
            main(["--public-base-url", "https://gateway.example.com"])

    def test_rejects_dsh_web_url_with_path_as_operator_error(self) -> None:
        with (
            patch.dict(os.environ, {"DSH_MCP_GATEWAY_ADMIN_PIN": "test-admin-pin"}, clear=False),
            self.assertRaisesRegex(SystemExit, "invalid --dsh-web-url: .*origin without a path"),
        ):
            main(
                [
                    "--public-base-url",
                    "https://gateway.example.com",
                    "--dsh-web-url",
                    "http://127.0.0.1:3080/api",
                ]
            )

    def test_rejects_non_positive_dynamic_client_capacity(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must be positive"):
            main(
                [
                    "--public-base-url",
                    "https://gateway.example.com",
                    "--max-registered-clients",
                    "0",
                ]
            )

    def test_rejects_non_positive_dynamic_client_metadata_budget(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--max-client-metadata-bytes must be positive"):
            main(
                [
                    "--public-base-url",
                    "https://gateway.example.com",
                    "--max-client-metadata-bytes",
                    "0",
                ]
            )

    def test_rejects_non_positive_dynamic_registration_request_budget(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--max-registration-request-bytes must be positive"):
            main(
                [
                    "--public-base-url",
                    "https://gateway.example.com",
                    "--max-registration-request-bytes",
                    "0",
                ]
            )

    def test_public_transport_security_accepts_declared_origin_and_rejects_mismatch(self) -> None:
        from starlette.testclient import TestClient

        security = build_transport_security("https://gateway.example.com")
        server = build_mcp_server(GatewayService(Mock()))
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
        self.assertEqual(bad_host.text, "Invalid Host header")
        self.assertEqual(bad_origin.status_code, 403)
        self.assertEqual(bad_origin.text, "Invalid Origin header")

    def test_happy_path_builds_runtime_only_oauth_gateway_with_canonical_issuer(self) -> None:
        fake_server = Mock()

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DSH_MCP_GATEWAY_ADMIN_PIN": "test-admin-pin"}, clear=False),
            patch(
                "dsh_mcp_gateway.cli.build_embedded_oauth_server",
                return_value=(fake_server, Mock()),
            ) as build_server,
        ):
            result = main(
                [
                    "--public-base-url",
                    "https://gateway.example.com",
                    "--state-dir",
                    tmp,
                    "--port",
                    "9876",
                ]
            )

        self.assertEqual(result, 0)
        self.assertIsNone(build_server.call_args.args[0])
        config = build_server.call_args.args[1]
        session_runtime = build_server.call_args.kwargs["session_runtime"]
        self.assertEqual(session_runtime.database, Path(tmp).resolve() / "sessions.sqlite3")
        self.assertEqual(config.issuer_url, "https://gateway.example.com/")
        self.assertEqual(config.resource_url, "https://gateway.example.com/mcp")
        self.assertEqual(config.state_db, Path(tmp).resolve() / "oauth.sqlite3")
        self.assertEqual(config.max_registered_clients, 256)
        self.assertEqual(config.max_client_metadata_bytes, 32 * 1024)
        self.assertEqual(config.max_registration_request_bytes, 64 * 1024)
        fake_server.run.assert_called_once()
        run_kwargs = fake_server.run.call_args.kwargs
        self.assertEqual(run_kwargs["transport"], "streamable-http")
        self.assertEqual(run_kwargs["host"], "127.0.0.1")
        self.assertEqual(run_kwargs["port"], 9876)
        self.assertEqual(run_kwargs["streamable_http_path"], "/mcp")
        self.assertTrue(run_kwargs["json_response"])
        security = run_kwargs["transport_security"]
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertIn("gateway.example.com", security.allowed_hosts)
        self.assertIn("gateway.example.com:443", security.allowed_hosts)
        self.assertIn("127.0.0.1:*", security.allowed_hosts)
        self.assertIn("https://gateway.example.com", security.allowed_origins)

    def test_primary_harness_mode_wires_generic_bridge_without_gateway_session_runtime(self) -> None:
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
            result = main(
                [
                    "--public-base-url",
                    "https://gateway.example.com",
                    "--state-dir",
                    tmp,
                    "--dsh-harness-url",
                    "http://127.0.0.1:3080",
                ]
            )
        self.assertEqual(result, 0)
        bridge_cls.assert_called_once_with("http://127.0.0.1:3080")
        self.assertIsNone(build_server.call_args.kwargs["session_runtime"])
        self.assertIs(build_server.call_args.kwargs["harness_bridge"], fake_bridge)
        self.assertFalse(build_server.call_args.kwargs["project_dsh_tools"])

    def test_projected_tool_surface_is_explicit_opt_in(self) -> None:
        fake_bridge = Mock()
        fake_bridge.base_url = "http://127.0.0.1:3080"
        fake_server = Mock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DSH_MCP_GATEWAY_ADMIN_PIN": "test-admin-pin"}, clear=False),
            patch("dsh_mcp_gateway.cli.HarnessBridgeClient", return_value=fake_bridge),
            patch(
                "dsh_mcp_gateway.cli.build_embedded_oauth_server",
                return_value=(fake_server, Mock()),
            ) as build_server,
        ):
            result = main(
                [
                    "--public-base-url",
                    "https://gateway.example.com",
                    "--state-dir",
                    tmp,
                    "--dsh-harness-url",
                    "http://127.0.0.1:3080",
                    "--tool-surface",
                    "projected",
                ]
            )
        self.assertEqual(result, 0)
        self.assertTrue(build_server.call_args.kwargs["project_dsh_tools"])

    def test_legacy_dsh_web_host_is_explicit_opt_in(self) -> None:
        fake_backend = Mock()
        fake_backend.base_url = "http://127.0.0.1:3080"
        fake_server = Mock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DSH_MCP_GATEWAY_ADMIN_PIN": "test-admin-pin"}, clear=False),
            patch("dsh_mcp_gateway.cli.ExperimentalWebHostBackend", return_value=fake_backend) as backend_cls,
            patch(
                "dsh_mcp_gateway.cli.build_embedded_oauth_server",
                return_value=(fake_server, Mock()),
            ) as build_server,
        ):
            result = main(
                [
                    "--public-base-url",
                    "https://gateway.example.com",
                    "--state-dir",
                    tmp,
                    "--dsh-web-url",
                    "http://127.0.0.1:3080",
                ]
            )
        self.assertEqual(result, 0)
        backend_cls.assert_called_once_with("http://127.0.0.1:3080", cwd=os.getcwd())
        service = build_server.call_args.args[0]
        self.assertIsNotNone(service)
        self.assertIs(service._backend, fake_backend)


class HealthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_only_readiness_has_no_model_dependency(self) -> None:
        from mcp.server import MCPServer

        server = MCPServer("health-test")
        install_health_routes(server)
        routes = {route.path: route for route in server._custom_starlette_routes}
        ready = await routes["/readyz"].endpoint(Mock())
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(json.loads(ready.body), {"ok": True, "dependency": "runtime-state"})

    async def test_harness_bridge_readiness_checks_dsh_catalog(self) -> None:
        from mcp.server import MCPServer

        server = MCPServer("health-test")
        bridge = Mock()
        bridge.tools.return_value = [{"name": "echo"}]
        install_health_routes(server, harness_bridge=bridge)
        routes = {route.path: route for route in server._custom_starlette_routes}
        ready = await routes["/readyz"].endpoint(Mock())
        bridge.tools.assert_called_once_with(timeout_s=1.0)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(json.loads(ready.body), {"ok": True, "dependency": "dsh-harness-bridge"})

    async def test_health_and_ready_routes_are_minimal_and_no_store(self) -> None:
        from mcp.server import MCPServer

        server = MCPServer("health-test")
        backend = Mock()
        backend.describe_host.return_value = {"cwd": "/secret/workspace", "provider": "secret-provider"}
        install_health_routes(server, backend)
        routes = {route.path: route for route in server._custom_starlette_routes}

        health = await routes["/healthz"].endpoint(Mock())
        self.assertEqual(health.status_code, 200)
        self.assertEqual(json.loads(health.body), {"ok": True, "service": "dsh-mcp-gateway"})
        self.assertEqual(health.headers["cache-control"], "no-store")

        ready = await routes["/readyz"].endpoint(Mock())
        backend.describe_host.assert_called_once_with(timeout_s=1.0)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(json.loads(ready.body), {"ok": True, "dependency": "dsh-web-host"})
        self.assertNotIn("secret", ready.body.decode())
        self.assertEqual(ready.headers["cache-control"], "no-store")

    async def test_ready_route_collapses_dependency_errors_to_503(self) -> None:
        from mcp.server import MCPServer

        server = MCPServer("health-test")
        backend = Mock()
        backend.describe_host.side_effect = RuntimeError("private transport detail")
        install_health_routes(server, backend)
        routes = {route.path: route for route in server._custom_starlette_routes}

        ready = await routes["/readyz"].endpoint(Mock())
        backend.describe_host.assert_called_once_with(timeout_s=1.0)
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(json.loads(ready.body), {"ok": False, "dependency": "dsh-web-host"})
        self.assertNotIn("private transport detail", ready.body.decode())


if __name__ == "__main__":
    unittest.main()
