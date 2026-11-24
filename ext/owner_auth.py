"""Built-in sign-in: a single-owner OAuth 2.1 server inside this process.

The Python server is both the authorization server and the resource server,
on mcp 2.x's mcp.server.auth. With auth=build(config).settings and
auth_server_provider=build(config).provider, the SDK serves:

    GET      /.well-known/oauth-authorization-server     AS metadata (RFC 8414)
    GET      /.well-known/oauth-protected-resource/mcp   resource metadata (RFC 9728)
    GET|POST /authorize    checks the client, redirect URI, scope and PKCE fields,
                           then calls OwnerAuthProvider.authorize()
    POST     /token        client authentication, code expiry, redirect URI, PKCE
    POST     /register     dynamic client registration (RFC 7591)
    POST     /revoke       RFC 7009
    *        /mcp          a bearer token is required: 401 + WWW-Authenticate otherwise

This module adds what the SDK leaves to the provider:

    /consent               the owner's page: approve with OWNER_SECRET, or deny
    /.well-known/oauth-protected-resource   the resource metadata at the root path too
    redirect allowlist     the claude.ai and claude.com callbacks, ALLOWED_REDIRECT_URIS,
                           and http loopback (127.0.0.1, localhost, [::1]) on any port
    RegistrationFilter     narrows what Claude asks for at /register to what this server
                           grants (two grants, the offline_access scope, client_secret_post)
    resource binding       every code and token is bound to <PUBLIC_URL>/mcp (RFC 8707)
    tokens                 stored as SHA-256 hashes in auth_store; single-use codes;
                           refresh tokens rotate on every use, and a reused one revokes
                           its grant, except for a 30 s idempotent replay window
                           (kept in memory only: after a restart a reuse revokes)
    static bearer          the optional MCP_BEARER_TOKEN header, for Claude Code or CI
    OriginCheck            /mcp refuses (403) a request whose Origin header is not
                           PUBLIC_URL's, Claude's web apps' or in ALLOWED_ORIGINS
    IssuerParameter        RFC 9207: `iss` on every authorization response, and
                           authorization_response_iss_parameter_supported in the metadata

Nothing here logs a secret: not OWNER_SECRET, MCP_BEARER_TOKEN, codes, tokens,
client secrets, state, PKCE values or pending ids.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import inspect
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

from mcp.server.auth.json_response import PydanticJSONResponse
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
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken, ProtectedResourceMetadata
from pydantic import AnyHttpUrl, AnyUrl, PrivateAttr, ValidationError
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import auth_config
import auth_store
from auth_config import AuthConfig
from auth_store import AuthStore, hash_secret

log = logging.getLogger("garmin_ext.auth")
SUBJECT = "owner"
STATIC_CLIENT_ID = "static-bearer"

REGISTRATION_MAX_BODY = 65536
CONSENT_MAX_BODY = 8192
CONSENT_MAX_FIELDS = 10
CLIENT_NAME_MAX = 80
LOG_VALUE_MAX = 120
REPLAY_MAX_ENTRIES = 1024
LOOPBACK_URL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})   # as pydantic spells them

REDIRECT_HELP = ("allowed: the claude.ai or claude.com callback, ALLOWED_REDIRECT_URIS, "
                 "or http://127.0.0.1|localhost|[::1] on any port")
REDIRECT_NO_LONGER_ALLOWED = "redirect_uri is registered but no longer allowed; " + REDIRECT_HELP
CONSENT_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"   # no form-action
SELFCHECK_PATHS = (
    "/authorize", "/token", "/register", "/revoke",
    "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp",
    auth_config.MCP_PATH, auth_config.CONSENT_PATH, auth_config.ROOT_PRM_PATH,
)

_S256_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")


# --- small helpers ------------------------------------------------------------------


def _same_secret(given: str, expected: str) -> bool:
    """Constant-time compare. Digests, because hmac.compare_digest refuses a str with
    non-ASCII characters (Starlette decodes headers as latin-1)."""
    return hmac.compare_digest(
        hashlib.sha256(given.encode("utf-8", "surrogateescape")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def _canonical(value: Optional[str]) -> Optional[str]:
    """The URL form the SDK's audience check compares (bearer_auth.py), or None."""
    try:
        return str(AnyHttpUrl(value or "")).removesuffix("/")
    except (ValidationError, ValueError, TypeError):
        return None


def _split(scopes: Optional[str]) -> List[str]:
    return scopes.split() if scopes else []


def _is_loopback(url: AnyUrl) -> bool:
    return url.scheme == "http" and (url.host or "").lower() in LOOPBACK_URL_HOSTS


def _loopback_key(url: AnyUrl) -> tuple:
    # Everything but the port. A registered "http://localhost" has an empty path
    # and a requested "http://localhost:5555" has "/": the same here.
    return (url.scheme, (url.host or "").lower(), url.path or "/", url.query, url.fragment,
            url.username, url.password)


def _normal(url: Any) -> Optional[str]:
    """A URL in pydantic's default spelling, where an empty path is "/", or None.
    The SDK's client model keeps a registered "https://app.example.com" without
    the "/", while /authorize and /token parse the requested one with it."""
    try:
        return str(AnyUrl(str(url)))
    except ValidationError:
        return None


def _host_of(uri: str) -> str:
    """host[:port] of a URL, never its user info: for logs and the consent page."""
    try:
        return urlsplit(uri).netloc.rpartition("@")[2] or "?"
    except ValueError:
        return "?"


def _plain(text: str, limit: int) -> str:
    """Untrusted text for one line: control and format characters (newlines, bidi
    overrides) become spaces, runs of spaces collapse, cut to `limit` characters."""
    cleaned = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in text)
    return " ".join(cleaned.split())[:limit]


