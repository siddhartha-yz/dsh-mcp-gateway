from __future__ import annotations

import unittest
from unittest.mock import patch

from dsh_mcp_gateway.mcp_compat import disable_modern_subscriptions


class _LowLevel:
    def __init__(self, handlers):
        self._request_handlers = handlers

    def get_request_handler(self, method):
        return self._request_handlers.get(method)


class _Server:
    def __init__(self, lowlevel):
        self._lowlevel_server = lowlevel


class McpCompatibilityTests(unittest.TestCase):
    def test_disable_modern_subscriptions_removes_only_listen_handler(self) -> None:
        ping = object()
        listen = object()
        server = _Server(_LowLevel({"ping": ping, "subscriptions/listen": listen}))

        disable_modern_subscriptions(server)

        self.assertEqual(server._lowlevel_server._request_handlers, {"ping": ping})
        self.assertIsNone(server._lowlevel_server.get_request_handler("subscriptions/listen"))

    def test_disable_modern_subscriptions_rejects_unverified_mcp_version(self) -> None:
        server = _Server(_LowLevel({"subscriptions/listen": object()}))
        with (
            patch("dsh_mcp_gateway.mcp_compat.version", return_value="2.1.0"),
            self.assertRaisesRegex(RuntimeError, "supports mcp==2.0.0, found 2.1.0"),
        ):
            disable_modern_subscriptions(server)

    def test_disable_modern_subscriptions_fails_closed_when_sdk_seam_changes(self) -> None:
        for lowlevel in (object(), _LowLevel({"ping": object()})):
            with self.subTest(lowlevel=lowlevel), self.assertRaisesRegex(
                RuntimeError,
                "cannot guarantee meta-only tool surface",
            ):
                disable_modern_subscriptions(_Server(lowlevel))


if __name__ == "__main__":
    unittest.main()
