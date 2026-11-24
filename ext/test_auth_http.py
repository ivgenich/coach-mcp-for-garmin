"""Tests for auth_http: the sign-in rate limits, the client IP and the access-log
redaction. Standard library only, so they run on every leg.

    python -m unittest test_auth_http -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import auth_http  # noqa: E402
from auth_http import Rule, SignInRateLimit  # noqa: E402

CALLER = "203.0.113.9"  # documentation addresses (RFC 5737)
OTHER = "198.51.100.7"
ANTHROPIC = "160.79.104.9"  # inside 160.79.104.0/21


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Downstream:
    """The app behind the middleware: counts what reaches it, answers 200."""

    def __init__(self):
        self.calls = 0
        self.scopes = []

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.scopes.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})


def http_scope(method="POST", path="/consent", ip=CALLER, peer="127.0.0.1", headers=()):
    raw = list(headers)
    if ip is not None:
        raw.append((b"x-client-ip", ip.encode("latin-1")))
    scope = {"type": "http", "method": method, "path": path, "headers": raw, "query_string": b""}
    if peer is not None:
        scope["client"] = (peer, 50000)
    return scope


class Case(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.clock = FakeClock()
        self.app = Downstream()
        # Keep the 429 warnings out of the test output; assertLogs still sees them.
        logger = logging.getLogger("garmin_ext.http")
        saved = (logger.handlers[:], logger.propagate)
        logger.handlers, logger.propagate = [logging.NullHandler()], False
        self.addCleanup(lambda: setattr(logger, "handlers", saved[0]) or setattr(logger, "propagate", saved[1]))

    def middleware(self, rules=auth_http.DEFAULT_RULES, max_keys=4096):
        return SignInRateLimit(self.app, rules=rules, clock=self.clock, max_keys=max_keys)

    def send(self, mw, scope):
        """(status, {header: value}, body) for one request through mw."""
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        self.loop.run_until_complete(mw(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return start["status"], headers, body

    def status(self, mw, **kwargs):
        return self.send(mw, http_scope(**kwargs))[0]

    def statuses(self, mw, count, **kwargs):
        return [self.status(mw, **kwargs) for _ in range(count)]


class ClientIp(unittest.TestCase):
    """X-Client-IP counts only from a loopback peer (Caddy)."""

    def test_header_from_a_loopback_peer(self):
        for peer in ("127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"):
            with self.subTest(peer=peer):
                self.assertEqual(auth_http.client_ip(http_scope(peer=peer)), CALLER)

    def test_header_ignored_when_the_peer_is_not_loopback(self):
        # Without Caddy in front, a caller could pick any bucket with the header.
        self.assertEqual(auth_http.client_ip(http_scope(ip="1.2.3.4", peer=OTHER)), OTHER)
        self.assertEqual(auth_http.client_ip(http_scope(ip="1.2.3.4", peer="2001:db8::5")), "2001:db8::5")

    def test_falls_back_to_the_peer(self):
        self.assertEqual(auth_http.client_ip(http_scope(ip=None)), "127.0.0.1")
        self.assertEqual(auth_http.client_ip(http_scope(ip=None, peer=OTHER)), OTHER)

    def test_a_bad_header_value_falls_back_to_the_peer(self):
        for value in ("", " ", "not-an-ip", "203.0.113.9, 198.51.100.7", "203.0.113.999", "unknown", "[::1]"):
            with self.subTest(value=value):
                self.assertEqual(auth_http.client_ip(http_scope(ip=value)), "127.0.0.1")

    def test_header_is_stripped_and_normalized(self):
        self.assertEqual(auth_http.client_ip(http_scope(ip="  203.0.113.9 ")), CALLER)
        self.assertEqual(auth_http.client_ip(http_scope(ip="2001:DB8:0:0::1")), "2001:db8::1")
        self.assertEqual(auth_http.client_ip(http_scope(ip="::ffff:1.2.3.4")), "1.2.3.4")

    def test_no_peer_is_unknown(self):
        self.assertEqual(auth_http.client_ip(http_scope(peer=None)), "unknown")
        self.assertEqual(auth_http.client_ip(http_scope(ip=None, peer=None)), "unknown")
        scope = http_scope()
        scope["client"] = None
        self.assertEqual(auth_http.client_ip(scope), "unknown")

    def test_a_peer_that_is_not_an_address_is_used_as_is(self):
        # Starlette's TestClient reports ("testclient", 50000).
        self.assertEqual(auth_http.client_ip(http_scope(ip=CALLER, peer="testclient")), "testclient")


class BucketKey(unittest.TestCase):
    def test_ipv4_as_is(self):
        self.assertEqual(auth_http.bucket_key(CALLER), CALLER)

    def test_ipv4_mapped_ipv6_is_its_ipv4_address(self):
        self.assertEqual(auth_http.bucket_key("::ffff:1.2.3.4"), auth_http.bucket_key("1.2.3.4"))
        self.assertEqual(auth_http.bucket_key("::ffff:1.2.3.4"), "1.2.3.4")

    def test_ipv6_by_64(self):
        same = {auth_http.bucket_key(a) for a in ("2001:db8:1:2::1", "2001:db8:1:2:ffff:ffff:ffff:ffff")}
        self.assertEqual(same, {"2001:db8:1:2::/64"})
        self.assertNotEqual(auth_http.bucket_key("2001:db8:1:3::1"), auth_http.bucket_key("2001:db8:1:2::1"))

    def test_other_text_as_is(self):
        self.assertEqual(auth_http.bucket_key("unknown"), "unknown")
        self.assertEqual(auth_http.bucket_key("testclient"), "testclient")


class DefaultRules(Case):
    def test_the_numbers(self):
        rules = {(tuple(sorted(r.methods)), r.path): r for r in auth_http.DEFAULT_RULES}
        self.assertEqual(set(rules), {(("POST",), "/consent"), (("GET", "POST"), "/authorize"), (("POST",), "/register")})
        consent, authorize, register = (rules[(("POST",), "/consent")], rules[(("GET", "POST"), "/authorize")],
                                        rules[(("POST",), "/register")])
        self.assertEqual((consent.limit, consent.window_s, consent.wide_limit), (20, 600.0, None))
        self.assertEqual((authorize.limit, authorize.window_s, authorize.wide_limit), (30, 600.0, None))
        self.assertEqual((register.limit, register.window_s, register.wide_limit), (20, 3600.0, 300))
        self.assertEqual(register.wide_nets, auth_http.ANTHROPIC_EGRESS)
        self.assertEqual(auth_http.ANTHROPIC_EGRESS, ("160.79.104.0/21", "153.61.208.0/20"))

    def test_no_rule_is_global(self):
        # A total budget would let any stranger lock the owner out.
        self.assertTrue(all(rule.per_ip for rule in auth_http.DEFAULT_RULES))

    def test_consent_post_20_per_10_minutes_per_ip(self):
        mw = self.middleware()
        self.assertEqual(self.statuses(mw, 21), [200] * 20 + [429])
        self.assertEqual(self.status(mw, ip=OTHER), 200)  # another caller is not limited
        self.assertEqual(self.status(mw, method="GET"), 200)  # the consent page itself is not limited

    def test_authorize_30_per_10_minutes_get_and_post_together(self):
        mw = self.middleware()
        self.assertEqual(self.statuses(mw, 15, method="GET", path="/authorize")
                         + self.statuses(mw, 16, method="POST", path="/authorize"), [200] * 30 + [429])

    def test_register_20_per_hour_for_an_ordinary_address(self):
        mw = self.middleware()
        self.assertEqual(self.statuses(mw, 21, path="/register"), [200] * 20 + [429])

    def test_register_300_per_hour_inside_anthropics_ranges(self):
        mw = self.middleware()
        self.assertEqual(self.statuses(mw, 301, path="/register", ip=ANTHROPIC), [200] * 300 + [429])
        for ip in ("160.79.111.254", "153.61.208.1", "153.61.223.254", "::ffff:160.79.104.10"):
            with self.subTest(ip=ip):
                self.assertEqual(self.statuses(mw, 25, path="/register", ip=ip), [200] * 25)
        # Just outside both ranges: the ordinary budget.
        for ip in ("160.79.112.1", "153.61.224.1", "160.79.103.255"):
            with self.subTest(ip=ip):
                self.assertEqual(self.statuses(mw, 21, path="/register", ip=ip), [200] * 20 + [429])

    def test_register_has_no_total_budget(self):
        # S38: fresh addresses keep registering after hundreds of others.
        mw = self.middleware()
        for n in range(1, 31):
            self.assertEqual(self.statuses(mw, 20, path="/register", ip=f"198.51.100.{n}"), [200] * 20)
        self.assertEqual(self.status(mw, path="/register", ip="198.51.100.200"), 200)

    def test_token_revoke_and_mcp_are_never_limited(self):
        mw = self.middleware()
        for method, path in (("POST", "/token"), ("POST", "/revoke"), ("POST", "/mcp"), ("GET", "/.well-known/oauth-authorization-server")):
            with self.subTest(path=path):
                self.assertEqual(self.statuses(mw, 40, method=method, path=path), [200] * 40)


class Answer429(Case):
    def test_status_headers_and_body(self):
        mw = self.middleware()
        self.statuses(mw, 20)
        self.clock.advance(100.2)
        status, headers, body = self.send(mw, http_scope())
        self.assertEqual(status, 429)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["retry-after"], "500")  # 499.8 s left, rounded up
        self.assertEqual(headers["content-length"], str(len(body)))
        self.assertEqual(json.loads(body), {
            "error": "too_many_requests",
            "error_description": "Too many sign-in requests. Try again in 500 seconds.",
        })

    def test_retry_after_is_at_least_one_second(self):
        mw = self.middleware()
        self.statuses(mw, 20)
        self.clock.advance(599.95)
        self.assertEqual(self.send(mw, http_scope())[1]["retry-after"], "1")

    def test_retry_after_follows_the_oldest_hit(self):
        mw = self.middleware()
        for _ in range(20):
            self.status(mw)
            self.clock.advance(10)  # hits at 0, 10, ..., 190
        self.assertEqual(self.send(mw, http_scope())[1]["retry-after"], "400")  # the first leaves at 600

    def test_the_app_is_not_called(self):
        mw = self.middleware()
        self.statuses(mw, 21)
        self.assertEqual(self.app.calls, 20)

    def test_a_warning_without_secrets(self):
        mw = self.middleware()
        self.statuses(mw, 20)
        with self.assertLogs("garmin_ext.http", logging.WARNING) as logs:
            self.status(mw)
        self.assertEqual(logs.output, [f"WARNING:garmin_ext.http:rate limit: POST /consent (per-ip) from {CALLER}, retry in 600s"])


class SlidingWindow(Case):
    def test_the_window_slides(self):
        mw = self.middleware()
        for _ in range(20):
            self.status(mw)
            self.clock.advance(10)  # hits at 0, 10, ..., 190; now 200
        self.assertEqual(self.status(mw), 429)
        self.clock.advance(399.5)  # 599.5 after the first hit: still inside
        self.assertEqual(self.status(mw), 429)
        self.clock.advance(0.5)  # 600: the first hit left (exact in binary floats)
        self.assertEqual(self.statuses(mw, 2), [200, 429])
        self.clock.advance(10)  # 610: the second left
        self.assertEqual(self.statuses(mw, 2), [200, 429])

    def test_after_a_full_window_the_whole_budget_is_back(self):
        mw = self.middleware()
        self.statuses(mw, 21)
        self.clock.advance(600)
        self.assertEqual(self.statuses(mw, 21), [200] * 20 + [429])

    def test_a_blocked_request_is_not_counted(self):
        mw = self.middleware()
        self.statuses(mw, 20)
        for _ in range(50):  # hammering while blocked
            self.clock.advance(10)
            self.assertEqual(self.status(mw), 429)
        self.clock.advance(100)  # 600 after the 20 hits: if blocked ones counted, still full
        self.assertEqual(self.statuses(mw, 21), [200] * 20 + [429])


class Buckets(Case):
    def test_per_ip_and_total_buckets(self):
        rules = (Rule(frozenset({"POST"}), "/a", 3, 60.0, True),
                 Rule(frozenset({"POST"}), "/b", 3, 60.0, False))
        mw = self.middleware(rules)
        self.assertEqual(self.statuses(mw, 4, path="/a", ip=CALLER), [200, 200, 200, 429])
        self.assertEqual(self.statuses(mw, 3, path="/a", ip=OTHER), [200] * 3)
        # The total bucket is one for everyone.
        self.assertEqual(self.statuses(mw, 2, path="/b", ip=CALLER) + self.statuses(mw, 2, path="/b", ip=OTHER),
                         [200, 200, 200, 429])
        with self.assertLogs("garmin_ext.http", logging.WARNING) as logs:
            self.status(mw, path="/b", ip="192.0.2.1")
        self.assertIn("rate limit: POST /b (total) from 192.0.2.1, retry in 60s", logs.output[0])

    def test_ipv6_callers_in_one_64_share_a_bucket(self):
        mw = self.middleware()
        self.assertEqual(self.statuses(mw, 10, ip="2001:db8:1:2::1") + self.statuses(mw, 11, ip="2001:db8:1:2::abcd"),
                         [200] * 20 + [429])
        self.assertEqual(self.status(mw, ip="2001:db8:1:3::1"), 200)

    def test_ipv4_mapped_caller_shares_the_ipv4_bucket(self):
        mw = self.middleware()
        self.assertEqual(self.statuses(mw, 10, ip="::ffff:203.0.113.9") + self.statuses(mw, 11, ip=CALLER),
                         [200] * 20 + [429])

    def test_without_a_trusted_header_every_caller_shares_the_peers_bucket(self):
        # Degraded but not spoofable: no X-Client-IP, so Caddy's own address.
        mw = self.middleware()
        self.assertEqual(self.statuses(mw, 10, ip=None) + self.statuses(mw, 11, ip="bad value"), [200] * 20 + [429])
        # A direct (non-loopback) peer cannot choose its bucket with the header.
        self.assertEqual(self.statuses(mw, 10, ip="192.0.2.1", peer=OTHER)
                         + self.statuses(mw, 11, ip="192.0.2.2", peer=OTHER), [200] * 20 + [429])

    def test_a_request_full_in_one_rule_is_counted_in_none(self):
        rules = (Rule(frozenset({"POST"}), "/x", 2, 60.0, True),
                 Rule(frozenset({"POST"}), "/x", 5, 60.0, False))
        mw = self.middleware(rules)
        self.assertEqual(self.statuses(mw, 3, ip=CALLER, path="/x"), [200, 200, 429])  # per-ip full at 2
        # The total rule counted only the two that passed: three more from others fit.
        self.assertEqual(self.statuses(mw, 4, ip=OTHER, path="/x"), [200, 200, 429, 429])
        self.assertEqual(self.statuses(mw, 2, ip="192.0.2.1", path="/x"), [200, 429])

    def test_least_recently_used_bucket_dropped_past_max_keys(self):
        mw = self.middleware(max_keys=2)
        a, b, c = "192.0.2.1", "192.0.2.2", "192.0.2.3"
        self.assertEqual(self.statuses(mw, 21, ip=a), [200] * 20 + [429])
        self.assertEqual(self.statuses(mw, 21, ip=b), [200] * 20 + [429])
        self.status(mw, ip=a)  # a is used again (blocked), so b is the least recently used
        self.status(mw, ip=c)  # a third key: b's bucket is dropped
        self.assertEqual(self.status(mw, ip=a), 429)
        self.assertEqual(self.statuses(mw, 21, ip=b), [200] * 20 + [429])  # b starts over
        consent = mw._rules[0]
        self.assertLessEqual(len(consent.buckets), 2)

    def test_max_keys_must_be_positive(self):
        with self.assertRaises(ValueError):
            SignInRateLimit(self.app, max_keys=0)


class PassThrough(Case):
    def test_other_methods_and_paths_pass(self):
        mw = self.middleware()
        self.statuses(mw, 20)  # /consent is full for this caller
        for method, path in (("GET", "/consent"), ("HEAD", "/consent"), ("PUT", "/consent"), ("POST", "/consent/"),
                             ("POST", "/Consent"), ("POST", "/consent/x"), ("DELETE", "/authorize"),
                             ("GET", "/register"), ("POST", "/healthz")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.status(mw, method=method, path=path), 200)

    def test_websocket_and_lifespan_scopes_pass(self):
        mw = self.middleware()
        self.statuses(mw, 20)
        for scope in ({"type": "lifespan"}, {"type": "websocket", "path": "/consent", "headers": [], "client": (CALLER, 1)}):
            with self.subTest(scope=scope["type"]):
                async def receive():
                    return {"type": "lifespan.startup"}

                async def send(message):
                    pass

                before = self.app.calls
                self.loop.run_until_complete(mw(scope, receive, send))
                self.assertEqual(self.app.calls, before + 1)
        self.assertEqual(self.app.scopes[-2:], ["lifespan", "websocket"])


class NotAllowed(Case):
    """V1-2: methods the SDK's sign-in routes take that no client needs get 405
    before any rule, and are counted nowhere."""

    CASES = (("HEAD", "/authorize", "GET, POST"), ("OPTIONS", "/register", "POST"),
             ("OPTIONS", "/token", "POST"), ("OPTIONS", "/revoke", "POST"))
    PREFLIGHT = ((b"origin", b"https://app.example.com"), (b"access-control-request-method", b"POST"))

    def test_405_with_allow_and_the_app_is_not_called(self):
        mw = self.middleware()
        for method, path, allow in self.CASES:
            with self.subTest(method=method, path=path):
                status, headers, body = self.send(mw, http_scope(method=method, path=path))
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], allow)
                self.assertEqual(headers["content-type"], "application/json")
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["content-length"], str(len(body)))
                self.assertEqual(json.loads(body), {"error": "invalid_request",
                                                    "error_description": "method not allowed"})
        self.assertEqual(self.app.calls, 0)

    def test_counted_nowhere_and_never_429(self):
        mw = self.middleware()
        self.assertEqual(self.statuses(mw, 40, method="HEAD", path="/authorize"), [405] * 40)
        self.assertEqual(self.statuses(mw, 30, method="OPTIONS", path="/register"), [405] * 30)
        # The caller's budgets are untouched.
        self.assertEqual(self.statuses(mw, 31, method="GET", path="/authorize"), [200] * 30 + [429])
        self.assertEqual(self.statuses(mw, 21, method="POST", path="/register"), [200] * 20 + [429])
        # A full bucket does not turn the 405 into a 429.
        self.assertEqual(self.status(mw, method="HEAD", path="/authorize"), 405)

    def test_a_cors_preflight_passes_and_either_header_alone_does_not(self):
        mw = self.middleware()
        for path in ("/register", "/token", "/revoke"):
            with self.subTest(path=path):
                self.assertEqual(self.status(mw, method="OPTIONS", path=path, headers=self.PREFLIGHT), 200)
                for one in self.PREFLIGHT:
                    self.assertEqual(self.status(mw, method="OPTIONS", path=path, headers=(one,)), 405)
        self.assertEqual(self.app.calls, 3)

    def test_other_methods_on_those_paths_still_pass(self):
        mw = self.middleware()
        for method, path in (("GET", "/authorize"), ("POST", "/authorize"), ("POST", "/register"),
                             ("POST", "/token"), ("POST", "/revoke"), ("OPTIONS", "/authorize"),
                             ("HEAD", "/register"), ("OPTIONS", "/.well-known/oauth-authorization-server")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.status(mw, method=method, path=path), 200)

    def test_no_log_line(self):
        mw = self.middleware()
        with self.assertNoLogs("garmin_ext.http"):
            for method, path, _ in self.CASES:
                self.status(mw, method=method, path=path)


def uvicorn_record(path, args_count=5, msg='%s - "%s %s HTTP/%s" %d'):
    args = ("127.0.0.1:50000", "GET", path, "1.1", 302)[:args_count]
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, args, None)


class Redaction(unittest.TestCase):
    def test_query_string_is_redacted(self):
        record = uvicorn_record("/authorize?response_type=code&state=STATE-VALUE&code_challenge=CHALLENGE")
        self.assertTrue(auth_http.DropQueryString().filter(record))
        self.assertEqual(record.getMessage(), '127.0.0.1:50000 - "GET /authorize?[redacted] HTTP/1.1" 302')
        record = uvicorn_record("/consent?req=PENDING-ID")
        auth_http.DropQueryString().filter(record)
        self.assertNotIn("PENDING-ID", record.getMessage())
        self.assertIn("/consent?[redacted]", record.getMessage())

    def test_records_without_a_query_or_of_another_shape_are_unchanged(self):
        for record in (uvicorn_record("/mcp"), uvicorn_record("/a?b", args_count=4, msg="%s %s %s %s"),
                       logging.LogRecord("x", logging.INFO, __file__, 1, "plain ? text", None, None)):
            with self.subTest(msg=record.msg):
                before = (record.msg, record.args)
                self.assertTrue(auth_http.DropQueryString().filter(record))
                self.assertEqual((record.msg, record.args), before)

    def test_install_is_idempotent(self):
        access = logging.getLogger("uvicorn.access")
        saved = list(access.filters)
        self.addCleanup(setattr, access, "filters", saved)
        auth_http.install_log_redaction()
        auth_http.install_log_redaction()
        self.assertEqual(sum(isinstance(f, auth_http.DropQueryString) for f in access.filters), 1)

    def test_survives_uvicorns_logging_config(self):
        # uvicorn runs logging.config.dictConfig(LOGGING_CONFIG) when it
        # starts, after launch.py installed the filter. In a subprocess, since
        # dictConfig closes every handler in the process.
        script = r"""
