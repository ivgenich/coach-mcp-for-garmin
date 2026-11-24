"""HTTP guards for the built-in sign-in: rate limits and access-log redaction.

Standard library only (it imports on Python 3.10 without the mcp SDK), so the
rules are testable anywhere.

SignInRateLimit is ASGI middleware. launch.py puts it outermost, through
mcp2_compat.set_asgi_middleware(), so it sees every request before the SDK.
It limits the sign-in endpoints per client IP:

    POST /consent         20 per 10 minutes per IP (the owner secret is typed here)
    GET, POST /authorize  30 per 10 minutes per IP
    POST /register        20 per hour per IP; 300 per hour for an IP inside
                          Anthropic's published egress ranges, which claude.ai's
                          backend registers from and every claude.ai user shares

There is no total (global) budget: any stranger could spend it and lock the
owner out. /token and /revoke have no limit: tokens and codes are 256-bit, and
every claude.ai refresh comes from Anthropic's addresses.

Methods no client needs get 405 here, before any rule counts them: HEAD
/authorize (Starlette adds HEAD to every GET route, and the SDK's handler reads
the form body on any method but GET, so it would start a sign-in uncounted),
and OPTIONS on /register, /token and /revoke unless it is a CORS preflight
(both Origin and Access-Control-Request-Method), which the SDK answers without
its handler; any other OPTIONS would reach the handler, past the /register
limit and owner_auth's RegistrationFilter.

The client IP. Python listens on 127.0.0.1 only, behind Caddy. Caddy sets
X-Client-IP to the caller's address (Railway's X-Real-IP) and replaces any
value the caller sent. client_ip() trusts that header only when the direct
peer is a loopback address, that is Caddy; otherwise it uses the peer itself.
That needs the real TCP peer in scope["client"]. uvicorn trusts proxy
headers from 127.0.0.1 by default and would put the last X-Forwarded-For
address there instead (behind Railway, the edge's, shared by every caller),
so launch.py sets FORWARDED_ALLOW_IPS to empty (uvicorn then applies no
X-Forwarded-* header at all) and Caddy drops X-Forwarded-For. Caddy still
passes a caller's own X-Forwarded-Proto and X-Forwarded-Host through, so
nothing here or anywhere else in this server may build a URL or make a
security decision from the request's scheme, Host or X-Forwarded-* headers:
PUBLIC_URL is the only source of this server's own address (owner_auth's
Origin check compares the Origin header with it, never with Host).

DropQueryString keeps query strings out of uvicorn's access log: /authorize
carries state and code_challenge there, and /consent the pending request id.
"""

from __future__ import annotations

import collections
import ipaddress
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

log = logging.getLogger("garmin_ext.http")

CLIENT_IP_HEADER = "x-client-ip"  # set by Caddy from {client_ip}; Python listens on 127.0.0.1 only

# Anthropic's published outbound ranges; claude.ai's backend registers from them.
ANTHROPIC_EGRESS = ("160.79.104.0/21", "153.61.208.0/20")


@dataclass(frozen=True)
class Rule:
    methods: FrozenSet[str]
    path: str  # exact match on scope["path"]
    limit: int
    window_s: float
    per_ip: bool  # False: one bucket for everyone (no default rule uses it now)
    wide_limit: Optional[int] = None  # the limit for a caller inside wide_nets
    wide_nets: Tuple[str, ...] = ()


DEFAULT_RULES = (
    Rule(frozenset({"POST"}), "/consent", 20, 600.0, True),
    Rule(frozenset({"GET", "POST"}), "/authorize", 30, 600.0, True),
    Rule(frozenset({"POST"}), "/register", 20, 3600.0, True, wide_limit=300, wide_nets=ANTHROPIC_EGRESS),
)

_UNKNOWN = "unknown"

# (method, path) -> the Allow header for methods the SDK's sign-in routes take
# that no client needs (see the module docstring). A CORS preflight OPTIONS
# still passes.
NOT_ALLOWED = {
    ("HEAD", "/authorize"): "GET, POST",
    ("OPTIONS", "/register"): "POST",
    ("OPTIONS", "/token"): "POST",
    ("OPTIONS", "/revoke"): "POST",
}


def _parse_ip(value):
    """An ipaddress object for `value`, with an IPv4-mapped IPv6 address
    (::ffff:a.b.c.d) as its IPv4 address; None if it is not an IP address."""
    try:
        address = ipaddress.ip_address(value.strip())
    except (AttributeError, ValueError):
        return None
    if address.version == 6 and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _peer(scope):
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])
    return None


def _header(scope, name):
    """The last value of request header `name` (lowercase), or None."""
    found = None
    for key, value in scope.get("headers") or ():
        if key.lower() == name.encode("latin-1"):
            found = value
    return None if found is None else found.decode("latin-1")


def client_ip(scope: dict) -> str:
    """The caller's IP address for rate limits and log lines.

    X-Client-IP counts only when the direct peer (scope["client"][0]) is a
    loopback address, Caddy on this machine, and the header parses as an IP
    address; it is returned in its normal form. Otherwise the peer address,
    or "unknown" when the server has none.
    """
    peer = _peer(scope)
    peer_ip = _parse_ip(peer) if peer is not None else None
    if peer_ip is not None and peer_ip.is_loopback:
        value = _header(scope, CLIENT_IP_HEADER)
        address = _parse_ip(value) if value is not None else None
        if address is not None:
            return str(address)
    if peer is None:
        return _UNKNOWN
    return str(peer_ip) if peer_ip is not None else peer


