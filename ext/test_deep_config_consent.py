"""Deep tests (release test phase): the plan:write consent and scope boundaries of the sign-in.

The consent page with writes off is byte-identical to main's; plan:write comes only from the ticked box on a
page that offers it, over a same-origin form with the owner secret; a client never asks it into a grant;
refresh never widens; a plan:write in a code, grant or token row without its write_consent row is dropped;
the static bearer and a request without an auth context never write.

    cd ext && python -m unittest test_deep_config_consent

Needs mcp 2.x (the standard-library leg skips every test). Reuses test_owner_auth's SignInCase harness.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import unittest
from unittest import mock

import auth_store

from test_owner_auth import (CALLBACK, fake_secret, HAVE_MCP2, PAGE_HEADERS, query_of, SignInCase, why)

if HAVE_MCP2:
    import owner_auth
    import plan_confirm

WRITE = "plan:write"
# SHA-256 of main's (1.0.5) consent page bodies for fixed inputs (owner_auth._form_page, PUBLIC_URL
# https://coach.example.com, request id PENDING, started now): the claude.ai callback, the same after a wrong
# secret, and a loopback callback with no client name.
MAIN_PAGE_SHA256 = {
    ("https://claude.ai/api/mcp/auth_callback", "Claude", None, 200):
        "bc384df0cfbaa607a60ae2c70df9b60607d9c9880f99c47ce79506787f597a0f",
    ("https://claude.ai/api/mcp/auth_callback", "Claude", "Wrong secret.", 403):
        "a2530a2d0aa9597afa57a0e8e7ca66d0555c0e193348c50b8f9025f93eabc4b6",
    ("http://localhost:1234/callback", None, None, 200):
        "303f6c3d3a3f1079f03dccb56f92315a3d0773ef02b2aaa67c59d70868e7e220",
}
PLAN_ENV = ("PLAN_TOOLS", "PLAN_WRITES", "PLAN_TRACKING", "HISTORY_SEARCH", "ROLE", "GARMIN_AUTH", "DATABASE_URL",
            "HISTORY_TOKEN_KEY")


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class ConsentPageWritesOff(SignInCase):
    """With PLAN_WRITES off, or on without its prerequisites, the page is main's, byte for byte."""

    def page_hashes(self, auth):
        out = {}
        for (uri, name, error, status) in MAIN_PAGE_SHA256:
            row = {"redirect_uri": uri, "created_at": auth.provider.clock()}
            response = owner_auth._form_page(auth, "PENDING", row, name, error=error, status=status)
            out[(uri, name, error, status)] = hashlib.sha256(response.body).hexdigest()
            self.assertEqual(response.status_code, status)
            for header, value in PAGE_HEADERS.items():
                self.assertEqual(response.headers.get(header), value, header)
        return out

    def test_byte_identical_to_main(self):
        cleared = {name: "" for name in PLAN_ENV}
        environments = (
            {},
            {"PLAN_WRITES": "on"},                                                  # without PLAN_TOOLS
            {"PLAN_WRITES": "on", "PLAN_TOOLS": "on"},                              # without HISTORY_SEARCH
            {"PLAN_WRITES": "ON", "PLAN_TOOLS": "on", "HISTORY_SEARCH": "on", "GARMIN_AUTH": "key",
             "DATABASE_URL": "postgresql://u:TEST-ONLY@db/x"},                     # key mode
            {"PLAN_WRITES": "yes", "PLAN_TOOLS": "on", "HISTORY_SEARCH": "on", "GARMIN_AUTH": "full",
             "DATABASE_URL": "postgresql://u:TEST-ONLY@db/x"},                     # not "on"
        )
        for env in environments:
            with self.subTest(env=sorted(env.items())), mock.patch.dict(os.environ, {**cleared, **env}):
                auth = owner_auth.build(self.config(), clock=self.clock)     # the default writes_offered
                self.addCleanup(auth.provider.store.close)
                self.assertFalse(auth.provider.writes_offered())
                self.assertEqual(self.page_hashes(auth), MAIN_PAGE_SHA256)
        auth = owner_auth.build(self.config(), clock=self.clock, writes_offered=lambda: False)
        self.addCleanup(auth.provider.store.close)
        self.assertEqual(self.page_hashes(auth), MAIN_PAGE_SHA256)

    def test_over_http_the_page_has_no_box(self):
        with mock.patch.dict(os.environ, {name: "" for name in PLAN_ENV}):
            flow = self.start(self.register())
            response = self.client.get("/consent", params={"req": flow["pending"]})
        self.assertEqual(response.status_code, 200, why(response))
        for absent in ("plan_write", "checkbox", "calendar", "fieldset", "unless you tick", "label.check"):
            self.assertNotIn(absent, response.text)


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class WriteScopeBoundaries(SignInCase):
    """plan:write from the box only, never from the client, never wider on refresh, never without its row."""

    with_static_bearer = True
    writes = True

    def build(self, **changes):
        auth = owner_auth.build(self.config(**changes), clock=self.clock, writes_offered=lambda: self.writes)
        self.addCleanup(auth.provider.store.close)
        return auth

    def consent_rows(self):
        return [row[0] for row in self.sql("SELECT consent_id FROM write_consent ORDER BY consent_id")]

    def sign_in_with(self, fields=None, client=None, **kwargs):
        flow = self.start(client or self.register(), **kwargs)
        response = self.post_consent(flow["pending"], secret=self.owner_secret, **(fields or {}))
        self.assertEqual(response.status_code, 303, why(response))
        flow["code"] = query_of(response.headers["location"])["code"]
        response = self.exchange(flow)
        self.assertEqual(response.status_code, 200, why(response))
        flow["tokens"] = response.json()
        return flow

    def may_write(self, access_token):
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        token = self.run_async(self.provider.load_access_token(access_token))
        if token is None:
            return False
        reset = auth_context_var.set(AuthenticatedUser(token))
        try:
            return plan_confirm.caller_may_write()
        finally:
            auth_context_var.reset(reset)

    def scopes_of(self, flow):
        return (flow["tokens"].get("scope") or "").split()

    # the box

    def test_only_an_exact_yes_on_the_first_field_ticks_the_box(self):
        for fields, granted in (({"plan_write": "yes"}, True), ({}, False), ({"plan_write": "YES"}, False),
                                ({"plan_write": "yes "}, False), ({"plan_write": "1"}, False),
                                ({"plan_write": "true"}, False), ({"plan_write": "on"}, False),
                                ({"plan_write": ""}, False), ({"plan_write": ["no", "yes"]}, False),
                                ({"Plan_Write": "yes"}, False), ({"plan_write ": "yes"}, False),
                                ({"plan:write": "yes"}, False), ({"scope": WRITE}, False)):
            with self.subTest(fields=fields):
                flow = self.sign_in_with(fields)
                self.assertEqual(WRITE in self.scopes_of(flow), granted)
                self.assertEqual(self.may_write(flow["tokens"]["access_token"]), granted)
                self.assertEqual(self.scopes_of(flow).count(WRITE), 1 if granted else 0)

    def test_a_ticked_box_with_writes_off_grants_nothing_and_stores_no_row(self):
        self.writes = False
        flow = self.sign_in_with({"plan_write": "yes"}, scope="offline_access plan:write")
        self.assertNotIn(WRITE, self.scopes_of(flow))
        self.assertEqual(self.consent_rows(), [])
        self.assertFalse(self.may_write(flow["tokens"]["access_token"]))

    # the client's request

    def test_a_client_never_asks_the_scope_into_a_grant(self):
        for scope in ("plan:write", "offline_access plan:write", "plan:write plan:write", "PLAN:WRITE",
                      "plan:write\xa0claudeai", "offline_access\tplan:write", "plan%3Awrite", "plan:write "):
            with self.subTest(scope=repr(scope)):
                flow = self.sign_in_with(scope=scope)
                self.assertNotIn(WRITE, self.scopes_of(flow))
                self.assertFalse(self.may_write(flow["tokens"]["access_token"]))
                self.assertFalse(self.may_write(self.rotate(flow["client"], flow["tokens"]["refresh_token"])
                                                ["access_token"]))
        self.assertEqual(self.consent_rows(), [])

    def test_a_ticked_box_and_a_requested_scope_give_one_scope(self):
        flow = self.sign_in_with({"plan_write": "yes"}, scope="plan:write offline_access plan:write")
        self.assertEqual(self.scopes_of(flow), ["offline_access", WRITE])

    # refresh

    def test_refresh_never_widens(self):
        flow = self.sign_in_with()
        for scope in ("offline_access plan:write", WRITE, "offline_access claudeai", "PLAN:WRITE"):
            with self.subTest(scope=scope):
                refused = self.refresh(flow["client"], flow["tokens"]["refresh_token"], scope=scope)
                self.assertEqual((refused.status_code, refused.json()["error"]), (400, "invalid_scope"))
        rotated = self.rotate(flow["client"], flow["tokens"]["refresh_token"])
        self.assertEqual(rotated["scope"], "offline_access")
        self.assertFalse(self.may_write(rotated["access_token"]))

    def test_a_narrowed_refresh_keeps_the_grant_and_never_comes_back_wider_than_the_grant(self):
        flow = self.sign_in_with({"plan_write": "yes"})
        narrowed = self.refresh(flow["client"], flow["tokens"]["refresh_token"], scope="offline_access").json()
        self.assertFalse(self.may_write(narrowed["access_token"]))
        again = self.rotate(flow["client"], narrowed["refresh_token"])   # the refresh token keeps the grant's
        self.assertTrue(self.may_write(again["access_token"]))
        only = self.refresh(flow["client"], again["refresh_token"], scope=WRITE).json()
        self.assertEqual(only["scope"], WRITE)
        self.assertTrue(self.may_write(only["access_token"]))

    # rows without their consent

    def test_a_grant_whose_consent_row_is_gone_stops_writing(self):
        flow = self.sign_in_with({"plan_write": "yes"})
        self.assertTrue(self.may_write(flow["tokens"]["access_token"]))
        db = __import__("sqlite3").connect(self.db_path)
        with db:
            db.execute("DELETE FROM write_consent")
        db.close()
        self.assertFalse(self.may_write(flow["tokens"]["access_token"]))
        rotated = self.rotate(flow["client"], flow["tokens"]["refresh_token"])
        self.assertNotIn(WRITE, (rotated.get("scope") or "").split())
        self.assertFalse(self.may_write(rotated["access_token"]))

    def test_an_old_code_with_the_scope_but_no_consent_row_is_dropped(self):
        flow = self.start(self.register())
        code = "ac_" + secrets.token_urlsafe(32)
        taken = self.store.approve_pending(auth_store.hash_secret(flow["pending"]), code_hash=auth_store.hash_secret(code),
                                           code_ttl=60, scopes="offline_access plan:write", write_consent=False)
        self.assertIsNotNone(taken)
        flow["code"] = code
        response = self.exchange(flow)
        self.assertEqual(response.status_code, 200, why(response))
        self.assertNotIn(WRITE, (response.json().get("scope") or "").split())
        self.assertFalse(self.may_write(response.json()["access_token"]))
        self.assertEqual(self.consent_rows(), [])

    def test_token_rows_with_the_scope_but_no_consent_row_are_dropped(self):
        ticked = self.sign_in_with({"plan_write": "yes"})
        plain = self.sign_in_with()
        db = __import__("sqlite3").connect(self.db_path)
        with db:
            db.execute("UPDATE tokens SET scopes = 'offline_access plan:write'")
            db.execute("UPDATE grants SET scopes = 'offline_access plan:write'")
        db.close()
        self.assertTrue(self.may_write(ticked["tokens"]["access_token"]))
        self.assertFalse(self.may_write(plain["tokens"]["access_token"]))
        rotated = self.rotate(plain["client"], plain["tokens"]["refresh_token"])
        self.assertFalse(self.may_write(rotated["access_token"]))
        refused = self.refresh(plain["client"], rotated["refresh_token"], scope=WRITE)
        self.assertEqual(refused.status_code, 400, why(refused))
        # One grant's consent row is its own: the ticked grant's row never covers the plain one.
        self.assertEqual(len([r for r in self.consent_rows() if r.startswith("grant:")]), 1)

    # the static bearer and no auth context

    def test_the_static_bearer_never_writes(self):
        token = self.run_async(self.provider.load_access_token(self.bearer))
        self.assertEqual((token.client_id, token.scopes), (owner_auth.STATIC_CLIENT_ID, []))
        self.assertFalse(self.may_write(self.bearer))
        self.assert_mcp(self.bearer, 200)

    def test_a_client_cannot_register_as_the_static_bearer(self):
        info = self.register(client_id=owner_auth.STATIC_CLIENT_ID)
        self.assertNotEqual(info["client_id"], owner_auth.STATIC_CLIENT_ID)

    def test_no_auth_context_and_odd_tokens_never_write(self):
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        from mcp.server.auth.provider import AccessToken

        self.assertFalse(plan_confirm.caller_may_write())
        for scopes in ([], ["PLAN:WRITE"], ["plan:write\u200b"], ["offline_access plan:write"], [" plan:write"],
                       ["plan:writes"], ["plan"]):
            token = AccessToken(token="TEST-ONLY-token", client_id="c1", scopes=scopes, expires_at=None)
            reset = auth_context_var.set(AuthenticatedUser(token))
            try:
                with self.subTest(scopes=scopes):
                    self.assertFalse(plan_confirm.caller_may_write())
            finally:
                auth_context_var.reset(reset)

    # the form itself

    def test_the_box_post_needs_the_same_site_form_and_the_owner_secret(self):
        flow = self.start(self.register())
        ticked = {"plan_write": "yes"}
        cases = (
            dict(secret=self.owner_secret, site="cross-site", **ticked),
            dict(secret=self.owner_secret, site="same-site", **ticked),
            dict(secret=fake_secret(), **ticked),
            dict(secret="", **ticked),
            dict(secret=None, **ticked),
            dict(secret=self.owner_secret, action="maybe", **ticked),
        )
        for kwargs in cases:
            with self.subTest(kwargs={k: v for k, v in kwargs.items() if k != "secret"}):
                response = self.post_consent(flow["pending"], **kwargs)
                self.assertIn(response.status_code, (400, 403), why(response))
                self.assertNotIn("location", response.headers)
                self.assertEqual(self.consent_rows(), [])
            if kwargs.get("secret") != self.owner_secret:
                flow = self.start(self.register())   # a fresh request: three wrong secrets end one
        response = self.client.post("/consent", json={"req": flow["pending"], "secret": self.owner_secret,
                                                      "action": "approve", "plan_write": "yes"},
                                    headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 400, why(response))
        self.assertEqual(self.consent_rows(), [])
        # The third wrong secret ends the request with access_denied, never a code.
        for _ in range(3):
            response = self.post_consent(flow["pending"], secret=fake_secret(), plan_write="yes")
        self.assertEqual(response.status_code, 303, why(response))
        self.assertEqual(query_of(response.headers["location"]).get("error"), "access_denied")
        self.assertNotIn("code", query_of(response.headers["location"]))
        self.assertEqual(self.consent_rows(), [])
        # The refused posts left the request usable, and an unticked approval grants read access only.
        flow = self.start(self.register())
        self.post_consent(flow["pending"], secret=self.owner_secret, site="cross-site", plan_write="yes")
        response = self.post_consent(flow["pending"], secret=self.owner_secret)
        self.assertEqual(response.status_code, 303, why(response))
        flow["code"] = query_of(response.headers["location"])["code"]
        self.assertNotIn(WRITE, (self.exchange(flow).json().get("scope") or "").split())
        self.assertEqual(self.consent_rows(), [])

    def test_deny_with_a_ticked_box_stores_nothing(self):
        flow = self.start(self.register())
        response = self.post_consent(flow["pending"], action="deny", plan_write="yes")
        self.assertEqual(response.status_code, 303, why(response))
        self.assertTrue(response.headers["location"].startswith(CALLBACK + "?"))
        self.assertEqual(query_of(response.headers["location"])["error"], "access_denied")
        self.assertEqual(self.consent_rows(), [])

    def test_no_secret_or_scope_value_reaches_the_log(self):
        flow = self.sign_in_with({"plan_write": "yes"}, scope="offline_access plan:write claudeai")
        text = self.logs.server_text()
        for value in (self.owner_secret, flow["code"], flow["tokens"]["access_token"],
                      flow["tokens"]["refresh_token"], flow["pending"], flow["verifier"], flow["state"]):
            self.assertNotIn(value, text)
