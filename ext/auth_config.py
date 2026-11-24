"""Sign-in settings: the constants and the environment check.

Standard library only (it runs in `launch.py --check-config` on any Python
3.10+, before Caddy starts). owner_auth.py builds the sign-in from the
AuthConfig made here; auth_store.py keeps its records.

Sign-in is on whenever GARMIN_MCP_TRANSPORT is not stdio (the image always
serves streamable-http). The variables:

    OWNER_SECRET           required; typed on the consent page to approve a sign-in
    PUBLIC_URL             this server's public origin; default https://$RAILWAY_PUBLIC_DOMAIN
    MCP_BEARER_TOKEN       optional static header bearer for Claude Code or CI
    AUTH_DB                sqlite file for clients and tokens; default /data/auth.sqlite
    ALLOWED_REDIRECT_URIS  extra exact https:// redirect URIs, comma-separated

config_from_environ() never prints or returns a secret in an error line.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

# --- fixed values ---------------------------------------------------------------

CONSENT_PATH = "/consent"
ROOT_PRM_PATH = "/.well-known/oauth-protected-resource"
MCP_PATH = "/mcp"
CLAUDE_CALLBACKS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
GRANT_TYPES = ("authorization_code", "refresh_token")
VALID_SCOPES = ("offline_access",)
DEFAULT_AUTH_DB = "/data/auth.sqlite"
RAILWAY_DEPLOYMENT_VARS = ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_DEPLOYMENT_ID")
ACCESS_TTL = 3600                 # seconds
REFRESH_TTL = 30 * 86400          # from issue; every rotation issues a fresh one (sliding)
REFRESH_REUSE_GRACE = 120         # F4: idempotent replay window, seconds
CODE_TTL = 300
PENDING_TTL = 600
MAX_SECRET_FAILURES = 3
MAX_PENDING_PER_CLIENT = 5        # F5: past it, that client's own oldest row is deleted
MAX_CLIENTS = 200                 # F1
# F5: global; when full, authorize() refuses. It is at least MAX_CLIENTS x
# MAX_PENDING_PER_CLIENT (evicting a client deletes its rows), so strangers'
# clients can never fill it and refuse the owner's sign-in; it only bounds the
# table if those caps change.
MAX_PENDING = MAX_CLIENTS * MAX_PENDING_PER_CLIENT
MAX_EXTRA_REDIRECTS = 10
MAX_STATE_LEN = 1024              # F12
MAX_SCOPE_LEN = 512               # F12
EVICT_IDLE_DAYS = 7               # F11, tier 2

# Secret and token rules (section 3 of the build contract).
OWNER_SECRET_MIN, OWNER_SECRET_MAX = 32, 256
OWNER_SECRET_MIN_DISTINCT = 12
STATIC_BEARER_RE = re.compile(r"[A-Za-z0-9._~-]{32,512}")
MAX_REDIRECT_LEN = 512

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")   # as urlsplit().hostname spells them

# --- the --check-config lines (launch.py prefixes "launch: config error: ") -----

ERR_A1 = ("PUBLIC_URL is not set and RAILWAY_PUBLIC_DOMAIN is empty: set PUBLIC_URL to this server's "
          "https origin, e.g. https://your-app.up.railway.app, or with Docker the tunnel's https://<hostname>")
ERR_A2 = ("PUBLIC_URL must start with https:// (http:// only for 127.0.0.1, localhost or [::1], "
          "and never on Railway)")
ERR_A3 = ("PUBLIC_URL must be an origin only (scheme, host, optional port): "
          "no path, query, fragment or user info")
ERR_A4 = ("OWNER_SECRET is not set: on Railway ./scripts/deploy.sh generates one; with Docker see "
          "docs/deploy-docker.md; or set 32+ random characters")
ERR_A5 = "OWNER_SECRET must be 32 to 256 printable ASCII characters, with no space at the start or end"
ERR_A6 = "OWNER_SECRET is too simple: use at least 12 different characters"
ERR_A7 = "OWNER_SECRET must not be the same as MCP_BEARER_TOKEN"
ERR_A8 = "MCP_BEARER_TOKEN (optional) must be 32 to 512 characters of A-Z a-z 0-9 . _ ~ -"
ERR_A9 = "AUTH_DB must be an absolute file path, e.g. /data/auth.sqlite"
ERR_A10 = "ALLOWED_REDIRECT_URIS: entry {n} must be an https:// URL with a host and no fragment or user info"
ERR_A11 = "ALLOWED_REDIRECT_URIS: at most 10 entries"
ERR_A12 = "GARMIN_MCP_TRANSPORT=sse cannot be used with the built-in sign-in: use streamable-http"

# EDGE_CLIENT_IP_HEADER: the header Caddy reads the caller's address from (the Caddyfile's client_ip_headers),
# which keys the sign-in rate limits. Unset means X-Real-IP (Railway's edge sets it); CF-Connecting-IP is for
# Docker Compose behind a Cloudflare Tunnel. Set but empty, Caddy would fall back to X-Forwarded-For, which the
# caller writes. Checked in every mode and transport (edge_header_errors); the value is never echoed.
EDGE_ENV = "EDGE_CLIENT_IP_HEADER"
EDGE_HEADERS = ("X-Real-IP", "CF-Connecting-IP")
ERR_E1 = ("EDGE_CLIENT_IP_HEADER is set but empty: delete the variable (Railway) or set CF-Connecting-IP "
          "(Docker Compose behind a Cloudflare Tunnel)")
ERR_E2 = "EDGE_CLIENT_IP_HEADER must be X-Real-IP or CF-Connecting-IP; the value set is neither"
ERR_E3 = ("EDGE_CLIENT_IP_HEADER=CF-Connecting-IP is only for Docker Compose behind a Cloudflare Tunnel: "
          "on Railway, delete the variable")

# A DNS name as urlsplit() returns it (lowercase): labels of letters, digits,
# '_' and '-', joined by dots, with an optional final dot. Anything else
# (spaces, '%', unicode) is not something pydantic would keep as written.
_DNS_HOST = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?")


def auth_required(environ: Mapping[str, str]) -> bool:
    """True when the server listens on the network (any transport but stdio)."""
    return (environ.get("GARMIN_MCP_TRANSPORT") or "stdio").strip().lower() != "stdio"


@dataclass(frozen=True)
class AuthConfig:
    public_url: str                                   # e.g. "https://coach.example.com" (normalized, no trailing slash)
    owner_secret: str = field(repr=False)
    static_bearer: Optional[str] = field(default=None, repr=False)
    extra_redirect_uris: Tuple[str, ...] = ()
    db_path: str = DEFAULT_AUTH_DB

    @property
    def resource_url(self) -> str:
        """The protected resource (RFC 8707/9728): what Claude pastes as the connector URL."""
        return self.public_url + MCP_PATH

    @property
    def consent_url(self) -> str:
        return self.public_url + CONSENT_PATH

    @property
    def host(self) -> str:
        """host[:port] of the public origin, shown on the consent page."""
        return urlsplit(self.public_url).netloc


def _set(environ: Mapping[str, str], name: str) -> str:
    """The variable's value; unset counts as empty."""
    return environ.get(name) or ""


