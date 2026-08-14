from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dsh_mcp_gateway.cli import main


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

    def test_happy_path_builds_loopback_oauth_gateway_with_canonical_issuer(self) -> None:
        fake_backend = Mock()
        fake_backend.base_url = "http://127.0.0.1:3080"
        fake_backend.describe_host.return_value = {"version": "0.0.1"}
        fake_server = Mock()

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DSH_MCP_GATEWAY_ADMIN_PIN": "12345678"}, clear=False),
            patch("dsh_mcp_gateway.cli.ExperimentalWebHostBackend", return_value=fake_backend),
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
        config = build_server.call_args.args[1]
        self.assertEqual(config.issuer_url, "https://gateway.example.com/")
        self.assertEqual(config.resource_url, "https://gateway.example.com/mcp")
        self.assertEqual(config.state_db, Path(tmp).resolve() / "oauth.sqlite3")
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


if __name__ == "__main__":
    unittest.main()