def _shown_name(name: Any) -> str:
    return _plain(name, CLIENT_NAME_MAX) if isinstance(name, str) and name.strip() else "unnamed"


def _cut(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= LOG_VALUE_MAX else text[:LOG_VALUE_MAX - 3] + "..."


def _exact_redirects(config: AuthConfig) -> frozenset:
    """The exact https redirect URIs, in pydantic's spelling (case, default port)."""
    allowed = set()
    for n, uri in enumerate(auth_config.CLAUDE_CALLBACKS + tuple(config.extra_redirect_uris), 1):
        try:
            allowed.add(str(AnyUrl(uri)))
        except ValidationError:
            # auth_config checked the shape; pydantic is stricter about hosts.
            log.warning("sign-in: ALLOWED_REDIRECT_URIS entry %d is not a valid URL; ignored",
                        n - len(auth_config.CLAUDE_CALLBACKS))
    return frozenset(allowed)


# --- clients --------------------------------------------------------------------------


class LoopbackAwareClient(OAuthClientInformationFull):
    """What get_client() returns. The SDK calls these two methods itself at
    /authorize (handlers/authorize.py), so no SDK patch is needed.

    get_client() also hands it the provider's redirect_allowed(): a URI the
    client registered while ALLOWED_REDIRECT_URIS listed it is refused once the
    entry is gone, the same way as a URI it never registered (a 400, no redirect)."""

    _redirect_allowed: Optional[Callable[[AnyUrl], bool]] = PrivateAttr(default=None)

    def validate_redirect_uri(self, redirect_uri: Optional[AnyUrl]) -> AnyUrl:
        chosen = self._registered_redirect(redirect_uri)
        if self._redirect_allowed is not None and not self._redirect_allowed(chosen):
            raise InvalidRedirectUriError(REDIRECT_NO_LONGER_ALLOWED)
        return chosen

    def _registered_redirect(self, redirect_uri: Optional[AnyUrl]) -> AnyUrl:
        # RFC 8252 section 7.3: a native client (Claude Code) picks its port at run
        # time, so a registered loopback URI matches on any port. The code is bound
        # to the URI actually requested, and /token compares against that string.
        if redirect_uri is not None and _is_loopback(redirect_uri):
            wanted = _loopback_key(redirect_uri)
            for registered in self.redirect_uris or []:
                if _is_loopback(registered) and _loopback_key(registered) == wanted:
                    return redirect_uri
        # Any other URI must match a registered one exactly, compared in the same
        # spelling (an empty path is "/", as for loopback), so an
        # ALLOWED_REDIRECT_URIS entry like "https://app.example.com" works too.
        if redirect_uri is not None:
            wanted = _normal(redirect_uri)
            for registered in self.redirect_uris or []:
                if wanted is not None and _normal(registered) == wanted:
                    return redirect_uri
        return super().validate_redirect_uri(redirect_uri)

    def validate_scope(self, requested_scope: Optional[str]) -> Optional[List[str]]:
        # One owner, one all-or-nothing grant: scopes mean nothing here, and
        # claude.ai has been seen asking for "claudeai". Accept and echo.
        if requested_scope is None:
            return None
        return [s for s in requested_scope.split(" ") if s]


# --- the provider -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Replay:
    """What a refresh rotation returned, kept in memory only for REFRESH_REUSE_GRACE."""

    token: OAuthToken
    access_expires_at: float
    client_id: str
    rotated_at: float
    refresh_hash: str


class OwnerAuthProvider:
    """OAuthAuthorizationServerProvider for one owner (SDK/server/auth/provider.py:146;
    structural typing, no subclassing). Records live in AuthStore."""

    def __init__(self, config: AuthConfig, store: AuthStore) -> None:
        self.config = config
        self.store = store
        self._allowed_redirects = _exact_redirects(config)
        # Guards refresh rotation together with the replay map; the
        # store has its own lock per operation. Nothing awaits while holding it.
        self._lock = threading.Lock()
        self._replay: "OrderedDict[str, _Replay]" = OrderedDict()

    @property
    def clock(self) -> Callable[[], float]:
        return self.store.clock

    def _revoke(self, grant_id: str, reason: str) -> None:
        client_id = self.store.revoke_grant(grant_id, reason)
        if client_id is not None:
            log.warning("token: grant revoked for client %s (%s)", client_id, reason)

    # registration

    def redirect_allowed(self, uri: AnyUrl) -> bool:
        """Exact https allowlist, or http loopback on any port and path; never a
        fragment or user info."""
        if uri.fragment is not None or uri.username or uri.password:
            return False
        if _is_loopback(uri):
            return True
        try:
            return str(AnyUrl(str(uri))) in self._allowed_redirects
        except ValidationError:
            return False

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        info = self.store.get_client(client_id)
        if info is None:
            return None
        try:
            client = LoopbackAwareClient.model_validate_json(info)
        except ValidationError:
            # Never let the error (it quotes the record, client secret included) reach a log.
            return None
        client._redirect_allowed = self.redirect_allowed   # today's allowlist, not the one at registration
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = client_info.redirect_uris or []
        if not uris or not all(self.redirect_allowed(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri", REDIRECT_HELP)
        if not set(client_info.grant_types or []) <= set(auth_config.GRANT_TYPES):
            raise RegistrationError("invalid_client_metadata",
                                    "grant_types: only authorization_code and refresh_token")
        try:
            evicted = self.store.add_client(client_info.client_id, client_info.model_dump_json(),
                                            max_clients=auth_config.MAX_CLIENTS,
                                            evict_idle_days=auth_config.EVICT_IDLE_DAYS,
                                            pending_ttl=auth_config.PENDING_TTL)
        except auth_store.ClientLimitError:
            raise RegistrationError("invalid_client_metadata", "too many registered clients; try again later") from None
        for gone in evicted:
            for _ in gone["revoked_grants"]:
                log.warning("token: grant revoked for client %s (%s)", gone["client_id"], auth_store.REASON_EVICTED)
            log.info("dcr: evicted idle client %s", gone["client_id"])
        stored, _ = self.store.counts()
        log.info("dcr: registered client %s (%s), auth method %s, %d clients stored", client_info.client_id,
                 _shown_name(client_info.client_name), client_info.token_endpoint_auth_method, stored)

    # authorization

    def _bind_resource(self, requested: Optional[str]) -> str:
        if requested is None:
            return self.config.resource_url   # clients SHOULD send it; bind to us anyway
        canonical = _canonical(requested)
        if canonical is None or canonical != _canonical(self.config.resource_url):
            raise AuthorizeError("invalid_target", "unknown resource")
        return self.config.resource_url

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        resource = self._bind_resource(params.resource)
        if not _S256_CHALLENGE.fullmatch(params.code_challenge or ""):
            raise AuthorizeError("invalid_request", "code_challenge must be a 43-character S256 value")
        scopes = " ".join(params.scopes or [])
        if (params.state is not None and len(params.state) > auth_config.MAX_STATE_LEN) \
                or len(scopes) > auth_config.MAX_SCOPE_LEN:
            raise AuthorizeError("invalid_request", "state or scope too long")
        pending = secrets.token_urlsafe(32)
        stored = self.store.add_pending(
            pending_hash=hash_secret(pending), client_id=client.client_id, redirect_uri=str(params.redirect_uri),
            redirect_uri_explicit=params.redirect_uri_provided_explicitly, state=params.state,
            code_challenge=params.code_challenge, scopes=scopes, resource=resource,
            ttl=auth_config.PENDING_TTL, max_pending=auth_config.MAX_PENDING,
            max_per_client=auth_config.MAX_PENDING_PER_CLIENT)
        if not stored:   # never delete another client's sign-in to make room
            raise AuthorizeError("temporarily_unavailable", "too many sign-ins in progress; try again in a few minutes")
        log.info("authorize: client %s, redirect host %s", client.client_id, _host_of(str(params.redirect_uri)))
        return f"{self.config.consent_url}?req={pending}"

    # codes

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> Optional[AuthorizationCode]:
        row = self.store.get_code(hash_secret(authorization_code))
        if row is None or row["client_id"] != client.client_id:
            return None
        if row["used_at"] is not None:
            # RFC 6749 section 4.1.2: a code used twice; revoke what it produced.
            if row["grant_id"]:
                self._revoke(row["grant_id"], auth_store.REASON_CODE_REUSE)
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=_split(row["scopes"]),
            expires_at=row["expires_at"],   # the SDK checks expiry; exchange checks it again
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
            subject=SUBJECT,
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        access, refresh = "at_" + secrets.token_urlsafe(32), "rt_" + secrets.token_urlsafe(32)
        result = self.store.exchange_code(
            code_hash=hash_secret(authorization_code.code), client_id=client.client_id,
            grant_id=secrets.token_hex(16), access_hash=hash_secret(access), refresh_hash=hash_secret(refresh),
            access_ttl=auth_config.ACCESS_TTL, refresh_ttl=auth_config.REFRESH_TTL)
        if not result["ok"]:
            if result["revoked"]:   # lost a race with another exchange of the same code
                log.warning("token: grant revoked for client %s (%s)", result["revoked"][1],
                            auth_store.REASON_CODE_REUSE)
            if result["reason"] == "expired":
                raise TokenError("invalid_grant", "authorization code has expired")
            raise TokenError("invalid_grant", "authorization code already used")
        log.info("token: code exchanged for client %s (auth method %s)", client.client_id,
                 client.token_endpoint_auth_method)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=auth_config.ACCESS_TTL,
                          scope=result["scopes"] or None, refresh_token=refresh)

    # refresh tokens: rotation with an idempotent replay window

    def _replay_prune(self, now: float) -> None:
        while self._replay:
            oldest = next(iter(self._replay.values()))
            if now - oldest.rotated_at <= auth_config.REFRESH_REUSE_GRACE:
                break
            self._replay.popitem(last=False)

    def _replay_for(self, token_hash: str, client_id: str) -> Optional[_Replay]:
        """The pair a rotation of this token returned, if a retry may get it again:
        at most REFRESH_REUSE_GRACE old, the same client, the new refresh token still
        unused and its grant not revoked. Call with self._lock held."""
        now = self.clock()
        self._replay_prune(now)
        entry = self._replay.get(token_hash)
        if entry is None or entry.client_id != client_id:
            return None
        if now - entry.rotated_at > auth_config.REFRESH_REUSE_GRACE:
            return None
        if not self.store.refresh_unused(entry.refresh_hash):
            return None
        return entry

    def _replay_put(self, token_hash: str, entry: _Replay) -> None:
        self._replay_prune(entry.rotated_at)
        self._replay[token_hash] = entry
        self._replay.move_to_end(token_hash)
        while len(self._replay) > REPLAY_MAX_ENTRIES:
            self._replay.popitem(last=False)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[RefreshToken]:
        token_hash = hash_secret(refresh_token)
        with self._lock:
            row = self.store.get_token(token_hash, "refresh")
            if row is None or row["client_id"] != client.client_id:
                return None
            if row["revoked_at"] is not None or row["expires_at"] <= self.clock():
                return None
            if row["used_at"] is not None and self._replay_for(token_hash, client.client_id) is None:
                # A rotated-away refresh token came back: a copy exists somewhere.
                self._revoke(row["grant_id"], auth_store.REASON_REFRESH_REUSE)
                return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=_split(row["scopes"]),
                            expires_at=int(row["expires_at"]), resource=row["resource"], subject=SUBJECT)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: List[str]) -> OAuthToken:
        old_hash = hash_secret(refresh_token.token)
        access, refresh = "at_" + secrets.token_urlsafe(32), "rt_" + secrets.token_urlsafe(32)
        access_scopes = " ".join(scopes)
        # The rotation, its inserts and the map entry happen together,
        # with no await, so a concurrent duplicate sees an unused token or the entry.
        with self._lock:
            rotated = self.store.rotate_refresh(
                old_hash=old_hash, client_id=client.client_id, access_hash=hash_secret(access),
                refresh_hash=hash_secret(refresh), access_scopes=access_scopes,
                access_ttl=auth_config.ACCESS_TTL, refresh_ttl=auth_config.REFRESH_TTL)
            now = self.clock()
            if rotated is not None:
                token = OAuthToken(access_token=access, token_type="Bearer", expires_in=auth_config.ACCESS_TTL,
                                   scope=access_scopes or None, refresh_token=refresh)
                self._replay_put(old_hash, _Replay(token, rotated["access_expires_at"], client.client_id, now,
                                                   hash_secret(refresh)))
                replay = None
            else:
                replay = self._replay_for(old_hash, client.client_id)
                if replay is None:
                    row = self.store.get_token(old_hash, "refresh")
                    if row is not None and row["client_id"] == client.client_id and row["used_at"] is not None:
                        self._revoke(row["grant_id"], auth_store.REASON_REFRESH_REUSE)
                    raise TokenError("invalid_grant", "refresh token is no longer valid")
        if replay is None:
            log.info("token: refresh for client %s", client.client_id)
            return token
        log.warning("token: refresh token reused within %ds; returned the same pair for client %s",
                    int(now - replay.rotated_at), client.client_id)
        remaining = max(int(replay.access_expires_at - now), 0)
        return replay.token.model_copy(update={"expires_in": remaining})

    # access tokens and revocation

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        static = self.config.static_bearer
        if static is not None and _same_secret(token, static):
            # The opt-in header bearer. Header only: the SDK's BearerAuthBackend
            # reads nothing but the Authorization header.
            return AccessToken(token=token, client_id=STATIC_CLIENT_ID, scopes=[], expires_at=None,
                               resource=self.config.resource_url, subject=SUBJECT)
        row = self.store.get_token(hash_secret(token), "access")
        if row is None or row["revoked_at"] is not None or row["expires_at"] <= self.clock():
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=_split(row["scopes"]),
                           expires_at=int(row["expires_at"]), resource=row["resource"] or None, subject=SUBJECT)

    async def revoke_token(self, token: Any) -> None:
        if getattr(token, "client_id", None) == STATIC_CLIENT_ID:
            return   # the static bearer is rotated in Railway, not here
        static = self.config.static_bearer
        if static is not None and _same_secret(token.token, static):
            return
        revoked = self.store.revoke_token_grant(hash_secret(token.token), auth_store.REASON_CLIENT)
        if revoked is not None:
            log.warning("token: grant revoked for client %s (%s)", revoked[1], auth_store.REASON_CLIENT)

    async def exchange_identity_assertion(self, client: OAuthClientInformationFull, params: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "The JWT bearer grant is not supported by this server")