import logging, logging.config, sys
sys.path.insert(0, sys.argv[1])
import auth_http
auth_http.install_log_redaction()
auth_http.install_log_redaction()
try:
    from uvicorn.config import LOGGING_CONFIG
    which = "uvicorn"
except ImportError:  # the standard-library leg: the same shape
    which = "stdlib"
    LOGGING_CONFIG = {
        "version": 1, "disable_existing_loggers": False,
        "formatters": {"access": {"format": "%(levelname)s: %(message)s"}},
        "handlers": {"access": {"class": "logging.StreamHandler", "formatter": "access", "stream": "ext://sys.stdout"}},
        "loggers": {"uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False}},
    }
logging.config.dictConfig(LOGGING_CONFIG)
access = logging.getLogger("uvicorn.access")
print(which, sum(isinstance(f, auth_http.DropQueryString) for f in access.filters), file=sys.stderr)
access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", "/authorize?state=STATE-VALUE&x=1", "1.1", 302)
"""
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
        proc = subprocess.run([sys.executable, "-c", script, HERE], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        which, count = proc.stderr.split()
        self.assertEqual(count, "1", which)
        self.assertIn('"GET /authorize?[redacted] HTTP/1.1" 302', proc.stdout)
        self.assertNotIn("STATE-VALUE", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