def bucket_key(ip: str) -> str:
    """The rate-limit bucket for a client_ip() value: an IPv4 address as
    is (an IPv4-mapped IPv6 address counts as its IPv4 address); any other
    IPv6 address as its /64, since one host usually holds a whole /64; any
    other text as is."""
    address = _parse_ip(ip)
    if address is None:
        return ip
    if address.version == 4:
        return str(address)
    return str(ipaddress.IPv6Network((int(address) >> 64 << 64, 64)))


def _in_nets(ip, nets):
    address = _parse_ip(ip)
    return address is not None and any(address in net for net in nets)


class _Bucket:
    __slots__ = ("hits",)

    def __init__(self):
        self.hits = collections.deque()


class _RuleState:
    """One rule's buckets, least recently used first."""

    def __init__(self, rule):
        self.rule = rule
        self.nets = tuple(ipaddress.ip_network(net) for net in rule.wide_nets)
        self.buckets: "collections.OrderedDict[str, _Bucket]" = collections.OrderedDict()

    def limit_for(self, ip):
        if self.rule.wide_limit is not None and self.nets and _in_nets(ip, self.nets):
            return self.rule.wide_limit
        return self.rule.limit

    def bucket(self, key, max_keys):
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = self.buckets[key] = _Bucket()
            while len(self.buckets) > max_keys:
                self.buckets.popitem(last=False)
        else:
            self.buckets.move_to_end(key)
        return bucket


def not_allowed(scope) -> Optional[str]:
    """The Allow header value when the request's method on its path is in
    NOT_ALLOWED and it is not a CORS preflight (OPTIONS with both Origin and
    Access-Control-Request-Method, as the SDK's CORS layer tells one), else None."""
    method = scope.get("method")
    allow = NOT_ALLOWED.get((method, scope.get("path")))
    if allow is None:
        return None
    if (method == "OPTIONS" and _header(scope, "origin") is not None
            and _header(scope, "access-control-request-method") is not None):
        return None
    return allow


class SignInRateLimit:
    """ASGI middleware: sliding-window limits on the sign-in endpoints.

    A request is checked against every rule that matches its method and path.
    If any of their buckets is full, the answer is 429 and the request is not
    counted anywhere; otherwise it is counted in each of them and passed on.
    Each rule keeps at most max_keys buckets and drops the least recently used
    one past that. Before any rule, a method in NOT_ALLOWED gets 405 and is
    counted nowhere. Only "http" scopes are checked; websocket and lifespan
    scopes pass through.
    """

    def __init__(self, app, rules: Sequence[Rule] = DEFAULT_RULES,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 4096) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        self.app = app
        self.clock = clock
        self.max_keys = max_keys
        self._rules: List[_RuleState] = [_RuleState(rule) for rule in rules]

    def _matching(self, scope):
        method, path = scope.get("method"), scope.get("path")
        return [state for state in self._rules if path == state.rule.path and method in state.rule.methods]

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        allow = not_allowed(scope)
        if allow is not None:
            await _method_not_allowed(send, allow)
            return
        states = self._matching(scope)
        if not states:
            await self.app(scope, receive, send)
            return

        ip = client_ip(scope)
        key = bucket_key(ip)
        now = self.clock()
        checked: List[Tuple[_RuleState, _Bucket]] = []
        retry: Dict[int, float] = {}
        for index, state in enumerate(states):
            rule = state.rule
            bucket = state.bucket(key if rule.per_ip else "*", self.max_keys)
            hits = bucket.hits
            while hits and hits[0] <= now - rule.window_s:
                hits.popleft()
            limit = state.limit_for(ip)
            if len(hits) >= limit:
                # Free again when the hit that makes it full leaves the window.
                retry[index] = hits[len(hits) - limit] + rule.window_s - now
            checked.append((state, bucket))

        if retry:
            seconds = max(1, math.ceil(max(retry.values())))
            kinds = sorted({"per-ip" if states[i].rule.per_ip else "total" for i in retry})
            log.warning("rate limit: %s %s (%s) from %s, retry in %ds",
                        scope.get("method"), scope.get("path"), "+".join(kinds), ip, seconds)
            await _too_many(send, seconds)
            return

        for _, bucket in checked:
            bucket.hits.append(now)
        await self.app(scope, receive, send)


async def _too_many(send, seconds):
    body = json.dumps({
        "error": "too_many_requests",
        "error_description": f"Too many sign-in requests. Try again in {seconds} seconds.",
    }, separators=(",", ":")).encode("ascii")
    await send({
        "type": "http.response.start",
        "status": 429,
        "headers": [
            (b"content-type", b"application/json"),
            (b"retry-after", str(seconds).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def _method_not_allowed(send, allow):
    body = json.dumps({"error": "invalid_request", "error_description": "method not allowed"},
                      separators=(",", ":")).encode("ascii")
    await send({
        "type": "http.response.start",
        "status": 405,
        "headers": [
            (b"content-type", b"application/json"),
            (b"allow", allow.encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class DropQueryString(logging.Filter):
    """uvicorn.access records: args[2] "path?query" -> "path?[redacted]".

    uvicorn logs each request as '%s - "%s %s HTTP/%s" %d' with
    (client, method, path with query string, HTTP version, status). Any other
    record passes unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str) and "?" in args[2]:
            path = args[2].split("?", 1)[0]
            record.args = (args[0], args[1], path + "?[redacted]", args[3], args[4])
        return True


def install_log_redaction() -> None:
    """Put DropQueryString on the "uvicorn.access" logger. Idempotent.

    A filter on the logger itself, not on a handler, so it survives the
    logging.config.dictConfig() uvicorn runs at startup: dictConfig replaces a
    configured logger's handlers but keeps its filters. Every handler, the
    ones uvicorn adds included, sees the record after the filter.
    """
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, DropQueryString) for f in access.filters):
        access.addFilter(DropQueryString())