# --- wiring ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerAuth:
    config: AuthConfig
    settings: AuthSettings
    provider: "OwnerAuthProvider"


def build(config: AuthConfig, *, clock: Callable[[], float] = time.time) -> OwnerAuth:
    """Open the sign-in database and make the SDK settings and the provider.
    `clock` is for tests (a fake clock that starts at time.time())."""
    store = AuthStore(config.db_path, clock=clock)
    try:
        if store.owner_secret_changed(config.owner_secret):
            log.warning("sign-in: the owner secret changed; every sign-in was revoked")
        settings = AuthSettings(
            issuer_url=config.public_url,
            resource_server_url=config.resource_url,
            validate_token_resource=True,   # unset means False plus a warning (SDK/server/auth/settings.py)
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=list(auth_config.VALID_SCOPES),
                default_scopes=list(auth_config.VALID_SCOPES)),   # no client secret expiry (RG-3)
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=None,
        )
        if str(settings.issuer_url) != config.public_url or str(settings.resource_server_url) != config.resource_url:
            raise RuntimeError(f"owner_auth: the SDK spells PUBLIC_URL {config.public_url} as {settings.issuer_url}; "
                               "set PUBLIC_URL to that spelling")
        provider = OwnerAuthProvider(config, store)
        clients, grants = store.counts()
        log.info("sign-in: database ready (%d clients, %d live grants)", clients, grants)
        return OwnerAuth(config=config, settings=settings, provider=provider)
    except BaseException:
        store.close()
        raise


