from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from dsh_mcp_gateway import build_embedded_oauth_server
from dsh_mcp_gateway.routing import GatewayService
from dsh_mcp_gateway.types import SessionHandle, SessionPresence

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

if MCP_AVAILABLE:
    from mcp.server.auth.provider import (
        AuthorizationParams,
        AuthorizeError,
        RegistrationError,
        TokenError,
    )
    from mcp.shared.auth import OAuthClientInformationFull
    from mcp.types import LATEST_PROTOCOL_VERSION
    from starlette.testclient import TestClient

    from dsh_mcp_gateway.oauth import (
        EmbeddedOAuthConfig,
        EmbeddedOAuthProvider,
        PinAttemptLimiter,
        RegistrationBodyLimitMiddleware,
    )


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
            admin_pin="test-admin-pin",
        )

    def test_issuer_is_canonicalized_once_for_metadata_and_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertEqual(config.issuer_url, "http://127.0.0.1:8000/")
            self.assertEqual(config.scopes, ("dsh:control", "offline_access"))
            self.assertEqual(config.required_scopes, ("dsh:control",))

    def test_required_scopes_must_be_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "subset"):
            EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                scopes=("offline_access",),
                required_scopes=("dsh:control",),
            )

    def test_admin_pin_requires_at_least_twelve_characters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "at least 12"):
            EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="too-short",
            )

    def test_dynamic_client_capacity_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "positive integer"):
            EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                max_registered_clients=0,
            )

    def test_dynamic_client_metadata_budget_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "positive integer"):
            EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                max_client_metadata_bytes=0,
            )

    def test_dynamic_registration_request_budget_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "positive integer"):
            EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                max_registration_request_bytes=0,
            )

    async def test_registration_body_limit_counts_streamed_bytes_without_content_length(self) -> None:
        app_called = False

        async def downstream(_scope, _receive, _send):
            nonlocal app_called
            app_called = True

        middleware = RegistrationBodyLimitMiddleware(downstream, max_bytes=5)
        messages = [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ]
        sent: list[dict[str, object]] = []

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        await middleware(
            {"type": "http", "method": "POST", "path": "/register", "headers": []},
            receive,
            send,
        )
        self.assertFalse(app_called)
        starts = [message for message in sent if message.get("type") == "http.response.start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["status"], 413)

    def test_pending_per_client_capacity_must_not_exceed_global_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "must not exceed"):
            EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                max_pending_authorizations=1,
                max_pending_per_client=2,
            )

    async def test_dynamic_client_capacity_is_atomic_under_concurrent_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                max_registered_clients=1,
            )
            provider = EmbeddedOAuthProvider(config)
            clients = [
                OAuthClientInformationFull(
                    client_id=f"client-{index}",
                    redirect_uris=["http://127.0.0.1:9999/callback"],
                    response_types=["code"],
                    grant_types=["authorization_code"],
                    token_endpoint_auth_method="none",
                    scope="dsh:control",
                )
                for index in (1, 2)
            ]

            results = await asyncio.gather(
                *(provider.register_client(client) for client in clients),
                return_exceptions=True,
            )
            self.assertEqual(sum(result is None for result in results), 1)
            failures = [result for result in results if isinstance(result, RegistrationError)]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].error, "invalid_client_metadata")

            registered = [client for client in clients if await provider.get_client(client.client_id) is not None]
            self.assertEqual(len(registered), 1)
            await provider.register_client(registered[0])
            with sqlite3.connect(config.state_db) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0], 1)

    async def test_dynamic_client_metadata_size_is_bounded_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ordinary = OAuthClientInformationFull(
                client_id="ordinary-client",
                client_name="ordinary",
                redirect_uris=["http://127.0.0.1:9999/callback"],
                response_types=["code"],
                grant_types=["authorization_code"],
                token_endpoint_auth_method="none",
                scope="dsh:control",
            )
            ordinary_bytes = len(ordinary.model_dump_json().encode("utf-8"))
            config = EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                max_client_metadata_bytes=ordinary_bytes + 64,
            )
            provider = EmbeddedOAuthProvider(config)
            await provider.register_client(ordinary)

            oversized = OAuthClientInformationFull(
                client_id="oversized-client",
                client_name="x" * 1024,
                redirect_uris=["http://127.0.0.1:9999/callback"],
                response_types=["code"],
                grant_types=["authorization_code"],
                token_endpoint_auth_method="none",
                scope="dsh:control",
            )
            with self.assertRaises(RegistrationError) as captured:
                await provider.register_client(oversized)
            self.assertEqual(captured.exception.error, "invalid_client_metadata")
            self.assertIn("persistence limit", captured.exception.error_description or "")
            self.assertIsNone(await provider.get_client("oversized-client"))
            with sqlite3.connect(config.state_db) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0], 1)

    async def test_pending_authorization_capacity_is_atomic_and_prunes_expired_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                max_pending_authorizations=1,
                max_pending_per_client=1,
            )
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

            async def authorize(state: str):
                return await provider.authorize(
                    client,
                    AuthorizationParams(
                        state=state,
                        scopes=["dsh:control"],
                        code_challenge="challenge",
                        redirect_uri="http://127.0.0.1:9999/callback",
                        redirect_uri_provided_explicitly=True,
                        resource=config.resource_url,
                    ),
                )

            results = await asyncio.gather(
                authorize("first"),
                authorize("second"),
                return_exceptions=True,
            )
            self.assertEqual(sum(isinstance(result, str) for result in results), 1)
            failures = [result for result in results if isinstance(result, AuthorizeError)]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].error, "temporarily_unavailable")
            with sqlite3.connect(config.state_db) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM pending_authorizations").fetchone()[0],
                    1,
                )
                db.execute("UPDATE pending_authorizations SET expires_at = 0")
                db.commit()

            target = await authorize("after-expiry")
            self.assertIn("/approve?request=", target)
            with sqlite3.connect(config.state_db) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM pending_authorizations").fetchone()[0],
                    1,
                )

    async def test_oauth_writes_prune_expired_short_lived_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            provider = EmbeddedOAuthProvider(config)
            first = OAuthClientInformationFull(
                client_id="client-1",
                redirect_uris=["http://127.0.0.1:9999/callback"],
                response_types=["code"],
                grant_types=["authorization_code"],
                token_endpoint_auth_method="none",
                scope="dsh:control",
            )
            await provider.register_client(first)
            with sqlite3.connect(config.state_db) as db:
                db.execute(
                    """
                    INSERT INTO pending_authorizations(
                        request_id, client_id, scopes_json, code_challenge,
                        redirect_uri, redirect_uri_explicit, resource, state, expires_at
                    ) VALUES ('expired-pending', 'client-1', '[\"dsh:control\"]', 'challenge',
                              'http://127.0.0.1:9999/callback', 1, ?, NULL, 0)
                    """,
                    (config.resource_url,),
                )
                db.execute(
                    """
                    INSERT INTO authorization_codes(
                        code, client_id, scopes_json, expires_at, code_challenge,
                        redirect_uri, redirect_uri_explicit, resource, subject
                    ) VALUES ('expired-code', 'client-1', '[\"dsh:control\"]', 0, 'challenge',
                              'http://127.0.0.1:9999/callback', 1, ?, 'owner')
                    """,
                    (config.resource_url,),
                )
                for table, token in (("access_tokens", "expired-access"), ("refresh_tokens", "expired-refresh")):
                    db.execute(
                        f"""
                        INSERT INTO {table}(
                            token, grant_id, client_id, scopes_json, expires_at,
                            issuer, resource, subject
                        ) VALUES (?, 'expired-grant', 'client-1', '[\"dsh:control\"]', 0, ?, ?, 'owner')
                        """,
                        (token, config.issuer_url, config.resource_url),
                    )
                db.commit()

            second = OAuthClientInformationFull(
                client_id="client-2",
                redirect_uris=["http://127.0.0.1:9999/callback"],
                response_types=["code"],
                grant_types=["authorization_code"],
                token_endpoint_auth_method="none",
                scope="dsh:control",
            )
            await provider.register_client(second)

            with sqlite3.connect(config.state_db) as db:
                for table in (
                    "pending_authorizations",
                    "authorization_codes",
                    "access_tokens",
                    "refresh_tokens",
                ):
                    self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    async def test_approve_and_deny_compete_for_one_terminal_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
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
                    state="terminal-race",
                    scopes=["dsh:control"],
                    code_challenge="challenge",
                    redirect_uri="http://127.0.0.1:9999/callback",
                    redirect_uri_provided_explicitly=True,
                    resource=config.resource_url,
                ),
            )
            request_id = parse_qs(urlparse(target).query)["request"][0]

            approve_result, deny_result = await asyncio.gather(
                provider.approve(request_id),
                provider.deny(request_id),
            )
            terminal_results = [result for result in (approve_result, deny_result) if result is not None]
            self.assertEqual(len(terminal_results), 1)
            query = parse_qs(urlparse(terminal_results[0]).query)
            if approve_result is not None:
                self.assertIn("code", query)
                self.assertNotIn("error", query)
                expected_codes = 1
            else:
                self.assertEqual(query["error"], ["access_denied"])
                self.assertNotIn("code", query)
                expected_codes = 0

            with sqlite3.connect(config.state_db) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM pending_authorizations").fetchone()[0], 0)
                self.assertEqual(
                    db.execute("SELECT count(*) FROM authorization_codes").fetchone()[0],
                    expected_codes,
                )

    def test_pin_limiter_is_request_scoped_without_cross_request_lockout(self) -> None:
        limiter = PinAttemptLimiter(limit=2, window_s=300)
        self.assertTrue(limiter.allowed("request-a"))
        limiter.fail("request-a")
        limiter.fail("request-a")
        self.assertFalse(limiter.allowed("request-a"))
        self.assertTrue(limiter.allowed("request-b"))
        limiter.fail("request-b")
        self.assertTrue(limiter.allowed("request-c"))
        limiter.clear("request-a")
        self.assertTrue(limiter.allowed("request-a"))

    def test_pin_limiter_prunes_abandoned_request_keys_and_does_not_track_reads(self) -> None:
        limiter = PinAttemptLimiter(limit=2, window_s=10)
        with patch("dsh_mcp_gateway.oauth.time.monotonic", return_value=100.0):
            limiter.fail("abandoned-request")
        self.assertIn("abandoned-request", limiter._failures)

        with patch("dsh_mcp_gateway.oauth.time.monotonic", return_value=111.0):
            self.assertTrue(limiter.allowed("new-request"))

        self.assertNotIn("abandoned-request", limiter._failures)
        self.assertNotIn("new-request", limiter._failures)

    async def issue_tokens(
        self,
        root: Path,
        *,
        client_id: str = "client-1",
    ):
        config = self.config(root)
        provider = EmbeddedOAuthProvider(config)
        client = OAuthClientInformationFull(
            client_id=client_id,
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
        code = parse_qs(urlparse(redirect).query)["code"][0]
        auth_code = await provider.load_authorization_code(client, code)
        assert auth_code is not None
        tokens = await provider.exchange_authorization_code(client, auth_code)
        assert tokens.refresh_token is not None
        return config, provider, client, tokens

    async def test_authorization_code_rejects_fractionally_expired_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            provider = EmbeddedOAuthProvider(config)
            client = OAuthClientInformationFull(
                client_id="client-1",
                redirect_uris=["http://127.0.0.1:9999/callback"],
                response_types=["code"],
                grant_types=["authorization_code", "refresh_token"],
                token_endpoint_auth_method="none",
                scope="dsh:control",
            )
            await provider.register_client(client)
            with patch("dsh_mcp_gateway.oauth.time.time", return_value=1000.25):
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
            code = parse_qs(urlparse(redirect).query)["code"][0]
            auth_code = await provider.load_authorization_code(client, code)
            assert auth_code is not None

            with patch("dsh_mcp_gateway.oauth.time.time", return_value=1300.5):
                try:
                    await provider.exchange_authorization_code(client, auth_code)
                except TokenError as exc:
                    self.assertEqual(exc.error, "invalid_grant")
                else:
                    self.fail("fractionally expired authorization code was accepted")

    async def test_retried_approval_reuses_same_unexpired_authorization_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            provider = EmbeddedOAuthProvider(config)
            client = OAuthClientInformationFull(
                client_id="client-1",
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
                    state="retry-state",
                    scopes=["dsh:control"],
                    code_challenge="challenge",
                    redirect_uri="http://127.0.0.1:9999/callback",
                    redirect_uri_provided_explicitly=True,
                    resource=config.resource_url,
                ),
            )
            request_id = parse_qs(urlparse(target).query)["request"][0]
            first = await provider.approve(request_id)
            second = await provider.approve(request_id)
            self.assertEqual(first, second)
            assert first is not None
            query = parse_qs(urlparse(first).query)
            self.assertEqual(query["state"], ["retry-state"])
            self.assertNotIn("iss", query)
            with sqlite3.connect(config.state_db) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM authorization_codes").fetchone()[0], 1)

    async def test_authorization_code_is_single_use_under_concurrent_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            provider = EmbeddedOAuthProvider(config)
            client = OAuthClientInformationFull(
                client_id="client-1",
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
            code = parse_qs(urlparse(redirect).query)["code"][0]
            auth_code = await provider.load_authorization_code(client, code)
            assert auth_code is not None

            results = await asyncio.gather(
                provider.exchange_authorization_code(client, auth_code),
                provider.exchange_authorization_code(client, auth_code),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, TokenError)]
            successes = [result for result in results if not isinstance(result, Exception)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].error, "invalid_grant")
            with sqlite3.connect(config.state_db) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM authorization_codes").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM access_tokens").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0], 1)

    async def test_access_and_refresh_tokens_expire_at_exact_expiry_second(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, provider, client, tokens = await self.issue_tokens(Path(tmp))
            with sqlite3.connect(config.state_db) as db:
                db.execute("UPDATE access_tokens SET expires_at = 1000 WHERE token = ?", (tokens.access_token,))
                db.execute("UPDATE refresh_tokens SET expires_at = 1000 WHERE token = ?", (tokens.refresh_token,))

            with patch("dsh_mcp_gateway.oauth.time.time", return_value=1000.0):
                self.assertIsNone(await provider.load_access_token(tokens.access_token))
                self.assertIsNone(await provider.load_refresh_token(client, tokens.refresh_token))

    async def test_refresh_token_is_single_use_under_concurrent_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, provider, client, tokens = await self.issue_tokens(Path(tmp))
            refresh = await provider.load_refresh_token(client, tokens.refresh_token)
            assert refresh is not None

            results = await asyncio.gather(
                provider.exchange_refresh_token(client, refresh, ["dsh:control"]),
                provider.exchange_refresh_token(client, refresh, ["dsh:control"]),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, TokenError)]
            successes = [result for result in results if not isinstance(result, Exception)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].error, "invalid_grant")
            self.assertIsNone(await provider.load_refresh_token(client, tokens.refresh_token))
            with sqlite3.connect(config.state_db) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0], 1)

    async def test_revoking_access_token_revokes_entire_grant_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _config, provider, client, tokens = await self.issue_tokens(Path(tmp))
            access = await provider.load_access_token(tokens.access_token)
            refresh = await provider.load_refresh_token(client, tokens.refresh_token)
            assert access is not None
            assert refresh is not None

            await provider.revoke_token(access)

            self.assertIsNone(await provider.load_access_token(tokens.access_token))
            self.assertIsNone(await provider.load_refresh_token(client, tokens.refresh_token))

    async def test_refresh_rotation_preserves_family_for_later_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _config, provider, client, tokens = await self.issue_tokens(Path(tmp))
            original_access = tokens.access_token
            refresh = await provider.load_refresh_token(client, tokens.refresh_token)
            assert refresh is not None
            rotated = await provider.exchange_refresh_token(client, refresh, ["dsh:control"])
            assert rotated.refresh_token is not None
            rotated_refresh = await provider.load_refresh_token(client, rotated.refresh_token)
            assert rotated_refresh is not None

            await provider.revoke_token(rotated_refresh)

            self.assertIsNone(await provider.load_access_token(original_access))
            self.assertIsNone(await provider.load_access_token(rotated.access_token))
            self.assertIsNone(await provider.load_refresh_token(client, rotated.refresh_token))

    async def test_tokens_are_bound_to_issuing_oauth_issuer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, provider, client, tokens = await self.issue_tokens(root)
            self.assertIsNotNone(await provider.load_access_token(tokens.access_token))
            refresh = await provider.load_refresh_token(client, tokens.refresh_token)
            assert refresh is not None

            changed = EmbeddedOAuthProvider(
                EmbeddedOAuthConfig(
                    issuer_url="http://localhost:8000",
                    resource_url=config.resource_url,
                    state_db=config.state_db,
                    admin_pin=config.admin_pin,
                )
            )
            self.assertIsNone(await changed.load_access_token(tokens.access_token))
            self.assertIsNone(await changed.load_refresh_token(client, tokens.refresh_token))
            with self.assertRaises(TokenError):
                await changed.exchange_refresh_token(client, refresh, ["dsh:control"])

    async def test_legacy_token_schema_is_invalidated_but_registered_clients_survive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            client = OAuthClientInformationFull(
                client_id="legacy-client",
                redirect_uris=["http://127.0.0.1:9999/callback"],
                response_types=["code"],
                grant_types=["authorization_code"],
                token_endpoint_auth_method="none",
                scope="dsh:control",
            )
            with sqlite3.connect(config.state_db) as db:
                db.executescript(
                    """
                    CREATE TABLE oauth_clients (
                        client_id TEXT PRIMARY KEY,
                        client_json TEXT NOT NULL
                    );
                    CREATE TABLE access_tokens (
                        token TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        scopes_json TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        resource TEXT NOT NULL,
                        subject TEXT NOT NULL
                    );
                    CREATE TABLE refresh_tokens (
                        token TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        scopes_json TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        resource TEXT NOT NULL,
                        subject TEXT NOT NULL
                    );
                    """
                )
                db.execute(
                    "INSERT INTO oauth_clients VALUES (?, ?)",
                    (client.client_id, client.model_dump_json()),
                )
                db.execute(
                    "INSERT INTO access_tokens VALUES (?, ?, ?, ?, ?, ?)",
                    ("legacy-access", client.client_id, '["dsh:control"]', 4102444800, config.resource_url, "owner"),
                )
                db.execute(
                    "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?, ?, ?)",
                    ("legacy-refresh", client.client_id, '["dsh:control"]', 4102444800, config.resource_url, "owner"),
                )

            provider = EmbeddedOAuthProvider(config)
            loaded_client = await provider.get_client(client.client_id)
            self.assertIsNotNone(loaded_client)
            self.assertIsNone(await provider.load_access_token("legacy-access"))
            self.assertIsNone(await provider.load_refresh_token(client, "legacy-refresh"))

            with sqlite3.connect(config.state_db) as db:
                access_columns = {row[1] for row in db.execute("PRAGMA table_info(access_tokens)")}
                refresh_columns = {row[1] for row in db.execute("PRAGMA table_info(refresh_tokens)")}
                self.assertIn("grant_id", access_columns)
                self.assertIn("issuer", access_columns)
                self.assertIn("grant_id", refresh_columns)
                self.assertIn("issuer", refresh_columns)
                index_names = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
                    )
                }
                self.assertEqual(
                    index_names,
                    {
                        "idx_pending_authorizations_expires",
                        "idx_pending_authorizations_client",
                        "idx_authorization_codes_expires",
                        "idx_authorization_codes_request",
                        "idx_access_tokens_expires",
                        "idx_access_tokens_grant",
                        "idx_refresh_tokens_expires",
                        "idx_refresh_tokens_grant",
                    },
                )
                self.assertEqual(db.execute("SELECT count(*) FROM access_tokens").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0], 0)

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
            self.assertNotIn("iss", redirect_query)
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
            self.assertNotIn("iss", redirect_query)
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

    def test_oversized_dynamic_registration_request_is_rejected_before_sdk_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                max_registration_request_bytes=512,
            )
            server, _provider = build_embedded_oauth_server(GatewayService(FakeBackend()), config)
            app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, host="127.0.0.1")

            with TestClient(app) as client:
                registration = client.post(
                    "/register",
                    json={
                        "client_name": "x" * 4096,
                        "redirect_uris": ["https://example.com/callback"],
                        "response_types": ["code"],
                        "grant_types": ["authorization_code"],
                        "token_endpoint_auth_method": "none",
                        "scope": "dsh:control",
                    },
                )
                self.assertEqual(registration.status_code, 413)
                self.assertEqual(registration.json()["error"], "invalid_client_metadata")
                self.assertIn("HTTP body limit", registration.json()["error_description"])
                self.assertEqual(registration.headers["cache-control"], "no-store")

            self.assertFalse(config.state_db.exists())

    def test_oversized_dynamic_client_metadata_is_rejected_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = EmbeddedOAuthConfig(
                issuer_url="http://127.0.0.1:8000",
                resource_url="http://127.0.0.1:8000/mcp",
                state_db=Path(tmp) / "oauth.sqlite3",
                admin_pin="test-admin-pin",
                max_client_metadata_bytes=512,
            )
            server, _provider = build_embedded_oauth_server(GatewayService(FakeBackend()), config)
            app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, host="127.0.0.1")

            with TestClient(app) as client:
                registration = client.post(
                    "/register",
                    json={
                        "client_name": "x" * 4096,
                        "redirect_uris": ["https://example.com/callback"],
                        "response_types": ["code"],
                        "grant_types": ["authorization_code"],
                        "token_endpoint_auth_method": "none",
                        "scope": "dsh:control",
                    },
                )
                self.assertEqual(registration.status_code, 400)
                self.assertEqual(registration.json()["error"], "invalid_client_metadata")
                self.assertIn("persistence limit", registration.json()["error_description"])

            if config.state_db.exists():
                with sqlite3.connect(config.state_db) as db:
                    table_exists = db.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'oauth_clients'"
                    ).fetchone()[0]
                    if table_exists:
                        self.assertEqual(db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0], 0)

    def test_pin_lockout_is_scoped_to_one_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            server, _provider = build_embedded_oauth_server(GatewayService(FakeBackend()), config)
            app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, host="127.0.0.1")
            redirect_uri = "https://example.com/chatgpt-callback"
            challenge = "challenge"

            with TestClient(app) as client:
                registration = client.post(
                    "/register",
                    json={
                        "client_name": "pin-scope-test",
                        "redirect_uris": [redirect_uri],
                        "response_types": ["code"],
                        "grant_types": ["authorization_code"],
                        "token_endpoint_auth_method": "none",
                        "scope": "dsh:control",
                    },
                )
                self.assertEqual(registration.status_code, 201)
                client_id = registration.json()["client_id"]

                request_ids: list[str] = []
                for state in ("blocked-request", "independent-request"):
                    authorization = client.get(
                        "/authorize",
                        params={
                            "response_type": "code",
                            "client_id": client_id,
                            "redirect_uri": redirect_uri,
                            "scope": "dsh:control",
                            "state": state,
                            "code_challenge": challenge,
                            "code_challenge_method": "S256",
                            "resource": config.resource_url,
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(authorization.status_code, 302)
                    request_ids.append(
                        parse_qs(urlparse(authorization.headers["location"]).query)["request"][0]
                    )

                for _ in range(5):
                    wrong = client.post(
                        "/approve",
                        data={"request": request_ids[0], "pin": "definitely-wrong", "action": "approve"},
                        follow_redirects=False,
                    )
                    self.assertEqual(wrong.status_code, 403)
                blocked = client.post(
                    "/approve",
                    data={"request": request_ids[0], "pin": config.admin_pin, "action": "approve"},
                    follow_redirects=False,
                )
                self.assertEqual(blocked.status_code, 429)

                independent = client.post(
                    "/approve",
                    data={"request": request_ids[1], "pin": config.admin_pin, "action": "approve"},
                    follow_redirects=False,
                )
                self.assertEqual(independent.status_code, 302)
                self.assertIn("code", parse_qs(urlparse(independent.headers["location"]).query))

    def test_public_client_pkce_flow_reaches_protected_mcp_and_issues_refresh_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            server, _provider = build_embedded_oauth_server(GatewayService(FakeBackend()), config)
            app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, host="127.0.0.1")
            verifier = "v" * 48
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            redirect_uri = "https://example.com/chatgpt-callback"

            with TestClient(app, base_url="http://127.0.0.1:8000") as client:
                metadata = client.get("/.well-known/oauth-authorization-server")
                self.assertEqual(metadata.status_code, 200)
                metadata_json = metadata.json()
                self.assertIn("offline_access", metadata_json["scopes_supported"])
                self.assertIn("none", metadata_json["token_endpoint_auth_methods_supported"])
                self.assertNotIn("none", metadata_json["revocation_endpoint_auth_methods_supported"])
                metadata_options = client.options(
                    "/.well-known/oauth-authorization-server",
                    headers={
                        "Origin": "https://example.com",
                        "Access-Control-Request-Method": "GET",
                    },
                )
                self.assertEqual(metadata_options.status_code, 200)
                self.assertEqual(metadata_options.headers["access-control-allow-origin"], "*")
                protected = client.get("/.well-known/oauth-protected-resource/mcp")
                self.assertEqual(protected.status_code, 200)
                self.assertEqual(protected.json()["scopes_supported"], ["dsh:control"])

                registration = client.post(
                    "/register",
                    json={
                        "client_name": "chatgpt-public-client-test",
                        "redirect_uris": [redirect_uri],
                        "response_types": ["code"],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_method": "none",
                        "scope": "dsh:control offline_access",
                    },
                )
                self.assertEqual(registration.status_code, 201)
                registered = registration.json()
                self.assertEqual(registered["token_endpoint_auth_method"], "none")
                self.assertNotIn("client_secret", registered)

                authorization = client.get(
                    "/authorize",
                    params={
                        "response_type": "code",
                        "client_id": registered["client_id"],
                        "redirect_uri": redirect_uri,
                        "scope": "dsh:control offline_access",
                        "state": "state-public-client",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "resource": config.resource_url,
                    },
                    follow_redirects=False,
                )
                self.assertEqual(authorization.status_code, 302)
                request_id = parse_qs(urlparse(authorization.headers["location"]).query)["request"][0]

                approval_page = client.get(f"/approve?request={request_id}")
                self.assertEqual(approval_page.status_code, 200)
                self.assertEqual(approval_page.headers["cache-control"], "no-store")
                self.assertEqual(approval_page.headers["pragma"], "no-cache")
                self.assertEqual(approval_page.headers["referrer-policy"], "no-referrer")
                self.assertEqual(approval_page.headers["x-content-type-options"], "nosniff")
                self.assertEqual(approval_page.headers["x-frame-options"], "DENY")
                self.assertIn("frame-ancestors 'none'", approval_page.headers["content-security-policy"])
                self.assertNotIn("form-action", approval_page.headers["content-security-policy"])
                self.assertIn("base-uri 'none'", approval_page.headers["content-security-policy"])
                approval_text = approval_page.text
                self.assertIn("chatgpt-public-client-test", approval_text)
                self.assertIn(registered["client_id"], approval_text)
                self.assertIn(redirect_uri, approval_text)
                self.assertIn("<strong>Client auth:</strong> none", approval_text)

                wrong_pin = client.post(
                    "/approve",
                    data={"request": request_id, "pin": "definitely-wrong", "action": "approve"},
                    follow_redirects=False,
                )
                self.assertEqual(wrong_pin.status_code, 403)
                self.assertEqual(wrong_pin.headers["cache-control"], "no-store")
                self.assertEqual(wrong_pin.headers["x-frame-options"], "DENY")

                approval = client.post(
                    "/approve",
                    data={"request": request_id, "pin": config.admin_pin, "action": "approve"},
                    follow_redirects=False,
                )
                self.assertEqual(approval.status_code, 302)
                self.assertEqual(approval.headers["cache-control"], "no-store")
                self.assertEqual(approval.headers["referrer-policy"], "no-referrer")
                callback = parse_qs(urlparse(approval.headers["location"]).query)
                self.assertNotIn("iss", callback)

                repeated_approval = client.post(
                    "/approve",
                    data={"request": request_id, "pin": config.admin_pin, "action": "approve"},
                    follow_redirects=False,
                )
                self.assertEqual(repeated_approval.status_code, 302)
                self.assertEqual(repeated_approval.headers["location"], approval.headers["location"])

                token = client.post(
                    "/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": registered["client_id"],
                        "code": callback["code"][0],
                        "redirect_uri": redirect_uri,
                        "code_verifier": verifier,
                        "resource": config.resource_url,
                    },
                )
                self.assertEqual(token.status_code, 200)
                issued = token.json()
                self.assertTrue(issued["access_token"])
                self.assertTrue(issued["refresh_token"])
                self.assertEqual(set(issued["scope"].split()), {"dsh:control", "offline_access"})

                initialize = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LATEST_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "oauth-http-regression", "version": "1.0"},
                    },
                }
                unauthenticated = client.post(
                    "/mcp",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json=initialize,
                )
                self.assertEqual(unauthenticated.status_code, 401)

                authenticated_headers = {
                    "Authorization": f"Bearer {issued['access_token']}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
                initialized = client.post("/mcp", headers=authenticated_headers, json=initialize)
                self.assertEqual(initialized.status_code, 200)
                initialized_json = initialized.json()
                negotiated_version = initialized_json["result"]["protocolVersion"]
                self.assertTrue(negotiated_version)
                mcp_session_id = initialized.headers.get("mcp-session-id")
                self.assertTrue(mcp_session_id)

                session_headers = {
                    **authenticated_headers,
                    "mcp-session-id": mcp_session_id,
                    "mcp-protocol-version": negotiated_version,
                }
                initialized_notification = client.post(
                    "/mcp",
                    headers=session_headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                self.assertEqual(initialized_notification.status_code, 202)

                tools = client.post(
                    "/mcp",
                    headers=session_headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                self.assertEqual(tools.status_code, 200)
                tool_names = {tool["name"] for tool in tools.json()["result"]["tools"]}
                self.assertIn("dsh_messages", tool_names)
                self.assertIn("dsh_list", tool_names)

                list_call = client.post(
                    "/mcp",
                    headers=session_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "dsh_list", "arguments": {"limit": 1}},
                    },
                )
                self.assertEqual(list_call.status_code, 200)
                structured = list_call.json()["result"]["structuredContent"]
                self.assertEqual(structured["items"], [])
                self.assertEqual(structured["total"], 0)
                self.assertFalse(structured["has_more"])

            restarted_server, _restarted_provider = build_embedded_oauth_server(
                GatewayService(FakeBackend()),
                config,
            )
            restarted_app = restarted_server.streamable_http_app(
                streamable_http_path="/mcp",
                json_response=True,
                host="127.0.0.1",
            )
            with TestClient(restarted_app, base_url="http://127.0.0.1:8000") as restarted_client:
                restarted_headers = {
                    "Authorization": f"Bearer {issued['access_token']}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
                restarted_initialize = restarted_client.post(
                    "/mcp",
                    headers=restarted_headers,
                    json=initialize,
                )
                self.assertEqual(restarted_initialize.status_code, 200)
                restarted_negotiated_version = restarted_initialize.json()["result"]["protocolVersion"]
                restarted_session_id = restarted_initialize.headers.get("mcp-session-id")
                self.assertTrue(restarted_session_id)
                self.assertNotEqual(restarted_session_id, mcp_session_id)

                restarted_session_headers = {
                    **restarted_headers,
                    "mcp-session-id": restarted_session_id,
                    "mcp-protocol-version": restarted_negotiated_version,
                }
                restarted_notification = restarted_client.post(
                    "/mcp",
                    headers=restarted_session_headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                self.assertEqual(restarted_notification.status_code, 202)
                restarted_list_call = restarted_client.post(
                    "/mcp",
                    headers=restarted_session_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "dsh_list", "arguments": {"limit": 1}},
                    },
                )
                self.assertEqual(restarted_list_call.status_code, 200)
                self.assertEqual(restarted_list_call.json()["result"]["structuredContent"]["items"], [])

                rotated = restarted_client.post(
                    "/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": registered["client_id"],
                        "refresh_token": issued["refresh_token"],
                        "scope": "dsh:control offline_access",
                        "resource": config.resource_url,
                    },
                )
                self.assertEqual(rotated.status_code, 200)
                rotated_json = rotated.json()
                self.assertTrue(rotated_json["access_token"])
                self.assertNotEqual(rotated_json["refresh_token"], issued["refresh_token"])

                replayed_refresh = restarted_client.post(
                    "/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": registered["client_id"],
                        "refresh_token": issued["refresh_token"],
                        "scope": "dsh:control offline_access",
                        "resource": config.resource_url,
                    },
                )
                self.assertEqual(replayed_refresh.status_code, 400)
                self.assertEqual(replayed_refresh.json()["error"], "invalid_grant")


if __name__ == "__main__":
    unittest.main()
