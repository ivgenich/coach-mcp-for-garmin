"""Tests for auth_config: the sign-in constants and the environment check.

    python -m unittest discover -s ext -p "test_auth_config*.py" -v

Standard library only; runs on Python 3.10+. Every secret here is a fake
made at run time and is never printed.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth_config as ac  # noqa: E402
from auth_config import (  # noqa: E402
    ERR_A1, ERR_A2, ERR_A3, ERR_A4, ERR_A5, ERR_A6, ERR_A7, ERR_A8, ERR_A9, ERR_A11, ERR_A12,
)


def fake_secret():
    return "TEST-ONLY-" + secrets.token_urlsafe(24)


def a10(n):
    return ac.ERR_A10.format(n=n)


def good_env(**overrides):
    env = {
        "GARMIN_MCP_TRANSPORT": "streamable-http",
        "PUBLIC_URL": "https://coach.example.com",
        "OWNER_SECRET": fake_secret(),
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


class AuthRequired(unittest.TestCase):
    def test_truth_table(self):
        cases = {
            None: False,
            "": False,
            "stdio": False,
            " STDIO ": False,
            "Stdio": False,
            "streamable-http": True,
            "STREAMABLE-HTTP": True,
            "sse": True,
            "anything": True,
            "  ": True,  # not empty, not stdio: upstream refuses it too
        }
        for value, expected in cases.items():
            env = {} if value is None else {"GARMIN_MCP_TRANSPORT": value}
            with self.subTest(transport=value):
                self.assertIs(ac.auth_required(env), expected)


class Defaults(unittest.TestCase):
    def test_minimal_environment(self):
        env = good_env()
        config, errors = ac.config_from_environ(env)
        self.assertEqual(errors, [])
        self.assertEqual(config.public_url, "https://coach.example.com")
        self.assertEqual(config.owner_secret, env["OWNER_SECRET"])
        self.assertIsNone(config.static_bearer)
        self.assertEqual(config.extra_redirect_uris, ())
        self.assertEqual(config.db_path, ac.DEFAULT_AUTH_DB)
        self.assertEqual(config.db_path, "/data/auth.sqlite")

    def test_properties(self):
        config, _ = ac.config_from_environ(good_env(PUBLIC_URL="https://coach.example.com:8443"))
        self.assertEqual(config.resource_url, "https://coach.example.com:8443/mcp")
        self.assertEqual(config.consent_url, "https://coach.example.com:8443/consent")
        self.assertEqual(config.host, "coach.example.com:8443")
        config, _ = ac.config_from_environ(good_env())
        self.assertEqual(config.host, "coach.example.com")

    def test_constants(self):
        self.assertEqual(ac.CLAUDE_CALLBACKS, ("https://claude.ai/api/mcp/auth_callback",
                                               "https://claude.com/api/mcp/auth_callback"))
        self.assertEqual(ac.GRANT_TYPES, ("authorization_code", "refresh_token"))
        self.assertEqual(ac.VALID_SCOPES, ("offline_access",))
        self.assertEqual((ac.ACCESS_TTL, ac.REFRESH_TTL, ac.REFRESH_REUSE_GRACE), (3600, 30 * 86400, 30))
        self.assertEqual((ac.CODE_TTL, ac.PENDING_TTL, ac.MAX_SECRET_FAILURES), (300, 600, 3))
        self.assertEqual((ac.MAX_PENDING, ac.MAX_PENDING_PER_CLIENT, ac.MAX_CLIENTS), (1000, 5, 200))
        # V1-3: every stored client may hold its full share of consent requests,
        # so strangers' clients can never fill the table and refuse the owner's.
        self.assertGreaterEqual(ac.MAX_PENDING, ac.MAX_CLIENTS * ac.MAX_PENDING_PER_CLIENT)
        self.assertEqual((ac.MAX_EXTRA_REDIRECTS, ac.MAX_STATE_LEN, ac.MAX_SCOPE_LEN, ac.EVICT_IDLE_DAYS),
                         (10, 1024, 512, 7))
        self.assertEqual(ac.CONSENT_PATH, "/consent")
        self.assertEqual(ac.ROOT_PRM_PATH, "/.well-known/oauth-protected-resource")
        self.assertEqual(ac.MCP_PATH, "/mcp")

    def test_static_bearer_and_extras_kept(self):
        token = fake_secret()
        env = good_env(MCP_BEARER_TOKEN=token, AUTH_DB="/tmp/x/auth.sqlite",
                       ALLOWED_REDIRECT_URIS=" https://a.example/cb , ,https://B.example/cb?x=1,")
        config, errors = ac.config_from_environ(env)
        self.assertEqual(errors, [])
        self.assertEqual(config.static_bearer, token)
        self.assertEqual(config.db_path, "/tmp/x/auth.sqlite")
        # Stripped, empties dropped, otherwise kept as written.
        self.assertEqual(config.extra_redirect_uris, ("https://a.example/cb", "https://B.example/cb?x=1"))

    def test_empty_values_count_as_unset(self):
        config, errors = ac.config_from_environ(good_env(MCP_BEARER_TOKEN="", AUTH_DB="", ALLOWED_REDIRECT_URIS=""))
        self.assertEqual(errors, [])
        self.assertIsNone(config.static_bearer)
        self.assertEqual(config.db_path, ac.DEFAULT_AUTH_DB)
        config, errors = ac.config_from_environ(good_env(AUTH_DB="   "))
        self.assertEqual((errors, config.db_path), ([], ac.DEFAULT_AUTH_DB))

    def test_config_is_frozen_and_repr_hides_secrets(self):
        token = fake_secret()
        env = good_env(MCP_BEARER_TOKEN=token)
        config, _ = ac.config_from_environ(env)
        text = repr(config) + str(config)
        self.assertNotIn(env["OWNER_SECRET"], text)
        self.assertNotIn(token, text)
        self.assertIn("https://coach.example.com", text)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.public_url = "https://other.example"  # type: ignore[misc]


class PublicUrl(unittest.TestCase):
    def check(self, value, expected, **extra):
        config, errors = ac.config_from_environ(good_env(PUBLIC_URL=value, **extra))
        self.assertEqual(errors, [], value)
        self.assertEqual(config.public_url, expected)

    def test_normalization(self):
        cases = {
            "https://Coach.Example.COM/": "https://coach.example.com",
            "HTTPS://coach.example.com": "https://coach.example.com",
            "https://coach.example.com:443": "https://coach.example.com",   # default port dropped
            "https://coach.example.com:443/": "https://coach.example.com",
            "https://coach.example.com:8443": "https://coach.example.com:8443",
            "  https://coach.example.com  ": "https://coach.example.com",
            "http://127.0.0.1:8334": "http://127.0.0.1:8334",
            "http://localhost:80": "http://localhost",                      # default port dropped
            "http://LOCALHOST:8000/": "http://localhost:8000",
            "http://[::1]:8000": "http://[::1]:8000",
            "https://your-app.up.railway.app": "https://your-app.up.railway.app",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.check(value, expected)

    def test_railway_public_domain_fallback(self):
        self.check(None, "https://your-app.up.railway.app", RAILWAY_PUBLIC_DOMAIN="your-app.up.railway.app")
        self.check(None, "https://your-app.up.railway.app", RAILWAY_PUBLIC_DOMAIN=" Your-App.up.railway.app ")
        self.check("", "https://your-app.up.railway.app", RAILWAY_PUBLIC_DOMAIN="your-app.up.railway.app")
        # PUBLIC_URL wins (a custom domain).
        self.check("https://coach.example.com", "https://coach.example.com",
                   RAILWAY_PUBLIC_DOMAIN="your-app.up.railway.app")

    def test_railway_domain_must_pass_a3(self):
        _, errors = ac.config_from_environ(good_env(PUBLIC_URL=None, RAILWAY_PUBLIC_DOMAIN="x.example/path"))
        self.assertEqual(errors, [ERR_A3])

    def test_a1_unset(self):
        for env in (good_env(PUBLIC_URL=None), good_env(PUBLIC_URL="  ", RAILWAY_PUBLIC_DOMAIN=" ")):
            config, errors = ac.config_from_environ(env)
            self.assertIsNone(config)
            self.assertEqual(errors, [ERR_A1])

    def test_http_loopback_allowed_off_railway(self):
        for value in ("http://127.0.0.1:8334", "http://localhost:8000", "http://[::1]:9"):
            with self.subTest(value=value):
                config, errors = ac.config_from_environ(good_env(PUBLIC_URL=value))
                self.assertEqual(errors, [])

    def test_http_refused_on_railway(self):
        for name in ac.RAILWAY_DEPLOYMENT_VARS:
            with self.subTest(var=name):
                _, errors = ac.config_from_environ(good_env(PUBLIC_URL="http://127.0.0.1:8334", **{name: "x"}))
                self.assertEqual(errors, [ERR_A2])
        # Present but blank does not count; https is fine on Railway.
        _, errors = ac.config_from_environ(good_env(PUBLIC_URL="http://127.0.0.1:8334", RAILWAY_PROJECT_ID=" "))
        self.assertEqual(errors, [])
        _, errors = ac.config_from_environ(good_env(RAILWAY_DEPLOYMENT_ID="d"))
        self.assertEqual(errors, [])

    def test_a2_scheme(self):
        cases = {
            "http://example.com": [ERR_A2],
            "http://127.0.0.2:80": [ERR_A2],          # only the three loopback names
            "ftp://coach.example.com": [ERR_A2],
            "coach.example.com": [ERR_A2, ERR_A3],    # no scheme: also no host
            "http://example.com/mcp": [ERR_A2, ERR_A3],
            "http://[::1": [ERR_A2, ERR_A3],          # unparseable
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                config, errors = ac.config_from_environ(good_env(PUBLIC_URL=value))
                self.assertIsNone(config)
                self.assertEqual(errors, expected)

    def test_a3_origin_only(self):
        for value in (
            "https://coach.example.com/mcp",
            "https://coach.example.com/x/",
            "https://coach.example.com?a=1",
            "https://coach.example.com/?",
            "https://coach.example.com#x",
            "https://coach.example.com/#",
            "https://user:pass@coach.example.com",
            "https://user@coach.example.com",
            "https://@coach.example.com",
            "https://",
            "https:///path",
            "https://coach.example.com:99999",
            "https://coach.example.com:abc",
            "https://coach.example.com:0",
            "https://exa mple.com",
            "https://coach%2eexample.com",
            "https://[zz::1]",
            "https://[::1",
        ):
            with self.subTest(value=value):
                config, errors = ac.config_from_environ(good_env(PUBLIC_URL=value))
                self.assertIsNone(config)
                self.assertEqual(errors, [ERR_A3])


class OwnerSecret(unittest.TestCase):
    def errors(self, secret, **extra):
        return ac.config_from_environ(good_env(OWNER_SECRET=secret, **extra))[1]

    def test_a4_not_set(self):
        self.assertEqual(self.errors(None), [ERR_A4])
        self.assertEqual(self.errors(""), [ERR_A4])

    def test_a5_shape(self):
        base = "abcdefghijklmnopqrstuvwxyz0123456789"
        for secret in (
            base[:31],                         # too short
            (base * 8)[:257],                  # too long
            " " + base,                        # leading space
            base + " ",                        # trailing space
            "   ",                             # blank
            base[:20] + "\t" + base[:20],      # tab is not printable
            base[:20] + "\n" + base[:20],
            base[:30] + "éé",        # not ASCII
            base[:30] + "\x7f\x7f",
        ):
            with self.subTest(length=len(secret)):
                self.assertEqual(self.errors(secret), [ERR_A5])

    def test_a5_bounds_pass(self):
        base = "abcdefghijklmnopqrstuvwxyz0123456789"
        self.assertEqual(self.errors(base[:32]), [])
        self.assertEqual(self.errors((base * 8)[:256]), [])
        self.assertEqual(self.errors("a b c d e f g h i j k l m n o p q"), [])   # inner spaces are fine

    def test_a6_only_after_a5(self):
        self.assertEqual(self.errors("ab" * 16), [ERR_A6])
        self.assertEqual(self.errors("abcdefghijk" * 3), [ERR_A6])    # 11 distinct characters
        self.assertEqual(self.errors("abcdefghijkl" * 3), [])         # 12
        self.assertEqual(self.errors("ab" * 10), [ERR_A5])            # A5 wins, A6 not checked

    def test_a7_same_as_bearer(self):
        secret = fake_secret()
        self.assertEqual(self.errors(secret, MCP_BEARER_TOKEN=secret), [ERR_A7])
        # Checked only when both values are valid on their own.
        self.assertEqual(self.errors("ab" * 16, MCP_BEARER_TOKEN="ab" * 16), [ERR_A6])
        spaced = "a b c d e f g h i j k l m n o p q"   # a valid owner secret, not a valid bearer
        self.assertEqual(self.errors(spaced, MCP_BEARER_TOKEN=spaced), [ERR_A8])


class StaticBearer(unittest.TestCase):
    def errors(self, token):
        return ac.config_from_environ(good_env(MCP_BEARER_TOKEN=token))[1]

    def test_a8(self):
        for token in ("a" * 31, "a" * 513, "bad token" * 5, "a" * 40 + "+", "a" * 40 + "/", "a" * 40 + "=",
                      " " + "a" * 40, "a" * 40 + "\n", "é" * 40):
            with self.subTest(length=len(token)):
                self.assertEqual(self.errors(token), [ERR_A8])

    def test_valid(self):
        for token in ("a" * 32, "a" * 512, "Az09._~-" * 5, secrets.token_urlsafe(32)):
            with self.subTest(length=len(token)):
                self.assertEqual(self.errors(token), [])


class AuthDb(unittest.TestCase):
    def test_a9(self):
        for value in ("relative.sqlite", "data/auth.sqlite", "/data/", "/", " /data/auth.sqlite",
                      "/data/auth.sqlite ", "/data/..", "/data/.", "/data/a\x00b", "~/auth.sqlite"):
            with self.subTest(value=value):
                config, errors = ac.config_from_environ(good_env(AUTH_DB=value))
                self.assertIsNone(config)
                self.assertEqual(errors, [ERR_A9])

    def test_valid(self):
        config, errors = ac.config_from_environ(good_env(AUTH_DB="/var/lib/coach/auth.db"))
        self.assertEqual((errors, config.db_path), ([], "/var/lib/coach/auth.db"))


class Origins(unittest.TestCase):
    """ALLOWED_ORIGINS and ORIGIN_CHECK: the /mcp Origin check (owner_auth.OriginCheck)."""

    def test_defaults(self):
        config, errors = ac.config_from_environ(good_env())
        self.assertEqual((errors, config.extra_origins, config.origin_check), ([], (), True))
        self.assertEqual(config.allowed_origins,
                         frozenset({"https://coach.example.com", "https://claude.ai", "https://claude.com"}))
        self.assertEqual(ac.CLAUDE_ORIGINS, ("https://claude.ai", "https://claude.com"))

    def test_entries_are_normalized_and_deduplicated(self):
        config, errors = ac.config_from_environ(good_env(
            ALLOWED_ORIGINS=" HTTP://LocalHost:6274 , ,https://App.example.com:443/,https://app.example.com"))
        self.assertEqual(errors, [])
        self.assertEqual(config.extra_origins, ("http://localhost:6274", "https://app.example.com"))
        self.assertIn("http://localhost:6274", config.allowed_origins)

    def test_bad_entries_are_numbered_without_the_value(self):
        for value in ("bad.example", "https://bad.example/path", "ftp://bad.example", "null",
                      "https://u@bad.example", "https://bad.example?x", "https://bad.example:0",
                      "https://bad.example:99999", "https://bad host", "*"):
            with self.subTest(value=value):
                config, errors = ac.config_from_environ(good_env(ALLOWED_ORIGINS="https://ok.example," + value))
                self.assertIsNone(config)
                self.assertEqual(errors, [ac.ERR_A13.format(n=2)])
                self.assertNotIn(value, errors[0])

    def test_plain_http_only_on_loopback_and_never_on_railway(self):
        for value in ("http://localhost:6274", "http://127.0.0.1:8080", "http://[::1]:3000"):
            with self.subTest(value=value):
                config, errors = ac.config_from_environ(good_env(ALLOWED_ORIGINS=value))
                self.assertEqual(errors, [])
                self.assertEqual(len(config.extra_origins), 1)
        for value in ("http://app.example.com", "http://10.0.0.5:8080", "http://localhost.example.com"):
            with self.subTest(value=value):
                config, errors = ac.config_from_environ(good_env(ALLOWED_ORIGINS="https://ok.example," + value))
                self.assertIsNone(config)
                self.assertEqual(errors, [ac.ERR_A13.format(n=2)])
        env = good_env(ALLOWED_ORIGINS="http://localhost:6274", RAILWAY_SERVICE_ID="svc")
        self.assertEqual(ac.config_from_environ(env)[1], [ac.ERR_A13.format(n=1)])

    def test_at_most_ten(self):
        many = ",".join("https://o%d.example" % n for n in range(11))
        self.assertEqual(ac.config_from_environ(good_env(ALLOWED_ORIGINS=many))[1], [ac.ERR_A14])

    def test_origin_check_switch(self):
        for value, enabled in (("", True), ("on", True), (" ON ", True), ("off", False), ("Off", False)):
            with self.subTest(value=value):
                config, errors = ac.config_from_environ(good_env(ORIGIN_CHECK=value))
                self.assertEqual((errors, config.origin_check), ([], enabled))
        for value in ("no", "0", "false", "disabled"):
            with self.subTest(value=value):
                self.assertEqual(ac.config_from_environ(good_env(ORIGIN_CHECK=value))[1], [ac.ERR_A15])

    def test_normal_origin(self):
        cases = {
            "https://claude.ai": "https://claude.ai",
            "HTTPS://CLAUDE.AI:443": "https://claude.ai",
            "http://localhost:80": "http://localhost",
            "http://[::1]:8000": "http://[::1]:8000",
            "https://claude.ai/": "https://claude.ai",
            "null": None, "": None, "https://claude.ai//": None, "https://claude.ai#x": None,
            "https://claude.ai\n": None, "https://[::1": None, "https://claude.ai:x": None, "file:///x": None,
            "https://cl%61ude.ai": None, "https://" + "a" * 600: None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw[:30]):
                self.assertEqual(ac.normal_origin(raw), expected)


class RedirectUris(unittest.TestCase):
    def errors(self, value):
        return ac.config_from_environ(good_env(ALLOWED_REDIRECT_URIS=value))[1]

    def test_a10_each_entry_numbered_without_the_value(self):
        bad = [
            "http://x.example/cb",
            "https:///cb",
            "https://user@x.example/cb",
            "https://x.example/cb#frag",
            "https://x.example/cb#",
            "https://x.example/a b",
            "x.example/cb",
            "https://x.example/" + "a" * 500,
            "https://x.example:99999/cb",
            "javascript:alert(1)",
        ]
        for value in bad:
            with self.subTest(value=value):
                lines = self.errors("https://ok.example/cb," + value)
                self.assertEqual(lines, [a10(2)])
                self.assertNotIn(value, lines[0])

    def test_numbering_skips_empty_entries(self):
        self.assertEqual(self.errors(" , https://ok.example/cb , , http://bad.example/cb"), [a10(2)])

    def test_a11_at_most_ten(self):
        ten = ",".join(f"https://x{i}.example/cb" for i in range(10))
        self.assertEqual(self.errors(ten), [])
        eleven = ten + ",https://x10.example/cb"
        self.assertEqual(self.errors(eleven), [ERR_A11])
        # A10 lines come first, then A11.
        self.assertEqual(self.errors(eleven + ",http://bad.example/cb"), [a10(12), ERR_A11])

    def test_valid_entries(self):
        self.assertEqual(self.errors("https://claude.ai/api/mcp/auth_callback,https://x.example:8443/cb?a=1"), [])


class Order(unittest.TestCase):
    def test_e2e_bad_environment(self):
        # scripts/e2e-local.py expects exactly these four lines.
        env = good_env(PUBLIC_URL="http://example.com/mcp", OWNER_SECRET="short", MCP_BEARER_TOKEN="bad token")
        config, errors = ac.config_from_environ(env)
        self.assertIsNone(config)
        self.assertEqual(errors, [ERR_A2, ERR_A3, ERR_A5, ERR_A8])

    def test_every_error_in_order(self):
        extras = ",".join(f"https://x{i}.example/cb" for i in range(9))
        env = {
            "GARMIN_MCP_TRANSPORT": "SSE ",
            "PUBLIC_URL": "http://example.com/x",
            "OWNER_SECRET": "short",
            "MCP_BEARER_TOKEN": "bad token",
            "AUTH_DB": "relative.sqlite",
            "ALLOWED_REDIRECT_URIS": "http://bad.example/cb," + extras + ",https://u@x.example/cb",
        }
        config, errors = ac.config_from_environ(env)
        self.assertIsNone(config)
        self.assertEqual(errors, [ERR_A12, ERR_A2, ERR_A3, ERR_A5, ERR_A8, ERR_A9, a10(1), a10(11), ERR_A11])

    def test_a1_a4_a6_order(self):
        _, errors = ac.config_from_environ({"GARMIN_MCP_TRANSPORT": "sse"})
        self.assertEqual(errors, [ERR_A12, ERR_A1, ERR_A4])
        _, errors = ac.config_from_environ(good_env(PUBLIC_URL=None, OWNER_SECRET="ab" * 16, AUTH_DB="x"))
        self.assertEqual(errors, [ERR_A1, ERR_A6, ERR_A9])

    def test_a12_only_for_sse(self):
        for transport, expected in (("sse", [ERR_A12]), (" Sse ", [ERR_A12]), ("streamable-http", [])):
            with self.subTest(transport=transport):
                self.assertEqual(ac.config_from_environ(good_env(GARMIN_MCP_TRANSPORT=transport))[1], expected)


class NoSecretInErrors(unittest.TestCase):
    def test_no_error_line_carries_a_secret(self):
        owner, token = fake_secret(), fake_secret()
        environments = [
            good_env(OWNER_SECRET=owner, MCP_BEARER_TOKEN=owner),                        # A7
            good_env(OWNER_SECRET=owner + " ", MCP_BEARER_TOKEN=token + "!"),            # A5, A8
            good_env(OWNER_SECRET=" " + owner, MCP_BEARER_TOKEN="x" + token + " "),      # A5, A8
            good_env(OWNER_SECRET=owner[:31], MCP_BEARER_TOKEN=token[:31]),              # A5, A8
            good_env(OWNER_SECRET=owner + "é", PUBLIC_URL="http://" + owner,
                     AUTH_DB=owner, ALLOWED_REDIRECT_URIS="http://" + token + "/cb," + owner),
            good_env(OWNER_SECRET=owner * 7, MCP_BEARER_TOKEN=token * 13),               # too long
        ]
        for env in environments:
            _, errors = ac.config_from_environ(env)
            self.assertTrue(errors)
            for line in errors:
                for value in (owner, token, owner[:16], token[:16]):
                    self.assertNotIn(value, line)

    def test_never_raises(self):
        weird = ["", " ", "\x00", "http://[::1", "https://[", "https://:80", "https://a:b:c", "☃" * 100,
                 "https://" + "a" * 5000, "%", "https://x:-1", "https://x:1.5", "[::1]", "//x"]
        for value in weird:
            for key in ("PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN", "OWNER_SECRET", "MCP_BEARER_TOKEN", "AUTH_DB",
                        "ALLOWED_REDIRECT_URIS", "GARMIN_MCP_TRANSPORT", "ALLOWED_ORIGINS", "ORIGIN_CHECK"):
                with self.subTest(key=key, value=value[:20]):
                    config, errors = ac.config_from_environ(good_env(**{key: value}))
                    self.assertTrue(config is None or not errors)
                    self.assertIsInstance(errors, list)



class EdgeHeader(unittest.TestCase):
    """EDGE_CLIENT_IP_HEADER (the three lines the deploy docs quote, word for word)."""

    def test_texts(self):
        self.assertEqual(ac.EDGE_ENV, "EDGE_CLIENT_IP_HEADER")
        self.assertEqual(ac.EDGE_HEADERS, ("X-Real-IP", "CF-Connecting-IP"))
        self.assertEqual(ac.ERR_E1, "EDGE_CLIENT_IP_HEADER is set but empty: delete the variable (Railway) or set "
                                    "CF-Connecting-IP (Docker Compose behind a Cloudflare Tunnel)")
        self.assertEqual(ac.ERR_E2, "EDGE_CLIENT_IP_HEADER must be X-Real-IP or CF-Connecting-IP; the value set is "
                                    "neither")
        self.assertEqual(ac.ERR_E3, "EDGE_CLIENT_IP_HEADER=CF-Connecting-IP is only for Docker Compose behind a "
                                    "Cloudflare Tunnel: on Railway, delete the variable")
        for text in (ac.ERR_E1, ac.ERR_E2, ac.ERR_E3, ERR_A1, ERR_A4):
            self.assertTrue(text.isascii() and text.isprintable(), text)

    def test_accepted(self):
        for env in ({}, {"EDGE_CLIENT_IP_HEADER": "X-Real-IP"}, {"EDGE_CLIENT_IP_HEADER": " CF-Connecting-IP "},
                    {"EDGE_CLIENT_IP_HEADER": "X-Real-IP", "RAILWAY_PROJECT_ID": "p"},
                    {"EDGE_CLIENT_IP_HEADER": "CF-Connecting-IP", "RAILWAY_PROJECT_ID": " "}):
            with self.subTest(env=env):
                self.assertEqual(ac.edge_header_errors(env), [])

    def test_refused(self):
        sentinel = "TEST-ONLY-" + secrets.token_hex(8)
        cases = [("", ac.ERR_E1), ("   ", ac.ERR_E1), ("x-real-ip", ac.ERR_E2), ("X-Forwarded-For", ac.ERR_E2),
                 ("X-Real-IP CF-Connecting-IP", ac.ERR_E2), (sentinel, ac.ERR_E2)]
        for value, expected in cases:
            with self.subTest(value=value):
                errors = ac.edge_header_errors({"EDGE_CLIENT_IP_HEADER": value})
                self.assertEqual(errors, [expected])
                self.assertNotIn(sentinel, "".join(errors))
        for name in ac.RAILWAY_DEPLOYMENT_VARS:
            with self.subTest(railway=name):
                env = {"EDGE_CLIENT_IP_HEADER": "CF-Connecting-IP", name: "x"}
                self.assertEqual(ac.edge_header_errors(env), [ac.ERR_E3])

    def test_on_railway_is_public(self):
        self.assertFalse(ac.on_railway({}))
        self.assertFalse(ac.on_railway({"RAILWAY_PROJECT_ID": "  "}))
        self.assertFalse(ac.on_railway({"RAILWAY_PUBLIC_DOMAIN": "x.up.railway.app"}))
        for name in ac.RAILWAY_DEPLOYMENT_VARS:
            self.assertTrue(ac.on_railway({name: "x"}), name)

    def test_deploy_neutral_texts(self):
        # Docker users see their own remedy too.
        self.assertIn("with Docker the tunnel's https://<hostname>", ERR_A1)
        self.assertIn("with Docker see docs/deploy-docker.md", ERR_A4)
        self.assertTrue(ERR_A4.startswith("OWNER_SECRET is not set: "))
        # 1.0.0 has no Railway template: deploy.sh is the only Railway path.
        self.assertIn("on Railway ./scripts/deploy.sh generates one", ERR_A4)
        self.assertNotIn("template", ERR_A4.lower())


if __name__ == "__main__":
    unittest.main()