def register_routes(app: Any, auth: OwnerAuth) -> None:
    """Add GET/POST /consent and the root resource metadata with app.custom_route.
    The SDK mounts custom routes after its own (lowlevel/server.py), so these
    cannot shadow /authorize, /token, /register, /revoke, /mcp or its documents."""
    app.custom_route(auth_config.CONSENT_PATH, methods=["GET", "POST"])(_consent_endpoint(auth))
    app.custom_route(auth_config.ROOT_PRM_PATH, methods=["GET"])(_root_resource_metadata(auth))


def asgi_middleware(auth: OwnerAuth) -> List[Tuple[type, dict]]:
    """The ASGI middleware this sign-in needs, for mcp2_compat.set_asgi_middleware (first = outermost)."""
    return [
        (RegistrationFilter, {"allowed_grants": auth_config.GRANT_TYPES,
                              "valid_scopes": auth_config.VALID_SCOPES,
                              "max_body": REGISTRATION_MAX_BODY}),
        (OriginCheck, {"allowed": auth.config.allowed_origins, "enabled": auth.config.origin_check}),
        (IssuerParameter, {"issuer": auth.config.public_url, "consent_url": auth.config.consent_url}),
    ]


def _root_resource_metadata(auth: OwnerAuth) -> Callable:
    # The AuthSettings URLs keep their exact spelling (no trailing slash), so
    # this JSON equals the SDK's document at .../oauth-protected-resource/mcp.
    metadata = ProtectedResourceMetadata(resource=auth.settings.resource_server_url,
                                         authorization_servers=[auth.settings.issuer_url])

    async def root_resource_metadata(request: Request) -> Response:
        return PydanticJSONResponse(content=metadata, headers={"Cache-Control": "public, max-age=3600"})

    return root_resource_metadata


