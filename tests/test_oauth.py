from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dsh_mcp_gateway import build_embedded_oauth_server
from dsh_mcp_gateway.routing import GatewayService
from dsh_mcp_gateway.types import SessionHandle, SessionPresence

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

if MCP_AVAILABLE:
    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull

    from dsh_mcp_gateway.oauth import EmbeddedOAuthConfig, EmbeddedOAuthProvider


class FakeBackend:
    def presence(self, session_id: str) -> SessionPresence:
        return SessionPresence.ABSENT

    def reuse(self, session_id: str) -> SessionHandle:
        return SessionHandle(session_id)

    def resume(self, session_id: str) -> SessionHandle:
        return SessionHandle(session_id)

    def create(self, session_id: str | None = None) -> SessionHandle:
        return SessionHandle(session_id or "generated")

    def prompt(self, session_id: str, text: str) -> str:
        return "message-1"

    def status(self, session_id: str) -> dict[str, object]:
        return {"session_id": session_id}

    def history(self, session_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        return []

    def list_sessions(self) -> list[dict[str, object]]:
        return []

    def cancel(self, session_id: str) -> dict[str, object]:
        return {"session_id": session_id, "canceled": False}


@unittest.skipUnless(MCP_AVAILABLE, "install dsh-mcp-gateway[server] to test OAuth")
class EmbeddedOAuthTests(unittest.IsolatedAsyncioTestCase):
    def config(self, root: Path) -> EmbeddedOAuthConfig:
        return EmbeddedOAuthConfig(
            issuer_url="http://127.0.0.1:8000",
            resource_url="http://127.0.0.1:8000/mcp",
            state_db=root / "oauth.sqlite3",
            admin_pin="123456",
        )

    def test_issuer_is_canonicalized_once_for_metadata_and_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertEqual(config.issuer_url, "http://127.0.0.1:8000/")

    async def test_clients_and_rotated_tokens_survive_provider_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            provider = EmbeddedOAuthProvider(config)
            client = OAuthClientInformationFull(
                client_id="client-1",
                client_name="test",
                redirect_uris=["http://127.0.0.1:9999/callback"],
                response_types=["code"],
                grant_types=["authorization_code", "refresh_token"],
                token_endpoint_auth_method="none",
                scope="dsh:control",
            )
            await provider.register_client(client)
            target = await provider.authorize(
                client,
                AuthorizationParams(
                    state="state-1",
                    scopes=["dsh:control"],
                    code_challenge="challenge",
                    redirect_uri="http://127.0.0.1:9999/callback",
                    redirect_uri_provided_explicitly=True,
                    resource=config.resource_url,
                ),
            )
            request_id = parse_qs(urlparse(target).query)["request"][0]
            redirect = await provider.approve(request_id)
            assert redirect is not None
            redirect_query = parse_qs(urlparse(redirect).query)
            self.assertEqual(redirect_query["iss"], [config.issuer_url])
            code = redirect_query["code"][0]
            auth_code = await provider.load_authorization_code(client, code)
            assert auth_code is not None
            tokens = await provider.exchange_authorization_code(client, auth_code)
            refresh = await provider.load_refresh_token(client, tokens.refresh_token)
            assert refresh is not None
            rotated = await provider.exchange_refresh_token(client, refresh, ["dsh:control"])
            self.assertNotEqual(rotated.refresh_token, tokens.refresh_token)
            self.assertIsNone(await provider.load_refresh_token(client, tokens.refresh_token))

            restarted = EmbeddedOAuthProvider(config)
            loaded = await restarted.get_client("client-1")
            self.assertIsNotNone(loaded)
            self.assertIsNotNone(await restarted.load_access_token(rotated.access_token))

    async def test_access_token_is_bound_to_configured_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            provider = EmbeddedOAuthProvider(config)
            client = OAuthClientInformationFull(
                client_id="client-1",
                redirect_uris=["http://127.0.0.1:9999/callback"],
                response_types=["code"],
                grant_types=["authorization_code"],
                token_endpoint_auth_method="none",
                scope="dsh:control",
            )
            await provider.register_client(client)
            target = await provider.authorize(
                client,
                AuthorizationParams(
                    state=None,
                    scopes=["dsh:control"],
                    code_challenge="challenge",
                    redirect_uri="http://127.0.0.1:9999/callback",
                    redirect_uri_provided_explicitly=True,
                    resource=config.resource_url,
                ),
            )
            request_id = parse_qs(urlparse(target).query)["request"][0]
            redirect = await provider.approve(request_id)
            assert redirect is not None
            redirect_query = parse_qs(urlparse(redirect).query)
            self.assertEqual(redirect_query["iss"], [config.issuer_url])
            code = redirect_query["code"][0]
            auth_code = await provider.load_authorization_code(client, code)
            assert auth_code is not None
            tokens = await provider.exchange_authorization_code(client, auth_code)
            changed = EmbeddedOAuthProvider(
                EmbeddedOAuthConfig(
                    issuer_url=config.issuer_url,
                    resource_url="http://127.0.0.1:8000/other-mcp",
                    state_db=config.state_db,
                    admin_pin=config.admin_pin,
                )
            )
            self.assertIsNone(await changed.load_access_token(tokens.access_token))

    def test_mcp_app_contains_oauth_resource_and_approval_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            server, _provider = build_embedded_oauth_server(GatewayService(FakeBackend()), config)
            app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, host="127.0.0.1")
            paths = [getattr(route, "path", None) for route in app.routes]
            self.assertIn("/.well-known/oauth-authorization-server", paths)
            self.assertIn("/.well-known/oauth-protected-resource/mcp", paths)
            self.assertIn("/register", paths)
            self.assertIn("/authorize", paths)
            self.assertIn("/token", paths)
            self.assertIn("/revoke", paths)
            self.assertIn("/approve", paths)
            self.assertIn("/mcp", paths)


if __name__ == "__main__":
    unittest.main()
