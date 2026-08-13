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
        fake_server.run.assert_called_once_with(
            transport="streamable-http",
            host="127.0.0.1",
            port=9876,
            streamable_http_path="/mcp",
            json_response=True,
        )


if __name__ == "__main__":
    unittest.main()