# --- the consent page -------------------------------------------------------------------

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CONSENT_CSP,
}
_REDIRECT_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}

_STYLE = (
    "body{font:16px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;max-width:34rem;margin:2rem auto;"
    "padding:0 1rem;color:#1b1b1b;background:#fff}"
    "h1{font-size:1.4rem;margin:0 0 1rem}"
    ".warn{background:#fff4cc;border:1px solid #d9b84a;padding:.6rem .8rem;border-radius:.4rem}"
    ".error{color:#a30000;font-weight:600}.small{color:#555;font-size:.9rem}"
    "label{display:block;margin:.9rem 0 .25rem;font-weight:600}"
    "input{width:100%;box-sizing:border-box;padding:.55rem;font-size:1rem}"
    "button{padding:.6rem 1.3rem;margin:1.1rem .6rem 0 0;font-size:1rem;cursor:pointer}"
    "@media (prefers-color-scheme:dark){body{color:#eee;background:#151515}"
    ".warn{background:#3a3214;border-color:#7d6a28}.error{color:#ff8a8a}.small{color:#aaa}}"
)


def _page(title: str, body: str, status: int) -> HTMLResponse:
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=dict(_PAGE_HEADERS))


def _expired_page() -> HTMLResponse:
    return _page("Sign-in link expired",
                 "<p>This sign-in link expired or was already used. Start again from your Claude app.</p>", 400)


def _message_page(status: int, title: str, text: str) -> HTMLResponse:
    return _page(title, f"<p>{html.escape(text)}</p>", status)


def _redirect(url: str) -> RedirectResponse:
    # 303 after a POST that carried a credential (RFC 9700 section 4.12).
    return RedirectResponse(url, status_code=303, headers=dict(_REDIRECT_HEADERS))