def on_railway(environ: Mapping[str, str]) -> bool:
    """True when any of Railway's deployment variables is set (non-empty)."""
    return any(_set(environ, name).strip() for name in RAILWAY_DEPLOYMENT_VARS)


_on_railway = on_railway  # the older private name


def edge_header_errors(environ: Mapping[str, str]) -> List[str]:
    """[] or one error line for EDGE_CLIENT_IP_HEADER (ERR_E1-ERR_E3); the value is never in the line.

    Pure, standard library only. Surrounding spaces are ignored; the header names must be spelled exactly.
    """
    if EDGE_ENV not in environ:
        return []
    value = (environ.get(EDGE_ENV) or "").strip()
    if not value:
        return [ERR_E1]
    if value not in EDGE_HEADERS:
        return [ERR_E2]
    if value == "CF-Connecting-IP" and on_railway(environ):
        return [ERR_E3]
    return []


def _origin(raw: str, environ: Mapping[str, str]) -> Tuple[Optional[str], List[str]]:
    """(normalized origin, errors) for a PUBLIC_URL value. A2 and A3 are
    checked separately, so one value can give both lines."""
    errors = []
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname or ""
    except ValueError:  # for example an unclosed "[" in the host
        return None, [ERR_A3] if raw.lower().startswith("https://") else [ERR_A2, ERR_A3]

    scheme = parts.scheme
    if not (scheme == "https" or (scheme == "http" and hostname in LOOPBACK_HOSTS and not _on_railway(environ))):
        errors.append(ERR_A2)

    host = None
    if "@" not in parts.netloc and hostname:
        if parts.netloc.startswith("["):
            try:
                host = "[" + ipaddress.IPv6Address(hostname).compressed + "]"
            except ValueError:
                host = None
        elif _DNS_HOST.fullmatch(hostname):
            host = hostname
    try:
        port = parts.port
    except ValueError:  # not a number, or out of range
        port, host = None, None
    if port == 0:
        host = None
    if host is None or parts.path not in ("", "/") or parts.query or parts.fragment or "#" in raw or "?" in raw:
        errors.append(ERR_A3)
    if errors:
        return None, errors

    default_port = {"https": 443, "http": 80}[scheme]
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host}{suffix}", []


