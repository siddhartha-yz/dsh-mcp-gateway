from __future__ import annotations

import asyncio
import hmac
import html
import json
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
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
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response


@dataclass(frozen=True, slots=True)
class EmbeddedOAuthConfig:
    issuer_url: str
    resource_url: str
    state_db: Path
    admin_pin: str
    scopes: tuple[str, ...] = ("dsh:control",)
    pending_ttl_s: int = 600
    code_ttl_s: int = 300
    access_token_ttl_s: int = 3600
    refresh_token_ttl_s: int = 30 * 24 * 3600

    def __post_init__(self) -> None:
        if not self.issuer_url:
            raise ValueError("issuer_url is required")
        object.__setattr__(self, "issuer_url", f"{self.issuer_url.rstrip('/')}/")
        if not self.resource_url:
            raise ValueError("resource_url is required")
        if len(self.admin_pin) < 6:
            raise ValueError("admin_pin must contain at least 6 characters")
        if not self.scopes or any(not scope for scope in self.scopes):
            raise ValueError("at least one non-empty OAuth scope is required")
        for name in ("pending_ttl_s", "code_ttl_s", "access_token_ttl_s", "refresh_token_ttl_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


class GatewayRefreshToken(RefreshToken):
    resource: str | None = None


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
                    subject TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS access_tokens (
                    token TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL
                );
                """
            )
            connection.commit()
            self._initialized = True

    def save_client(self, client: OAuthClientInformationFull) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO oauth_clients(client_id, client_json) VALUES (?, ?)",
                (client.client_id, client.model_dump_json()),
            )

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
            row = db.execute(
                "SELECT * FROM pending_authorizations WHERE request_id = ? AND expires_at >= ?",
                (request_id, now),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            db.execute("DELETE FROM pending_authorizations WHERE request_id = ?", (request_id,))
            db.execute(
                """
                INSERT INTO authorization_codes(
                    code, client_id, scopes_json, expires_at, code_challenge,
                    redirect_uri, redirect_uri_explicit, resource, subject
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def delete_pending(self, request_id: str) -> PendingAuthorization | None:
        pending = self.load_pending(request_id)
        if pending is None:
            return None
        with self.connection() as db:
            db.execute("DELETE FROM pending_authorizations WHERE request_id = ?", (request_id,))
        return pending

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
        access_ttl_s: int,
        refresh_ttl_s: int,
    ) -> tuple[str, str, list[str], int, int, str, str] | None:
        now = int(time.time())
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM authorization_codes WHERE code = ? AND client_id = ? AND expires_at >= ?",
                (code, client_id, now),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            db.execute("DELETE FROM authorization_codes WHERE code = ?", (code,))
            scopes = list(json.loads(row["scopes_json"]))
            access_exp = now + access_ttl_s
            refresh_exp = now + refresh_ttl_s
            db.execute(
                "INSERT INTO access_tokens VALUES (?, ?, ?, ?, ?, ?)",
                (access, client_id, json.dumps(scopes), access_exp, row["resource"], row["subject"]),
            )
            db.execute(
                "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?, ?, ?)",
                (refresh, client_id, json.dumps(scopes), refresh_exp, row["resource"], row["subject"]),
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
            db.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
            access_exp = now + access_ttl_s
            refresh_exp = now + refresh_ttl_s
            db.execute(
                "INSERT INTO access_tokens VALUES (?, ?, ?, ?, ?, ?)",
                (new_access, client_id, json.dumps(scopes), access_exp, row["resource"], row["subject"]),
            )
            db.execute(
                "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?, ?, ?)",
                (new_refresh, client_id, json.dumps(scopes), refresh_exp, row["resource"], row["subject"]),
            )
            db.commit()
        return new_access, new_refresh, access_exp, refresh_exp, row["resource"], row["subject"]

    def revoke(self, token: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM access_tokens WHERE token = ?", (token,))
            db.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))


class EmbeddedOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, GatewayRefreshToken, AccessToken]):
    def __init__(self, config: EmbeddedOAuthConfig) -> None:
        self.config = config
        self.store = OAuthStore(config.state_db)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await asyncio.to_thread(self.store.load_client, client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await asyncio.to_thread(self.store.save_client, client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        resource = params.resource or self.config.resource_url
        if resource != self.config.resource_url:
            raise AuthorizeError(error="invalid_target", error_description="unknown OAuth resource")
        scopes = tuple(params.scopes or self.config.scopes)
        if not set(scopes).issubset(self.config.scopes):
            raise AuthorizeError(error="invalid_scope", error_description="requested scope is not allowed")
        request_id = secrets.token_urlsafe(24)
        await asyncio.to_thread(
            self.store.save_pending,
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
        if row is None or row["resource"] != self.config.resource_url:
            return None
        return GatewayRefreshToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=list(json.loads(row["scopes_json"])),
            expires_at=int(row["expires_at"]),
            subject=row["subject"],
            resource=row["resource"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: GatewayRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
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
        if row is None or row["resource"] != self.config.resource_url:
            return None
        return AccessToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=list(json.loads(row["scopes_json"])),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["subject"],
            claims={"iss": self.config.issuer_url},
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
        if result is None:
            return None
        pending, code = result
        return construct_redirect_uri(
            pending.redirect_uri,
            code=code,
            state=pending.state,
            iss=self.config.issuer_url,
        )

    async def deny(self, request_id: str) -> str | None:
        pending = await asyncio.to_thread(self.store.delete_pending, request_id)
        if pending is None:
            return None
        return construct_redirect_uri(
            pending.redirect_uri,
            error="access_denied",
            state=pending.state,
            iss=self.config.issuer_url,
        )


class PinAttemptLimiter:
    def __init__(
        self,
        *,
        limit: int = 5,
        global_limit: int = 20,
        window_s: float = 300.0,
    ) -> None:
        if limit <= 0 or global_limit <= 0 or window_s <= 0:
            raise ValueError("PIN attempt limits and window must be positive")
        self.limit = limit
        self.global_limit = global_limit
        self.window_s = window_s
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._global_failures: deque[float] = deque()

    def _prune(self, failures: deque[float], now: float) -> None:
        while failures and now - failures[0] > self.window_s:
            failures.popleft()

    def allowed(self, source: str) -> bool:
        now = time.monotonic()
        with self._lock:
            failures = self._failures[source]
            self._prune(failures, now)
            self._prune(self._global_failures, now)
            return len(failures) < self.limit and len(self._global_failures) < self.global_limit

    def fail(self, source: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._failures[source].append(now)
            self._global_failures.append(now)

    def clear(self, source: str) -> None:
        with self._lock:
            self._failures.pop(source, None)


def install_approval_route(mcp: Any, provider: EmbeddedOAuthProvider) -> None:
    limiter = PinAttemptLimiter()

    @mcp.custom_route("/approve", methods=["GET", "POST"], include_in_schema=False)
    async def approval_page(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        form: Any = None
        if request.method == "POST":
            form = await request.form()
            request_id = str(form.get("request", ""))
        pending = await provider.pending(request_id)
        if pending is None:
            return HTMLResponse("<h1>Invalid or expired authorization request</h1>", status_code=400)

        client = await provider.get_client(pending.client_id)
        client_name = client.client_name if client is not None and client.client_name else pending.client_id
        source = request.client.host if request.client is not None else "unknown"
        error = ""

        if request.method == "POST":
            action = str(form.get("action", "approve"))
            if action == "deny":
                target = await provider.deny(request_id)
                if target is None:
                    return HTMLResponse("<h1>Authorization request expired</h1>", status_code=400)
                return RedirectResponse(target, status_code=302)
            if not limiter.allowed(source):
                return HTMLResponse("<h1>Too many failed PIN attempts</h1>", status_code=429)
            candidate = str(form.get("pin", ""))
            if provider.pin_matches(candidate):
                limiter.clear(source)
                target = await provider.approve(request_id)
                if target is None:
                    return HTMLResponse("<h1>Authorization request expired</h1>", status_code=400)
                return RedirectResponse(target, status_code=302)
            limiter.fail(source)
            error = "Invalid PIN"

        scope_text = " ".join(pending.scopes)
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize dsh-mcp-gateway</title></head>
<body>
<h1>Authorize MCP client</h1>
<p><strong>Client:</strong> {html.escape(client_name)}</p>
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
        return HTMLResponse(body, status_code=403 if error else 200, headers={"Cache-Control": "no-store"})