def _started(seconds: float) -> str:
    minutes = int(max(seconds, 0) // 60)
    if minutes == 0:
        return "Started less than a minute ago."
    return f"Started {minutes} minute{'' if minutes == 1 else 's'} ago."


def _client_name(store: AuthStore, client_id: str) -> Tuple[bool, Optional[str]]:
    """(client exists, its client_name) without parsing secrets into any error."""
    info = store.get_client(client_id)
    if info is None:
        return False, None
    try:
        name = json.loads(info).get("client_name")
    except (ValueError, AttributeError):
        return True, None
    return True, name if isinstance(name, str) else None


def _form_page(auth: OwnerAuth, pending_id: str, row: Dict, client_name: Optional[str],
               error: Optional[str] = None, status: int = 200) -> HTMLResponse:
    redirect_uri = row["redirect_uri"]
    target = _host_of(redirect_uri)
    try:
        loopback = _is_loopback(AnyUrl(redirect_uri))
    except ValidationError:
        loopback = False
    if loopback:
        where = (f'<p class="warn">This sign-in returns to a program on this computer (<b>{html.escape(target)}</b>), '
                 "such as Claude Code. Approve only if you started it here just now.</p>")
    else:
        where = f"<p>After you approve, your browser goes back to <b>{html.escape(target)}</b>.</p>"
    name = _plain(client_name, CLIENT_NAME_MAX) if client_name and client_name.strip() else "an app with no name"
    started = _started(auth.provider.clock() - row["created_at"])
    alert = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    body = (
        f"<p>You are signing in to <b>{html.escape(auth.config.host)}</b>.</p>"
        f"<p>App: <b>{html.escape(name)}</b> (not verified)</p>"
        f"{where}"
        f"<p>{started} This gives read-only access to your Garmin data on this server.</p>"
        "<p>Approve only a sign-in you started yourself just now. If someone sent you this link, press Deny.</p>"
        f"{alert}"
        f'<form method="post" action="{html.escape(auth_config.CONSENT_PATH)}">'
        f'<input type="hidden" name="req" value="{html.escape(pending_id)}">'
        '<label for="username">Username</label>'
        f'<input id="username" name="username" value="{SUBJECT}" autocomplete="username" readonly>'
        '<label for="secret">Owner secret</label>'
        '<input id="secret" type="password" name="secret" autocomplete="current-password" required autofocus>'
        '<button type="submit" name="action" value="approve">Approve</button>'
        '<button type="submit" name="action" value="deny" formnovalidate>Deny</button>'
        "</form>"
        '<p class="small">The owner secret is set on the server as OWNER_SECRET.</p>'
    )
    return _page("Sign in", body, status)


def _consent_endpoint(auth: OwnerAuth) -> Callable:
    store = auth.provider.store

    async def consent(request: Request) -> Response:
        if request.method in ("GET", "HEAD"):
            return _consent_get(auth, store, request)
        return await _consent_post(auth, store, request)

    return consent


def _consent_get(auth: OwnerAuth, store: AuthStore, request: Request) -> Response:
    pending_id = request.query_params.get("req") or ""
    row = store.get_pending(hash_secret(pending_id)) if pending_id else None
    if row is None:
        return _expired_page()
    exists, name = _client_name(store, row["client_id"])
    if not exists:
        return _expired_page()
    return _form_page(auth, pending_id, row, name)


async def _consent_post(auth: OwnerAuth, store: AuthStore, request: Request) -> Response:
    # 1. Size: custom routes get no SDK body limit (Caddy also caps /consent at 16 KB).
    declared = request.headers.get("content-length")
    if declared is not None and declared.strip().isdigit() and int(declared) > CONSENT_MAX_BODY:
        return _message_page(413, "Request too large", "The form was too large.")
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > CONSENT_MAX_BODY:
                return _message_page(413, "Request too large", "The form was too large.")
    except ClientDisconnect:
        # The browser went away mid-form: nobody reads this answer, and a
        # traceback in the log would help no one.
        return _message_page(400, "Bad request", "The form did not arrive in full.")
    # 2. Only a plain HTML form post.
    media_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media_type != "application/x-www-form-urlencoded":
        return _message_page(400, "Bad request", "Send the sign-in form from the page.")
    # 3. A browser marks a form posted from another site.
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() not in ("same-origin", "none"):
        return _message_page(403, "Refused", "A sign-in form posted from another site is refused.")
    # 4. Parse.
    try:
        fields = parse_qs(bytes(body).decode("utf-8", "surrogateescape"), keep_blank_values=True,
                          max_num_fields=CONSENT_MAX_FIELDS)
    except ValueError:
        return _message_page(400, "Bad request", "Send the sign-in form from the page.")

    def first(name: str) -> Optional[str]:
        values = fields.get(name)
        return values[0] if values else None

    pending_id, action, secret = first("req") or "", first("action"), first("secret") or ""
    pending_hash = hash_secret(pending_id)
    # 5. The request must still be waiting.
    row = store.get_pending(pending_hash) if pending_id else None
    if row is None:
        return _expired_page()
    # 6. Deny needs no secret.
    if action == "deny":
        taken = store.take_pending(pending_hash)
        if taken is None:
            return _expired_page()
        log.info("consent: denied for client %s", taken["client_id"])
        return _redirect(construct_redirect_uri(taken["redirect_uri"], error="access_denied", state=taken["state"],
                                                iss=auth.config.public_url))
    # 7. A scripted submit without the button still approves.
    if action is None and secret:
        action = "approve"
    if action != "approve":
        return _message_page(400, "Bad request", "Press Approve or Deny on the sign-in page.")
    # 8. Wrong secret: three strikes end the request and tell the app.
    if not _same_secret(secret, auth.config.owner_secret):
        counted = store.pending_failure(pending_hash, max_failures=auth_config.MAX_SECRET_FAILURES)
        if counted is None:
            return _expired_page()
        failures, failed_row, deleted = counted
        log.warning("consent: wrong owner secret (%d of %d)", failures, auth_config.MAX_SECRET_FAILURES)
        if deleted:
            return _redirect(construct_redirect_uri(failed_row["redirect_uri"], error="access_denied",
                                                    state=failed_row["state"], iss=auth.config.public_url))
        _, name = _client_name(store, failed_row["client_id"])
        return _form_page(auth, pending_id, failed_row, name, error="Wrong secret.", status=403)
    # 9. Approved: take the request and mint the code in one transaction.
    code = "ac_" + secrets.token_urlsafe(32)
    taken = store.approve_pending(pending_hash, code_hash=hash_secret(code), code_ttl=auth_config.CODE_TTL)
    if taken is None:
        return _expired_page()
    log.info("consent: approved for client %s", taken["client_id"])
    # The OAuth parameters come from the stored request, never from the form. iss (RFC 9207) is the
    # issuer exactly as the metadata names it: AuthSettings keeps PUBLIC_URL's spelling (build()).
    return _redirect(construct_redirect_uri(taken["redirect_uri"], code=code, state=taken["state"],
                                            iss=auth.config.public_url))


# --- /register body filter (ASGI) -------------------------------------------------------------


class RegistrationFilter:
    """Narrows a dynamic client registration to what this server grants, before
    the SDK validates it (RFC 7591 section 3.2.1 lets the server replace values):

    - grant_types: with authorization_code present, exactly authorization_code and
      refresh_token (claude.ai asks for the jwt-bearer grant too, which the SDK refuses)
    - token_endpoint_auth_method: client_secret_basic becomes client_secret_post
      (the SDK needs client_id in the /token form even with a Basic header)
    - scope: only valid scopes; none left means the field goes (default offline_access)

    A body over max_body bytes (counted as streamed) gets 413. A body that is
    not a JSON object passes unchanged (the SDK answers 400). One INFO line
    records what was asked for; the response (it holds the client secret) is
    never looked at.
    """

    def __init__(self, app: Any, *, allowed_grants=auth_config.GRANT_TYPES, valid_scopes=auth_config.VALID_SCOPES,
                 max_body: int = REGISTRATION_MAX_BODY) -> None:
        self.app = app
        self.allowed_grants = tuple(allowed_grants)
        self.valid_scopes = tuple(valid_scopes)
        self.max_body = int(max_body)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/register":
            await self.app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > self.max_body:
            await _registration_too_large(send)
            return
        body = bytearray()
        trailing = None
        complete = False
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                trailing = message
                break
            body.extend(message.get("body", b""))
            if len(body) > self.max_body:   # counted as streamed, not from Content-Length
                await _registration_too_large(send)
                return
            if not message.get("more_body", False):
                complete = True
                break
        if not complete:
            # The client went away mid-body: hand on what arrived, unchanged.
            queued: List[dict] = [{"type": "http.request", "body": bytes(body), "more_body": True}]
            if trailing is not None:
                queued.append(trailing)
            await self.app(scope, _replaying(receive, queued), send)
            return
        rewritten = self._rewrite(bytes(body))
        if rewritten is not None:
            body = bytearray(rewritten)
            scope = dict(scope)
            scope["headers"] = [(k, v) for k, v in scope.get("headers", [])
                                if k.lower() not in (b"content-length", b"transfer-encoding")]
            scope["headers"].append((b"content-length", str(len(body)).encode("ascii")))
        # Replay the body once, then pass receive() through so http.disconnect still arrives.
        await self.app(scope, _replaying(receive, [{"type": "http.request", "body": bytes(body), "more_body": False}]),
                       send)

    def _rewrite(self, body: bytes) -> Optional[bytes]:
        """The narrowed body, or None to pass the original unchanged."""
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):   # not UTF-8, not JSON, or nested too deep
            return None
        if not isinstance(data, dict):
            return None
        asked_grants = data.get("grant_types")
        asked_method = data.get("token_endpoint_auth_method")
        asked_scope = data.get("scope")
        dropped: List[str] = []
        changed: List[str] = []

        if isinstance(asked_grants, list) and "authorization_code" in asked_grants:
            grants = list(self.allowed_grants)   # exactly these two
            dropped += [f"grant {g}" for g in asked_grants if g not in grants]
            if asked_grants != grants:
                data["grant_types"] = grants
                if any(g not in asked_grants for g in grants):
                    changed.append("grant_types")
        if asked_method == "client_secret_basic":   # the SDK needs client_id in the /token form
            data["token_endpoint_auth_method"] = "client_secret_post"
            changed.append("client_secret_basic->client_secret_post")
        if isinstance(asked_scope, str):
            kept: List[str] = []
            for token in asked_scope.split():
                if token in self.valid_scopes:
                    if token not in kept:
                        kept.append(token)
                else:
                    dropped.append(f"scope {token}")
            if kept:
                data["scope"] = " ".join(kept)
            else:
                del data["scope"]   # the SDK then assigns the default scopes

        uris = data.get("redirect_uris")
        hosts = [_origin_of(u) for u in uris[:10]] if isinstance(uris, list) else None
        log.info("dcr: request client_name=%s auth_method=%s grant_types=%s scope=%s redirect_hosts=%s "
                 "dropped=%s rewritten=%s", _cut(data.get("client_name")), _cut(asked_method), _cut(asked_grants),
                 _cut(asked_scope), _cut(hosts), _cut(dropped), _cut(changed))
        try:
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            return None