def _owner_secret_errors(secret: str) -> List[str]:
    if not secret:
        return [ERR_A4]
    printable_ascii = all(" " <= ch <= "~" for ch in secret)
    if not (OWNER_SECRET_MIN <= len(secret) <= OWNER_SECRET_MAX) or not printable_ascii or secret != secret.strip():
        return [ERR_A5]
    if len(set(secret)) < OWNER_SECRET_MIN_DISTINCT:
        return [ERR_A6]
    return []


def _redirect_ok(uri: str) -> bool:
    """An ALLOWED_REDIRECT_URIS entry: https, a host, no fragment, no user info."""
    if len(uri) > MAX_REDIRECT_LEN or "#" in uri or any(ch <= " " or ch == "\x7f" for ch in uri):
        return False
    try:
        parts = urlsplit(uri)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return False
    return parts.scheme == "https" and bool(hostname) and "@" not in parts.netloc and port != 0


def config_from_environ(environ: Mapping[str, str]) -> Tuple[Optional[AuthConfig], List[str]]:
    """Pure: no file system, no network, never raises for a bad value. (config, []) or (None, errors).

    Error order: A12, A1-A3, A4-A7, A8, A9, A10-A11. No error line contains
    the text of OWNER_SECRET or MCP_BEARER_TOKEN.
    """
    errors: List[str] = []

    if (_set(environ, "GARMIN_MCP_TRANSPORT")).strip().lower() == "sse":
        errors.append(ERR_A12)

    raw_url = _set(environ, "PUBLIC_URL").strip()
    if not raw_url:
        domain = _set(environ, "RAILWAY_PUBLIC_DOMAIN").strip()
        raw_url = "https://" + domain if domain else ""
    public_url = None
    if not raw_url:
        errors.append(ERR_A1)
    else:
        public_url, url_errors = _origin(raw_url, environ)
        errors += url_errors

    secret = _set(environ, "OWNER_SECRET")
    secret_errors = _owner_secret_errors(secret)
    static = _set(environ, "MCP_BEARER_TOKEN") or None   # an empty value counts as unset
    static_ok = static is None or STATIC_BEARER_RE.fullmatch(static) is not None
    errors += secret_errors
    if not secret_errors and static is not None and static_ok:
        # Both are printable ASCII here, so compare_digest takes them as str.
        if hmac.compare_digest(secret, static):
            errors.append(ERR_A7)
    if not static_ok:
        errors.append(ERR_A8)

    db_path = _set(environ, "AUTH_DB")
    if not db_path.strip():
        db_path = DEFAULT_AUTH_DB
    elif (db_path != db_path.strip() or "\x00" in db_path or not os.path.isabs(db_path)
          or db_path.endswith("/") or os.path.basename(db_path) in ("", ".", "..")):
        errors.append(ERR_A9)

    extra = tuple(u.strip() for u in _set(environ, "ALLOWED_REDIRECT_URIS").split(",") if u.strip())
    for n, uri in enumerate(extra, 1):
        if not _redirect_ok(uri):
            errors.append(ERR_A10.format(n=n))
    if len(extra) > MAX_EXTRA_REDIRECTS:
        errors.append(ERR_A11)

    if errors:
        return None, errors
    return AuthConfig(
        public_url=public_url,
        owner_secret=secret,
        static_bearer=static,
        extra_redirect_uris=extra,
        db_path=db_path,
    ), []
