from __future__ import annotations

import asyncio
import hmac
import html
import json
import secrets
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True, slots=True)
class EmbeddedOAuthConfig:
    issuer_url: str
    resource_url: str
    state_db: Path
    admin_pin: str
    scopes: tuple[str, ...] = ("dsh:control", "offline_access")
    required_scopes: tuple[str, ...] = ("dsh:control",)
    pending_ttl_s: int = 600
    code_ttl_s: int = 300
    access_token_ttl_s: int = 3600
    refresh_token_ttl_s: int = 30 * 24 * 3600
    max_registered_clients: int = 256
    max_client_metadata_bytes: int = 32 * 1024
    max_registration_request_bytes: int = 64 * 1024
    max_pending_authorizations: int = 512
    max_pending_per_client: int = 8

    def __post_init__(self) -> None:
        if not self.issuer_url:
            raise ValueError("issuer_url is required")
        object.__setattr__(self, "issuer_url", f"{self.issuer_url.rstrip('/')}/")
        if not self.resource_url:
            raise ValueError("resource_url is required")
        if len(self.admin_pin) < 12:
            raise ValueError("admin_pin must contain at least 12 characters")
        if not self.scopes or any(not scope for scope in self.scopes):
            raise ValueError("at least one non-empty OAuth scope is required")
        if not self.required_scopes or any(not scope for scope in self.required_scopes):
            raise ValueError("at least one non-empty required OAuth scope is required")
        if not set(self.required_scopes).issubset(self.scopes):
            raise ValueError("required OAuth scopes must be a subset of allowed scopes")
        for name in ("pending_ttl_s", "code_ttl_s", "access_token_ttl_s", "refresh_token_ttl_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "max_registered_clients",
            "max_client_metadata_bytes",
            "max_registration_request_bytes",
            "max_pending_authorizations",
            "max_pending_per_client",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_pending_per_client > self.max_pending_authorizations:
            raise ValueError("max_pending_per_client must not exceed max_pending_authorizations")


class RegistrationBodyLimitMiddleware:
    """Bound the anonymous DCR request body before the SDK parses it."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/register":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for key, value in scope.get("headers", ()) if key.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > self.max_bytes:
                await self._reject(scope, receive, send)
                return

        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if replayed or disconnected:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": (
                    "dynamic client registration request exceeds the configured "
                    f"{self.max_bytes}-byte HTTP body limit"
                ),
            },
            status_code=413,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)


def install_registration_body_limit(app: Any, *, max_bytes: int) -> None:
    app.add_middleware(RegistrationBodyLimitMiddleware, max_bytes=max_bytes)


class GatewayRefreshToken(RefreshToken):
    resource: str | None = None
    issuer: str | None = None


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    request_id: str
    client_id: str
    scopes: tuple[str, ...]
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    resource: str
    state: str | None
    expires_at: float


class OAuthStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema(connection)
        return connection

    @contextmanager
    def connection(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_authorizations (
                    request_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_uri_explicit INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    state TEXT,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authorization_codes (
                    code TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    code_challenge TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_uri_explicit INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    request_id TEXT,
                    state TEXT
                );
                CREATE TABLE IF NOT EXISTS access_tokens (
                    token TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    issuer TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    issuer TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL
                );
                """
            )
            required_token_columns = {
                "token",
                "grant_id",
                "client_id",
                "scopes_json",
                "expires_at",
                "issuer",
                "resource",
                "subject",
            }
            access_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(access_tokens)").fetchall()
            }
            refresh_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(refresh_tokens)").fetchall()
            }
            if access_columns != required_token_columns or refresh_columns != required_token_columns:
                # Legacy rows cannot be mapped back to a reliable authorization
                # grant family. Invalidate only token/code state and preserve DCR
                # clients plus pending approvals; the owner can re-authorize.
                connection.executescript(
                    """
                    DROP TABLE access_tokens;
                    DROP TABLE refresh_tokens;
                    DELETE FROM authorization_codes;
                    CREATE TABLE access_tokens (
                        token TEXT PRIMARY KEY,
                        grant_id TEXT NOT NULL,
                        client_id TEXT NOT NULL,
                        scopes_json TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        issuer TEXT NOT NULL,
                        resource TEXT NOT NULL,
                        subject TEXT NOT NULL
                    );
                    CREATE TABLE refresh_tokens (
                        token TEXT PRIMARY KEY,
                        grant_id TEXT NOT NULL,
                        client_id TEXT NOT NULL,
                        scopes_json TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        issuer TEXT NOT NULL,
                        resource TEXT NOT NULL,
                        subject TEXT NOT NULL
                    );
                    """
                )
            authorization_code_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(authorization_codes)").fetchall()
            }
            if "request_id" not in authorization_code_columns:
                connection.execute("ALTER TABLE authorization_codes ADD COLUMN request_id TEXT")
            if "state" not in authorization_code_columns:
                connection.execute("ALTER TABLE authorization_codes ADD COLUMN state TEXT")

            # Create secondary indexes after any legacy token-table rebuild so
            # DROP TABLE cannot silently remove the fresh token indexes.
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_pending_authorizations_expires
                    ON pending_authorizations(expires_at);
                CREATE INDEX IF NOT EXISTS idx_pending_authorizations_client
                    ON pending_authorizations(client_id);
                CREATE INDEX IF NOT EXISTS idx_authorization_codes_expires
                    ON authorization_codes(expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_authorization_codes_request
                    ON authorization_codes(request_id) WHERE request_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_access_tokens_expires
                    ON access_tokens(expires_at);
                CREATE INDEX IF NOT EXISTS idx_access_tokens_grant
                    ON access_tokens(grant_id);
                CREATE INDEX IF NOT EXISTS idx_refresh_tokens_expires
                    ON refresh_tokens(expires_at);
                CREATE INDEX IF NOT EXISTS idx_refresh_tokens_grant
                    ON refresh_tokens(grant_id);
                """
            )
            connection.commit()
            self._initialized = True

    @staticmethod
    def _prune_expired(db: sqlite3.Connection, *, now: float) -> None:
        db.execute("DELETE FROM pending_authorizations WHERE expires_at < ?", (now,))
        db.execute("DELETE FROM authorization_codes WHERE expires_at < ?", (now,))
        db.execute("DELETE FROM access_tokens WHERE expires_at < ?", (int(now),))
        db.execute("DELETE FROM refresh_tokens WHERE expires_at < ?", (int(now),))

    def save_client(self, client: OAuthClientInformationFull) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO oauth_clients(client_id, client_json) VALUES (?, ?)",
                (client.client_id, client.model_dump_json()),
            )

    def save_client_limited(self, client: OAuthClientInformationFull, *, max_clients: int) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune_expired(db, now=time.time())
            existing = db.execute(
                "SELECT 1 FROM oauth_clients WHERE client_id = ?",
                (client.client_id,),
            ).fetchone()
            if existing is None:
                count = int(db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0])
                if count >= max_clients:
                    return False
            db.execute(
                "INSERT OR REPLACE INTO oauth_clients(client_id, client_json) VALUES (?, ?)",
                (client.client_id, client.model_dump_json()),
            )
        return True

    def load_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT client_json FROM oauth_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row["client_json"])

    def save_pending(self, pending: PendingAuthorization) -> None:
        with self.connection() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO pending_authorizations(
                    request_id, client_id, scopes_json, code_challenge,
                    redirect_uri, redirect_uri_explicit, resource, state, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending.request_id,
                    pending.client_id,
                    json.dumps(pending.scopes),
                    pending.code_challenge,
                    pending.redirect_uri,
                    int(pending.redirect_uri_provided_explicitly),
                    pending.resource,
                    pending.state,
                    pending.expires_at,
                ),
            )

    def save_pending_limited(
        self,
        pending: PendingAuthorization,
        *,
        max_total: int,
        max_per_client: int,
    ) -> bool:
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune_expired(db, now=now)
            total = int(db.execute("SELECT count(*) FROM pending_authorizations").fetchone()[0])
            per_client = int(
                db.execute(
                    "SELECT count(*) FROM pending_authorizations WHERE client_id = ?",
                    (pending.client_id,),
                ).fetchone()[0]
            )
            if total >= max_total or per_client >= max_per_client:
                return False
            db.execute(
                """
                INSERT INTO pending_authorizations(
                    request_id, client_id, scopes_json, code_challenge,
                    redirect_uri, redirect_uri_explicit, resource, state, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending.request_id,
                    pending.client_id,
                    json.dumps(pending.scopes),
                    pending.code_challenge,
                    pending.redirect_uri,
                    int(pending.redirect_uri_provided_explicitly),
                    pending.resource,
                    pending.state,
                    pending.expires_at,
                ),
            )
        return True

    def load_pending(self, request_id: str) -> PendingAuthorization | None:
        now = time.time()
        with self.connection() as db:
            db.execute("DELETE FROM pending_authorizations WHERE expires_at < ?", (now,))
            row = db.execute(
                "SELECT * FROM pending_authorizations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return PendingAuthorization(
            request_id=row["request_id"],
            client_id=row["client_id"],
            scopes=tuple(json.loads(row["scopes_json"])),
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
            state=row["state"],
            expires_at=float(row["expires_at"]),
        )

    def consume_pending_for_code(
        self,
        request_id: str,
        *,
        code_ttl_s: int,
        subject: str,
    ) -> tuple[PendingAuthorization, str] | None:
        now = time.time()
        code = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune_expired(db, now=now)
            row = db.execute(
                "SELECT * FROM pending_authorizations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM pending_authorizations WHERE request_id = ?", (request_id,))
            db.execute(
                """
                INSERT INTO authorization_codes(
                    code, client_id, scopes_json, expires_at, code_challenge,
                    redirect_uri, redirect_uri_explicit, resource, subject, request_id, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    row["client_id"],
                    row["scopes_json"],
                    now + code_ttl_s,
                    row["code_challenge"],
                    row["redirect_uri"],
                    row["redirect_uri_explicit"],
                    row["resource"],
                    subject,
                    row["request_id"],
                    row["state"],
                ),
            )
            db.commit()
        pending = PendingAuthorization(
            request_id=row["request_id"],
            client_id=row["client_id"],
            scopes=tuple(json.loads(row["scopes_json"])),
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
            state=row["state"],
            expires_at=float(row["expires_at"]),
        )
        return pending, code

    def load_completed_approval(self, request_id: str) -> tuple[str, str, str | None] | None:
        """Return an unexpired authorization redirect payload for a retried approval POST."""
        now = time.time()
        with self.connection() as db:
            self._prune_expired(db, now=now)
            row = db.execute(
                "SELECT redirect_uri, code, state FROM authorization_codes WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["redirect_uri"]), str(row["code"]), row["state"]

    def delete_pending(self, request_id: str) -> PendingAuthorization | None:
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune_expired(db, now=now)
            row = db.execute(
                "SELECT * FROM pending_authorizations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM pending_authorizations WHERE request_id = ?", (request_id,))
        return PendingAuthorization(
            request_id=row["request_id"],
            client_id=row["client_id"],
            scopes=tuple(json.loads(row["scopes_json"])),
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
            state=row["state"],
            expires_at=float(row["expires_at"]),
        )

    def load_code(self, code: str, client_id: str) -> AuthorizationCode | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM authorization_codes WHERE code = ? AND client_id = ?",
                (code, client_id),
            ).fetchone()
        if row is None:
            return None
        return AuthorizationCode(
            code=row["code"],
            scopes=list(json.loads(row["scopes_json"])),
            expires_at=float(row["expires_at"]),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
            subject=row["subject"],
        )

    def consume_code_and_issue_tokens(
        self,
        code: str,
        client_id: str,
        *,
        issuer: str,
        access_ttl_s: int,
        refresh_ttl_s: int,
    ) -> tuple[str, str, list[str], int, int, str, str] | None:
        now = int(time.time())
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        grant_id = secrets.token_urlsafe(24)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM authorization_codes WHERE code = ? AND client_id = ? AND expires_at >= ?",
                (code, client_id, now),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            self._prune_expired(db, now=now)
            db.execute("DELETE FROM authorization_codes WHERE code = ?", (code,))
            scopes = list(json.loads(row["scopes_json"]))
            access_exp = now + access_ttl_s
            refresh_exp = now + refresh_ttl_s
            db.execute(
                """
                INSERT INTO access_tokens(
                    token, grant_id, client_id, scopes_json, expires_at, issuer, resource, subject
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    access,
                    grant_id,
                    client_id,
                    json.dumps(scopes),
                    access_exp,
                    issuer,
                    row["resource"],
                    row["subject"],
                ),
            )
            db.execute(
                """
                INSERT INTO refresh_tokens(
                    token, grant_id, client_id, scopes_json, expires_at, issuer, resource, subject
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    refresh,
                    grant_id,
                    client_id,
                    json.dumps(scopes),
                    refresh_exp,
                    issuer,
                    row["resource"],
                    row["subject"],
                ),
            )
            db.commit()
        return access, refresh, scopes, access_exp, refresh_exp, row["resource"], row["subject"]

    def load_access(self, token: str) -> sqlite3.Row | None:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM access_tokens WHERE token = ? AND expires_at >= ?",
                (token, int(time.time())),
            ).fetchone()

    def load_refresh(self, token: str, client_id: str) -> sqlite3.Row | None:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM refresh_tokens WHERE token = ? AND client_id = ? AND expires_at >= ?",
                (token, client_id, int(time.time())),
            ).fetchone()

    def rotate_refresh(
        self,
        token: str,
        client_id: str,
        scopes: list[str],
        *,
        access_ttl_s: int,
        refresh_ttl_s: int,
    ) -> tuple[str, str, int, int, str, str] | None:
        now = int(time.time())
        new_access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM refresh_tokens WHERE token = ? AND client_id = ? AND expires_at >= ?",
                (token, client_id, now),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            allowed = set(json.loads(row["scopes_json"]))
            if not set(scopes).issubset(allowed):
                db.rollback()
                raise ValueError("requested refresh scopes exceed original grant")
            self._prune_expired(db, now=now)
            db.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
            access_exp = now + access_ttl_s
            refresh_exp = now + refresh_ttl_s
            db.execute(
                """
                INSERT INTO access_tokens(
                    token, grant_id, client_id, scopes_json, expires_at, issuer, resource, subject
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_access,
                    row["grant_id"],
                    client_id,
                    json.dumps(scopes),
                    access_exp,
                    row["issuer"],
                    row["resource"],
                    row["subject"],
                ),
            )
            db.execute(
                """
                INSERT INTO refresh_tokens(
                    token, grant_id, client_id, scopes_json, expires_at, issuer, resource, subject
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_refresh,
                    row["grant_id"],
                    client_id,
                    json.dumps(scopes),
                    refresh_exp,
                    row["issuer"],
                    row["resource"],
                    row["subject"],
                ),
            )
            db.commit()
        return new_access, new_refresh, access_exp, refresh_exp, row["resource"], row["subject"]

    def revoke(self, token: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT grant_id FROM access_tokens WHERE token = ?", (token,)).fetchone()
            if row is None:
                row = db.execute("SELECT grant_id FROM refresh_tokens WHERE token = ?", (token,)).fetchone()
            if row is None:
                db.rollback()
                return
            grant_id = row["grant_id"]
            self._prune_expired(db, now=time.time())
            db.execute("DELETE FROM access_tokens WHERE grant_id = ?", (grant_id,))
            db.execute("DELETE FROM refresh_tokens WHERE grant_id = ?", (grant_id,))
            db.commit()


class EmbeddedOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, GatewayRefreshToken, AccessToken]):
    def __init__(self, config: EmbeddedOAuthConfig) -> None:
        self.config = config
        self.store = OAuthStore(config.state_db)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await asyncio.to_thread(self.store.load_client, client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        serialized = client_info.model_dump_json()
        if len(serialized.encode("utf-8")) > self.config.max_client_metadata_bytes:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description=(
                    "dynamic client metadata exceeds the configured "
                    f"{self.config.max_client_metadata_bytes}-byte persistence limit"
                ),
            )
        saved = await asyncio.to_thread(
            self.store.save_client_limited,
            client_info,
            max_clients=self.config.max_registered_clients,
        )
        if not saved:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="dynamic client registration capacity reached",
            )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        resource = params.resource or self.config.resource_url
        if resource != self.config.resource_url:
            raise AuthorizeError(error="invalid_target", error_description="unknown OAuth resource")
        scopes = tuple(params.scopes or self.config.scopes)
        if not set(scopes).issubset(self.config.scopes):
            raise AuthorizeError(error="invalid_scope", error_description="requested scope is not allowed")
        request_id = secrets.token_urlsafe(24)
        saved = await asyncio.to_thread(
            self.store.save_pending_limited,
            PendingAuthorization(
                request_id=request_id,
                client_id=client.client_id,
                scopes=scopes,
                code_challenge=params.code_challenge,
                redirect_uri=str(params.redirect_uri),
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=resource,
                state=params.state,
                expires_at=time.time() + self.config.pending_ttl_s,
            ),
            max_total=self.config.max_pending_authorizations,
            max_per_client=self.config.max_pending_per_client,
        )
        if not saved:
            raise AuthorizeError(
                error="temporarily_unavailable",
                error_description="authorization request capacity reached; retry later",
            )
        return f"{self.config.issuer_url.rstrip('/')}/approve?request={quote(request_id)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return await asyncio.to_thread(self.store.load_code, authorization_code, client.client_id)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        issued = await asyncio.to_thread(
            self.store.consume_code_and_issue_tokens,
            authorization_code.code,
            client.client_id,
            issuer=self.config.issuer_url,
            access_ttl_s=self.config.access_token_ttl_s,
            refresh_ttl_s=self.config.refresh_token_ttl_s,
        )
        if issued is None:
            raise TokenError(error="invalid_grant", error_description="authorization code is missing or already used")
        access, refresh, scopes, _access_exp, _refresh_exp, _resource, _subject = issued
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.config.access_token_ttl_s,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> GatewayRefreshToken | None:
        row = await asyncio.to_thread(self.store.load_refresh, refresh_token, client.client_id)
        if (
            row is None
            or row["resource"] != self.config.resource_url
            or row["issuer"] != self.config.issuer_url
        ):
            return None
        return GatewayRefreshToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=list(json.loads(row["scopes_json"])),
            expires_at=int(row["expires_at"]),
            subject=row["subject"],
            resource=row["resource"],
            issuer=row["issuer"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: GatewayRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if (
            refresh_token.resource != self.config.resource_url
            or refresh_token.issuer != self.config.issuer_url
        ):
            raise TokenError(error="invalid_grant", error_description="refresh token belongs to another issuer or resource")
        try:
            issued = await asyncio.to_thread(
                self.store.rotate_refresh,
                refresh_token.token,
                client.client_id,
                scopes,
                access_ttl_s=self.config.access_token_ttl_s,
                refresh_ttl_s=self.config.refresh_token_ttl_s,
            )
        except ValueError as exc:
            raise TokenError(error="invalid_scope", error_description=str(exc)) from exc
        if issued is None:
            raise TokenError(error="invalid_grant", error_description="refresh token is missing or already used")
        access, refresh, _access_exp, _refresh_exp, _resource, _subject = issued
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.config.access_token_ttl_s,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await asyncio.to_thread(self.store.load_access, token)
        if (
            row is None
            or row["resource"] != self.config.resource_url
            or row["issuer"] != self.config.issuer_url
        ):
            return None
        return AccessToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=list(json.loads(row["scopes_json"])),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["subject"],
            claims={"iss": row["issuer"]},
        )

    async def revoke_token(self, token: AccessToken | GatewayRefreshToken) -> None:
        await asyncio.to_thread(self.store.revoke, token.token)

    def pin_matches(self, candidate: str) -> bool:
        return hmac.compare_digest(self.config.admin_pin.encode(), candidate.encode())

    async def pending(self, request_id: str) -> PendingAuthorization | None:
        return await asyncio.to_thread(self.store.load_pending, request_id)

    async def approve(self, request_id: str) -> str | None:
        result = await asyncio.to_thread(
            self.store.consume_pending_for_code,
            request_id,
            code_ttl_s=self.config.code_ttl_s,
            subject="owner",
        )
        if result is not None:
            pending, code = result
            return construct_redirect_uri(
                pending.redirect_uri,
                code=code,
                state=pending.state,
            )
        completed = await asyncio.to_thread(self.store.load_completed_approval, request_id)
        if completed is None:
            return None
        redirect_uri, code, state = completed
        return construct_redirect_uri(redirect_uri, code=code, state=state)

    async def deny(self, request_id: str) -> str | None:
        pending = await asyncio.to_thread(self.store.delete_pending, request_id)
        if pending is None:
            return None
        return construct_redirect_uri(
            pending.redirect_uri,
            error="access_denied",
            state=pending.state,
        )


def advertise_public_client_auth_methods(app: Any, auth: Any) -> None:
    """Patch MCP 2.0 AS metadata to advertise its implemented public-client auth.

    MCP 2.0's token/revocation authenticators accept DCR clients registered with
    ``token_endpoint_auth_method=none`` at the token endpoint, but its metadata
    builder currently advertises only the two client-secret methods. Rebuild
    only the metadata route from the SDK's own metadata/CORS primitives and add
    that implemented token capability; all protocol handlers remain upstream-owned.
    """
    from mcp.server.auth.handlers.metadata import MetadataHandler
    from mcp.server.auth.routes import build_metadata, cors_middleware
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

    registration = auth.client_registration_options or ClientRegistrationOptions()
    revocation = auth.revocation_options or RevocationOptions()
    metadata = build_metadata(
        auth.issuer_url,
        auth.service_documentation_url,
        registration,
        revocation,
        supports_identity_assertion=auth.identity_assertion_enabled,
    )
    token_methods = list(metadata.token_endpoint_auth_methods_supported or ())
    if "none" not in token_methods:
        token_methods.append("none")
    metadata.token_endpoint_auth_methods_supported = token_methods

    metadata_path = "/.well-known/oauth-authorization-server"
    for index, route in enumerate(app.routes):
        if not isinstance(route, Route) or route.path != metadata_path:
            continue
        app.routes[index] = Route(
            metadata_path,
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
            name=route.name,
        )
        return
    raise RuntimeError("MCP authorization-server metadata route was not found")


class PinAttemptLimiter:
    """Bound failed PIN attempts to one opaque authorization request.

    Source-IP limits are intentionally avoided here: the supported deployment
    binds the gateway to loopback behind a reverse proxy, so every public client
    can otherwise collapse onto the proxy's source address and turn a brute-force
    defense into a cross-request denial of service.
    """

    def __init__(self, *, limit: int = 5, window_s: float = 300.0) -> None:
        if limit <= 0 or window_s <= 0:
            raise ValueError("PIN attempt limit and window must be positive")
        self.limit = limit
        self.window_s = window_s
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, failures: deque[float], now: float) -> None:
        while failures and now - failures[0] > self.window_s:
            failures.popleft()

    def _prune_all(self, now: float) -> None:
        stale: list[str] = []
        for request_id, failures in self._failures.items():
            self._prune(failures, now)
            if not failures:
                stale.append(request_id)
        for request_id in stale:
            self._failures.pop(request_id, None)

    def allowed(self, request_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._prune_all(now)
            failures = self._failures.get(request_id)
            return failures is None or len(failures) < self.limit

    def fail(self, request_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune_all(now)
            failures = self._failures.setdefault(request_id, deque())
            failures.append(now)

    def clear(self, request_id: str) -> None:
        with self._lock:
            self._failures.pop(request_id, None)


def install_approval_route(mcp: Any, provider: EmbeddedOAuthProvider) -> None:
    limiter = PinAttemptLimiter()
    html_headers = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Content-Security-Policy": (
            # Do not set form-action='self' here. The fixed form POSTs to
            # /approve, whose successful OAuth response redirects to the
            # registered client callback on another origin (for ChatGPT,
            # https://chatgpt.com/connector/oauth/...). Chromium applies
            # form-action across that navigation chain and otherwise blocks
            # the callback after the authorization code has already been
            # issued. The form action itself is hard-coded below; scripts and
            # base-uri remain disabled.
            "default-src 'none'; style-src 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }
    redirect_headers = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
    }

    def approval_html(body: str, *, status_code: int) -> HTMLResponse:
        return HTMLResponse(body, status_code=status_code, headers=html_headers)

    def approval_redirect(target: str) -> RedirectResponse:
        return RedirectResponse(target, status_code=302, headers=redirect_headers)

    @mcp.custom_route("/approve", methods=["GET", "POST"], include_in_schema=False)
    async def approval_page(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        form: Any = None
        if request.method == "POST":
            form = await request.form()
            request_id = str(form.get("request", ""))
        pending = await provider.pending(request_id)
        if pending is None:
            if request.method == "POST":
                action = str(form.get("action", "approve"))
                candidate = str(form.get("pin", ""))
                if action == "approve" and provider.pin_matches(candidate):
                    target = await provider.approve(request_id)
                    if target is not None:
                        limiter.clear(request_id)
                        return approval_redirect(target)
            return approval_html("<h1>Invalid or expired authorization request</h1>", status_code=400)

        client = await provider.get_client(pending.client_id)
        client_name = client.client_name if client is not None and client.client_name else pending.client_id
        client_auth_method = client.token_endpoint_auth_method if client is not None else "unknown"
        error = ""

        if request.method == "POST":
            action = str(form.get("action", "approve"))
            if action == "deny":
                limiter.clear(request_id)
                target = await provider.deny(request_id)
                if target is None:
                    return approval_html("<h1>Authorization request expired</h1>", status_code=400)
                return approval_redirect(target)
            if not limiter.allowed(request_id):
                return approval_html("<h1>Too many failed PIN attempts for this request</h1>", status_code=429)
            candidate = str(form.get("pin", ""))
            if provider.pin_matches(candidate):
                limiter.clear(request_id)
                target = await provider.approve(request_id)
                if target is None:
                    return approval_html("<h1>Authorization request expired</h1>", status_code=400)
                return approval_redirect(target)
            limiter.fail(request_id)
            error = "Invalid PIN"

        scope_text = " ".join(pending.scopes)
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize dsh-mcp-gateway</title></head>
<body>
<h1>Authorize MCP client</h1>
<p><strong>Client name:</strong> {html.escape(client_name)}</p>
<p><strong>Client ID:</strong> {html.escape(pending.client_id)}</p>
<p><strong>Redirect URI:</strong> {html.escape(pending.redirect_uri)}</p>
<p><strong>Client auth:</strong> {html.escape(client_auth_method)}</p>
<p><strong>Scopes:</strong> {html.escape(scope_text)}</p>
<p><strong>Resource:</strong> {html.escape(pending.resource)}</p>
<p style="color:#b00020">{html.escape(error)}</p>
<form method="post" action="/approve">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<label>Admin PIN <input name="pin" type="password" autocomplete="current-password" autofocus></label>
<button type="submit" name="action" value="approve">Approve</button>
<button type="submit" name="action" value="deny">Deny</button>
</form>
</body></html>"""
        return approval_html(body, status_code=403 if error else 200)