def _origin_of(uri: Any) -> str:
    """scheme://host[:port] of a requested redirect URI, for the dcr log line."""
    if not isinstance(uri, str):
        return "?"
    try:
        parts = urlsplit(uri)
        host = parts.netloc.rpartition("@")[2]
    except ValueError:
        return "?"
    return f"{parts.scheme}://{host}" if host else f"{parts.scheme}:"


def _content_length(scope: dict) -> Optional[int]:
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _replaying(receive: Callable, messages: List[dict]) -> Callable:
    queue: Deque[dict] = deque(messages)

    async def replay() -> dict:
        if queue:
            return queue.popleft()
        return await receive()

    return replay


async def _registration_too_large(send: Callable) -> None:
    body = b'{"error":"invalid_client_metadata","error_description":"registration request too large"}'
    await send({"type": "http.response.start", "status": 413, "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]})
    await send({"type": "http.response.body", "body": body})


# --- the Origin check on /mcp (ASGI) ----------------------------------------------------------


ORIGIN_REFUSED_BODY = b'{"error":"forbidden","error_description":"Origin not allowed"}'
ORIGIN_LOG_MAX = 50   # distinct refused origins logged per process, so a flood cannot fill the log


class OriginCheck:
    """The MCP spec's Origin rule for /mcp (Streamable HTTP): a request that carries an Origin
    header gets 403 unless the origin is allowed (`allowed`: PUBLIC_URL's, Claude's web apps and
    ALLOWED_ORIGINS, as auth_config.normal_origin spells them). A request without Origin passes:
    Claude's backend, Claude Code and other programs send none; a browser always does.

    Origin is then removed before the SDK sees the request. The server listens on 127.0.0.1, so
    the SDK's own DNS-rebinding guard allows only loopback origins and would refuse every allowed
    one; its Host half still runs (Caddy presents the loopback Host). enabled=False (ORIGIN_CHECK=off)
    skips the check and only removes the header, as Caddy did before this check existed.

    Only /mcp: the OAuth endpoints are meant for cross-origin use (the SDK answers CORS there), and
    /consent refuses a form posted from another site by Sec-Fetch-Site.
    """

    def __init__(self, app: Any, *, allowed=frozenset(), enabled: bool = True) -> None:
        self.app = app
        self.allowed = frozenset(allowed)
        self.enabled = bool(enabled)
        self._logged: set = set()

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http" or scope.get("path") not in (auth_config.MCP_PATH, auth_config.MCP_PATH + "/"):
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        origins = [value for name, value in headers if name.lower() == b"origin"]
        if not origins:
            await self.app(scope, receive, send)
            return
        if self.enabled:
            refused = [raw for raw in origins
                       if auth_config.normal_origin(raw.decode("latin-1")) not in self.allowed]
            if refused:
                self._log_refused(refused[0].decode("latin-1"))
                await send({"type": "http.response.start", "status": 403, "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(ORIGIN_REFUSED_BODY)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ]})
                await send({"type": "http.response.body", "body": ORIGIN_REFUSED_BODY})
                return
        scope = dict(scope)
        scope["headers"] = [(name, value) for name, value in headers if name.lower() != b"origin"]
        await self.app(scope, receive, send)

    def _log_refused(self, origin: str) -> None:
        shown = _plain(origin, LOG_VALUE_MAX)
        if shown in self._logged or len(self._logged) >= ORIGIN_LOG_MAX:
            return
        self._logged.add(shown)
        log.warning("mcp: refused a request from origin %r (not PUBLIC_URL, Claude or ALLOWED_ORIGINS)", shown)


# --- RFC 9207: the issuer in authorization responses (ASGI) ---------------------------------------


METADATA_PATH = "/.well-known/oauth-authorization-server"
ISS_SUPPORTED = "authorization_response_iss_parameter_supported"


class IssuerParameter:
    """RFC 9207 for the parts the SDK serves itself:

    - GET /.well-known/oauth-authorization-server: adds
      "authorization_response_iss_parameter_supported": true to the SDK's JSON;
    - /authorize: an error redirect back to the client (the SDK's own) gets iss=<issuer>.
      The success answer goes to /consent, which is no authorization response; /consent adds
      iss to its redirects itself.

    `issuer` must be the metadata's issuer exactly (PUBLIC_URL; build() checks the SDK keeps it).
    """

    def __init__(self, app: Any, *, issuer: str, consent_url: str) -> None:
        self.app = app
        self.issuer = issuer
        self.consent_url = consent_url
        self._iss = ("iss=" + quote(issuer, safe="")).encode("ascii")

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope.get("path"), scope.get("method")
        if path == METADATA_PATH and method == "GET":
            await self.app(scope, receive, self._metadata_sender(send))
        elif path == "/authorize":
            await self.app(scope, receive, self._authorize_sender(send))
        else:
            await self.app(scope, receive, send)

    def _authorize_sender(self, send: Callable) -> Callable:
        async def sender(message: dict) -> None:
            if message.get("type") == "http.response.start" and message.get("status") in (302, 303):
                headers = [(name, self._with_iss(value) if name.lower() == b"location" else value)
                           for name, value in message.get("headers", [])]
                message = {**message, "headers": headers}
            await send(message)

        return sender

    def _with_iss(self, location: bytes) -> bytes:
        text = location.decode("latin-1")
        if text.startswith(self.consent_url + "?") or "#" in text:
            return location
        query = urlsplit(text).query
        if any(part.split("=", 1)[0] == "iss" for part in query.split("&")):
            return location
        return location + (b"&" if query else b"?") + self._iss

    def _metadata_sender(self, send: Callable) -> Callable:
        start: Optional[dict] = None
        body = bytearray()

        async def sender(message: dict) -> None:
            nonlocal start
            kind = message.get("type")
            if kind == "http.response.start" and message.get("status") == 200:
                start = message   # held until the body is complete
                return
            if kind == "http.response.body" and start is not None:
                body.extend(message.get("body", b""))
                if message.get("more_body", False):
                    return
                new_body = self._add_flag(bytes(body))
                headers = [(name, value) for name, value in start.get("headers", [])
                           if name.lower() != b"content-length"]
                headers.append((b"content-length", str(len(new_body)).encode("ascii")))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": new_body})
                return
            await send(message)

        return sender

    def _add_flag(self, body: bytes) -> bytes:
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            return body
        if not isinstance(data, dict):
            return body
        data[ISS_SUPPORTED] = True
        return json.dumps(data, separators=(",", ":")).encode("utf-8")


# --- build-time check ----------------------------------------------------------------------------


@contextmanager
def _quiet() -> Iterator[None]:
    disabled = log.disabled
    log.disabled = True
    try:
        yield
    finally:
        log.disabled = disabled


def selfcheck() -> None:
    """The Docker build check: build a throwaway sign-in on a temporary database,
    serve it from a plain MCPServer, and check that every sign-in path is routed
    and that the SDK still has what this module relies on. No output on success,
    no network. Raises RuntimeError("owner_auth selfcheck: <what>")."""
    try:
        _selfcheck()
    except RuntimeError as exc:
        if str(exc).startswith("owner_auth selfcheck:"):
            raise
        raise RuntimeError(f"owner_auth selfcheck: {type(exc).__name__}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - the build must fail with one clear line
        raise RuntimeError(f"owner_auth selfcheck: {type(exc).__name__}: {exc}") from exc


def _selfcheck() -> None:
    from mcp.server.mcpserver import MCPServer

    for name in ("validate_redirect_uri", "validate_scope"):
        if not callable(getattr(OAuthClientInformationFull, name, None)):
            raise RuntimeError(f"owner_auth selfcheck: OAuthClientInformationFull has no {name}()")
    with tempfile.TemporaryDirectory(prefix="owner-auth-selfcheck-") as tmp, _quiet():
        config = AuthConfig(public_url="https://selfcheck.invalid",
                            owner_secret="selfcheck-" + secrets.token_urlsafe(24),
                            db_path=os.path.join(tmp, "auth.sqlite"))
        auth = build(config)
        try:
            wanted = [name for name, value in vars(OAuthAuthorizationServerProvider).items()
                      if not name.startswith("_") and callable(value)]
            missing = [name for name in wanted if not inspect.iscoroutinefunction(getattr(auth.provider, name, None))]
            if missing:
                raise RuntimeError(f"owner_auth selfcheck: the provider lacks {', '.join(missing)}")
            server = MCPServer("selfcheck", auth=auth.settings, auth_server_provider=auth.provider)
            register_routes(server, auth)
            app = server.streamable_http_app()
            routes = {getattr(route, "path", None): getattr(route, "methods", None) for route in app.routes}
            unrouted = [path for path in SELFCHECK_PATHS if path not in routes]
            if unrouted:
                raise RuntimeError(f"owner_auth selfcheck: not routed: {', '.join(unrouted)}")
            if not {"GET", "POST"} <= set(routes[auth_config.CONSENT_PATH] or ()):
                raise RuntimeError("owner_auth selfcheck: /consent does not take GET and POST")
        finally:
            auth.provider.store.close()
