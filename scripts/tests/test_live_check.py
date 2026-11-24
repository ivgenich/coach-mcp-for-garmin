"""Tests for scripts/live-check.py with a fake `railway` on PATH and loopback stubs.

No network beyond 127.0.0.1, no Docker, no real Railway project. Every token
and secret is built at run time.
"""

import ast
import json
import os
import re
import secrets
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livefakes  # noqa: E402

STAGING = "coach-mcp-staging"
DOMAIN = "coach-mcp-staging-production-" + "abcd.up.railway.app"
URL = "https://" + DOMAIN
IPV4_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
IPV6_RE = re.compile(r"\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){2,7}\b")


def status_json(name):
    return json.dumps({"id": "p-" + secrets.token_hex(4), "name": name, "environments": []})


def fake_token():
    # raw, percent-encoded and JSON-escaped forms all differ
    return secrets.token_hex(8) + '/+="\\' + secrets.token_hex(8)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = livefakes.temp_dir(self)
        self.token = fake_token()
        self.owner = secrets.token_urlsafe(24)
        self.variables = {"OWNER_SECRET": self.owner, "MCP_BEARER_TOKEN": self.token, "PUBLIC_URL": URL,
                          "RAILWAY_PUBLIC_DOMAIN": DOMAIN, "GARMIN_AUTH": "key", "REPORT_TZ": "UTC"}
        self.railway = livefakes.FakeRailway(self.tmp, [])
        self.set_rules([])

    def kv(self):
        return "".join(f"{k}={v}\n" for k, v in self.variables.items())

    def set_rules(self, rules, name=STAGING):
        base = [{"args": ["status", "--json"], "stdout": status_json(name)},
                {"args": ["variable", "list", "--kv"], "stdout": self.kv()}]
        self.railway.set_rules(list(rules) + base)

    def env(self, **extra):
        env = dict(os.environ)
        env["PATH"] = self.railway.path_env()
        env.update({"LIVE_CHECK_TEST_TIMEOUT": "10", "LIVE_CHECK_TEST_POLL": "0.1", "LIVE_CHECK_TEST_WAIT": "1"})
        env.update(extra)
        return env

    def lc(self, *args, env=None, **extra):
        return livefakes.run_script(livefakes.LIVE_CHECK, ["--dir", self.tmp] + list(args),
                                    env=env or self.env(**extra))

    def assertNoSecrets(self, out):
        for v in (self.token, self.owner, urllib.parse.quote(self.token, safe=""), json.dumps(self.token)[1:-1]):
            self.assertNotIn(v, out)


class GuardTest(Base):
    def test_pass(self):
        code, out = self.lc("guard")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS guard: linked to Railway project coach-mcp-staging", out)

    def test_other_project_stops_before_anything_else(self):
        self.set_rules([], name="my-live-server")
        code, out = self.lc("url")
        self.assertEqual(code, 2, out)
        self.assertIn('linked to project "my-live-server", not coach-mcp-staging. Nothing was run.', out)
        self.assertEqual(self.railway.calls(), [["status", "--json"]])

    def test_dryrun_project(self):
        self.set_rules([], name="coach-mcp-dryrun")
        code, out = self.lc("--project", "coach-mcp-dryrun", "guard")
        self.assertEqual(code, 0, out)
        code, out = self.lc("guard")
        self.assertEqual(code, 2, out)

    def test_unlinked(self):
        self.railway.set_rules([{"args": ["status"], "stdout": "", "stderr": "No linked project found. Run "
                                 "railway link to connect to a project\n", "exit": 1}])
        code, out = self.lc("guard")
        self.assertEqual(code, 2, out)
        self.assertIn("FAIL guard: this folder is not linked to a Railway project. Nothing was run.", out)
        code, out = self.lc("guard", "--allow-unlinked")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS guard: this folder is not linked to any Railway project (first deploy)", out)

    def test_cannot_confirm(self):
        cases = [{"stdout": "not json"}, {"stdout": json.dumps({"id": "x"})},
                 {"stdout": json.dumps({"project": {"name": STAGING}}), "exit": 3},
                 {"stdout": status_json(STAGING), "sleep": 3}]
        for rule in cases:
            with self.subTest(rule=rule):
                self.railway.set_rules([dict(rule, args=["status"])])
                code, out = self.lc("guard", LIVE_CHECK_TEST_TIMEOUT="1")
                self.assertEqual(code, 2, out)
                self.assertIn("FAIL guard: cannot confirm the linked project (", out)

    def test_nested_name_shapes(self):
        for data in ({"project": {"name": STAGING}}, {"projectName": STAGING}):
            self.railway.set_rules([{"args": ["status"], "stdout": json.dumps(data)}])
            code, out = self.lc("guard")
            self.assertEqual(code, 0, out)

    def test_cli_missing(self):
        empty = os.path.join(self.tmp, "empty")
        os.mkdir(empty)
        env = self.env()
        env["PATH"] = empty
        code, out = self.lc("guard", env=env)
        self.assertEqual(code, 2, out)
        self.assertIn("cannot confirm the linked project (CLI missing)", out)

    def test_project_refused_before_any_call(self):
        code, out = self.lc("--project", "live-thing", "guard")
        self.assertEqual(code, 2)
        self.assertIn("live-check: --project must be coach-mcp-staging or coach-mcp-dryrun", out)
        code, out = self.lc("url", "--project", "my-live-server")
        self.assertEqual(code, 2)
        self.assertEqual(self.railway.calls(), [])

    def test_hostile_project_name_not_printed(self):
        self.set_rules([], name="evil\nname; rm -rf")
        code, out = self.lc("guard")
        self.assertEqual(code, 2)
        self.assertIn('"another project"', out)
        self.assertNotIn("rm -rf", out)


class UrlTest(Base):
    def test_public_url_wins(self):
        self.variables["PUBLIC_URL"] = "https://custom.example.org"
        self.set_rules([])
        code, out = self.lc("url")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "https://custom.example.org")
        self.assertNoSecrets(out)

    def test_domain(self):
        del self.variables["PUBLIC_URL"]
        self.set_rules([])
        code, out = self.lc("url")
        self.assertEqual(out.strip().splitlines()[-1], URL)

    def test_neither(self):
        del self.variables["PUBLIC_URL"], self.variables["RAILWAY_PUBLIC_DOMAIN"]
        self.set_rules([])
        code, out = self.lc("url")
        self.assertEqual(code, 2, out)


CHILD = (
    "import json, os, sys, urllib.parse\n"
    "t = os.environ.get('MCP_BEARER_TOKEN', '')\n"
    "print(t)\n"
    "print(urllib.parse.quote(t, safe=''))\n"
    "print(json.dumps(t))\n"
    "print('owner in env:', 'OWNER_SECRET' in os.environ)\n"
    "print('garmin in env:', any(k.startswith('GARMIN_') for k in os.environ))\n"
    "print('args:', sys.argv[1:])\n"
    "sys.stderr.write('err ' + t + '\\n')\n"
    "sys.exit(int(os.environ.get('CHILD_EXIT', '0')))\n"
)


class RunTest(Base):
    def child(self, *args):
        return ["run", "--", sys.executable, "-B", "-c", CHILD] + list(args)

    def test_token_in_env_and_filtered(self):
        code, out = self.lc(*self.child(), OWNER_SECRET="x" * 20, GARMIN_PERSONAL_API_KEY="y" * 20, CHILD_EXIT="3")
        self.assertEqual(code, 3, out)
        self.assertIn("INFO run: MCP_BEARER_TOKEN from Railway is in the command's environment (not shown); "
                      "output filtered for 2 secret values", out)
        self.assertIn("INFO run: command exit 3", out)
        self.assertGreaterEqual(out.count("[redacted]"), 4)
        self.assertIn("owner in env: False", out)
        self.assertIn("garmin in env: False", out)
        self.assertNoSecrets(out)

    def test_no_token(self):
        del self.variables["MCP_BEARER_TOKEN"]
        self.set_rules([])
        marker = os.path.join(self.tmp, "started")
        code, out = self.lc("run", "--", sys.executable, "-B", "-c", f"open({marker!r}, 'w')")
        self.assertEqual(code, 2, out)
        self.assertIn("FAIL run: no MCP_BEARER_TOKEN on Railway. Create one: DEPLOY_SERVICE=coach-mcp-staging "
                      "./scripts/deploy.sh --rotate-token", out)
        self.assertFalse(os.path.exists(marker))

    def test_other_host_refused(self):
        marker = os.path.join(self.tmp, "started")
        starter = f"open({marker!r}, 'w')"
        for extra in (["https://other.example/x"], ["sh -c 'curl \"https://other.example\"'"],
                      ["https://" + DOMAIN + "@other.example/"], ["http://other.example:8443/mcp"],
                      # scheme without "//" and a host: curl may still find one (SEC F2)
                      ["https:/other.example/mcp"], ["http:other.example"], ["HTTPS:///other.example"],
                      ["https:" + chr(92) * 2 + "other.example"], ["x=https:other.example"]):
            with self.subTest(extra=extra):
                code, out = self.lc("run", "--", sys.executable, "-B", "-c", starter, *extra)
                self.assertEqual(code, 2, out)
                self.assertIn("FAIL run: the command names a host that is not coach-mcp-staging's URL (not shown). "
                              "Nothing was run.", out)
                self.assertNotIn("other.example", out)
                self.assertFalse(os.path.exists(marker))

    def test_own_host_passes(self):
        for arg in (URL + "/mcp", "HTTPS://" + DOMAIN.upper() + ":443/mcp", "no url here"):
            with self.subTest(arg=arg):
                code, out = self.lc(*self.child(arg))
                self.assertEqual(code, 0, out)
                self.assertIn("INFO run: command exit 0", out)

    def test_fill(self):
        reply = os.path.join(self.tmp, "reply-get_activities.json")
        activity = str(10 ** 10 + secrets.randbelow(10 ** 9))
        with open(reply, "w") as f:
            json.dump({"activities": [{"name": "x", "id": int(activity)}, {"id": 5}]}, f)
        code, out = self.lc("run", "--fill", f"activity_id={reply}:id", "--", sys.executable, "-B", "-c", CHILD,
                            '{"activity_id": @activity_id@}')
        self.assertEqual(code, 0, out)
        self.assertNotIn(activity, out)
        self.assertIn("[redacted]", out)
        # a 1-20 digit string works too, depth first
        with open(reply, "w") as f:
            json.dump([{"meta": {"id": "abc"}}, {"id": "0042"}], f)
        code, out = self.lc("run", "--fill", f"a={reply}:id", "--", sys.executable, "-B", "-c", CHILD, "@a@")
        self.assertEqual(code, 0, out)
        self.assertNotIn("0042", out)

    def test_fill_failures(self):
        marker = os.path.join(self.tmp, "started")
        starter = f"open({marker!r}, 'w')"
        reply = os.path.join(self.tmp, "reply.json")
        with open(reply, "w") as f:
            json.dump({"id": "not digits", "other": 1}, f)
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            f.write("not json")
        for spec in (f"a={reply}:id", f"a={reply}:missing", f"a={bad}:id", f"a={self.tmp}/nope.json:id"):
            with self.subTest(spec=spec):
                code, out = self.lc("run", "--fill", spec, "--", sys.executable, "-B", "-c", starter, "@a@")
                self.assertEqual(code, 2, out)
                self.assertIn("(not shown). Nothing was run.", out)
                self.assertFalse(os.path.exists(marker))
        code, out = self.lc("run", "--", sys.executable, "-B", "-c", starter, "@activity_id@")
        self.assertEqual(code, 2, out)
        self.assertIn("FAIL run: @activity_id@ was not filled", out)
        self.assertFalse(os.path.exists(marker))


def logline(msg):
    return json.dumps({"timestamp": "2026-11-17T10:00:00Z", "message": msg})


def caddy(uri, client_ip="198.18.7.7", headers=None, resp=None, remote_ip="10.0.0.5", method="GET"):
    req = {"remote_ip": remote_ip, "remote_port": "1234", "client_ip": client_ip, "proto": "HTTP/1.1",
           "method": method, "host": DOMAIN, "uri": uri}
    if headers is not None:
        req["headers"] = headers
    entry = {"level": "info", "ts": 1.0, "logger": "http.log.access.log0", "msg": "handled request",
             "request": req, "status": 200, "resp_headers": resp or {"Content-Type": ["text/html"]}}
    return logline(json.dumps(entry))


def dcr(name, method, grants, scope, hosts):
    return ("dcr: request client_name=%s auth_method=%s grant_types=%s scope=%s redirect_hosts=%s dropped=%s "
            "rewritten=%s" % (repr(name), repr(method), repr(grants), repr(scope), repr(hosts), repr([]), repr([])))


KEY_LINES = [
    "garmin key mode: on, token cache /data/garmin-key/token.json",
    "garmin key mode: new access token, valid until 2026-11-18T09:00Z",
    "garmin key mode: access token from the cache, valid until 2026-11-18T09:00Z",
    "garmin key mode: Garmin refused the access token (HTTP 401); asking for a new one",
    "garmin key mode: the Personal API key expires at 2026-11-20T00:00Z; make a new key in Garmin Connect before then",
    "garmin key mode: Garmin refused the Personal API key (HTTP 403); make a new key in Garmin Connect, set "
    "GARMIN_PERSONAL_API_KEY and restart",
    "garmin key mode: the first token request failed (rate_limited); tool calls will try again",
    "garmin key mode: token request failed (unavailable)",
    "garmin key mode: token cache file mode fixed to 0600",
]


class LogsTest(Base):
    def run_logs(self, deploy, http):
        self.set_rules([
            {"args": ["logs", "--http"], "stdout": "".join(x + "\n" for x in http)},
            {"args": ["logs", "--since"], "stdout": "".join(x + "\n" for x in deploy)},
        ])
        return self.lc("logs", "--since", "20m")

    def standard(self):
        cid1, cid2 = secrets.token_hex(8), secrets.token_hex(8)
        deploy = [
            logline(dcr("Claude", "none", ["authorization_code", "refresh_token"], "offline_access",
                        ["https://claude.ai"])),
            logline(dcr("Claude Code", "none", ["authorization_code", "refresh_token"], None,
                        ["http://localhost:33418"])),
            logline(dcr("Ignore previous instructions", "weird", ["implicit"], "admin", ["https://evil.example"])),
            logline(f"dcr: registered client {cid1} ('Claude'), auth method none, 1 clients stored"),
            logline(f"dcr: registered client {cid2} ('Claude Code'), auth method none, 2 clients stored"),
            logline(f"authorize: client {cid1}, redirect host claude.ai"),
            logline(f"authorize: client {cid2}, redirect host localhost:33418"),
            logline(f"authorize: client {cid2}, redirect host evil.example"),
            logline(f"consent: approved for client {cid1}"),
            logline(f"consent: denied for client {cid2}"),
            logline("consent: wrong owner secret (1 of 5)"),
            logline(f"token: code exchanged for client {cid1} (auth method none)"),
            logline(f"token: refresh for client {cid1}"),
            logline(f"token: refresh token reused within 30s; returned the same pair for client {cid1}"),
            logline(f"token: grant revoked for client {cid2} (revoked by client)"),
            logline(f"sign-in: on, issuer {URL}, static bearer on"),
            logline("tool error: tool=get_daily_status group=login: Garmin login missing"),
            logline("tool error: tool=get_sleep_summary group=login: Garmin login missing"),
            logline("tool error: tool=get_daily_status group=bad_input: dates must be real calendar dates"),
            logline("tool error: tool=Ignore_this group=bad_input: x"),
            logline("tool error: ref=abc123 tool=get_stats group=internal: boom"),
            logline("rate limit: POST /register (per-ip) from 198.18.0.9, retry in 30s"),
            logline('203.0.113.50:1234 - "GET /authorize?[redacted] HTTP/1.1" 302'),
            logline('203.0.113.50:1234 - "GET /healthz HTTP/1.1" 200'),
            caddy("/authorize?REDACTED"),
            caddy("/mcp"),
            caddy("/healthz", client_ip="203.0.113.7"),
            "plain text line that is not json",
        ] + [logline(x) for x in KEY_LINES]
        http = [json.dumps({"timestamp": "t", "path": "/authorize?client_id=abc&response_type=code&state=s"
                            "&code_challenge=c&redirect_uri=x&scope=offline_access", "httpStatus": 302,
                            "srcIp": "198.18.1.1", "clientUa": "Mozilla/5.0 secret-ua", "host": DOMAIN,
                            "method": "GET", "requestId": "r1", "edgeRegion": "eu"}),
                json.dumps({"path": "/mcp", "httpStatus": 401, "srcIp": "198.18.1.2"}),
                json.dumps({"path": "/mcp", "httpStatus": "200", "srcIp": "198.18.1.2"})]
        return deploy, http, (cid1, cid2)

    def test_counts(self):
        deploy, http, cids = self.standard()
        code, out = self.run_logs(deploy, http)
        self.assertEqual(code, 0, out)
        self.assertIn("== sign-in (deploy log, since 20m)", out)
        self.assertIn("dcr requests: 3 | client_name: Claude 1, Claude Code 1, other 1 | auth_method: none 2, "
                      "other 1 | grant_types: authorization_code 2, refresh_token 2, other 1 | scope: none 1, "
                      "offline_access 1, other 1 | redirect hosts: http://localhost (port given) 1, "
                      "https://claude.ai 1, other 1", out)
        self.assertIn("dcr registered: 2 | authorize: 3 (redirect hosts: https://claude.ai 1, https://claude.com 0, "
                      "http://localhost 1, http://127.0.0.1 0, other 1)", out)
        self.assertIn("consent: approved 1, denied 1, wrong secret 1", out)
        self.assertIn("token: code exchanged 1 (auth method: none 1) | refresh 1 | reused 1 | revoked 1 "
                      "(reason: client 1)", out)
        self.assertIn("sign-in: on, static bearer on | owner secret changed: 0", out)
        self.assertIn("login: get_daily_status 1, get_sleep_summary 1 | bad_input: get_daily_status 1, other 1 | "
                      "rate_limited: 0 | unavailable: 0 | no_data: 0 | internal: get_stats 1", out)
        self.assertIn("lines: 1 | caller address: public 1, documentation 0, private 0, loopback 0", out)
        self.assertIn("entries: 3 | client_ip: public 2, documentation 1, private 0, loopback 0 | /authorize with "
                      "?REDACTED: 1, with any other query: 0 | Location kept: 0 | request headers kept: 0", out)
        self.assertIn("lines: 2 | with a query: 1 | every query shown as ?[redacted]: yes", out)
        self.assertIn("entries: 3 | fields: clientUa, edgeRegion, host, httpStatus, method, path, requestId, srcIp, "
                      "timestamp | status: 2xx 1, 3xx 1, 4xx 1, 5xx 0 | paths with a query: 1 | query parameter "
                      "names: client_id, code_challenge, redirect_uri, response_type, scope, state | sensitive "
                      "names (code, token, secret, password, key): none", out)
        self.assertIn("secret values checked: 2 (OWNER_SECRET, MCP_BEARER_TOKEN) | found in deploy log: 0 | "
                      "found in HTTP log: 0", out)
        self.assertIn("WARN internal tool errors: 1 (see the Railway log; refs only)", out)
        self.assertTrue(out.rstrip().endswith("RESULT: PASS"), out)
        self.assertNoLeak(out, cids)

    def assertNoLeak(self, out, cids=()):
        out = out.replace("http://127.0.0.1 ", "")  # a fixed allowlist label, not an observed address
        self.assertIsNone(IPV4_RE.search(out), out)
        self.assertIsNone(IPV6_RE.search(out), out)
        for bad in ("Ignore", "evil", "Mozilla", "secret-ua", "abc123", "Garmin login missing", "plain text") + cids:
            self.assertNotIn(bad, out)
        self.assertNoSecrets(out)

    def test_key_mode_lines(self):
        code, out = self.run_logs([logline(x) for x in KEY_LINES] + [logline(x.lower()) for x in KEY_LINES], [])
        self.assertIn("on: 2 | new access token: 2 | access token from the cache: 2 | access token refused (401): 2 | "
                      "key refused: 2 (HTTP codes: 403) | first token request failed: 2 (groups: rate_limited) | "
                      "later token request failed: 2 (groups: unavailable) | key expiry warning: yes | cache file "
                      "mode fixed: 2", out)
        self.assertNotIn("/data/garmin-key", out)
        self.assertNotIn("2026-11-18", out)

    def test_key_mode_level_prefix_and_no_key_value(self):
        planted = secrets.token_hex(20)
        lines = ["WARNING  garmin key mode: Garmin refused the Personal API key (HTTP 401); key " + planted,
                 "INFO:garmin_ext.key_mode:garmin key mode: on, token cache " + planted]
        code, out = self.run_logs([logline(x) for x in lines], [])
        self.assertIn("on: 1 |", out)
        self.assertIn("key refused: 1 (HTTP codes: 401)", out)
        self.assertNotIn(planted, out)

    def test_secret_in_http_path(self):
        deploy, http, _ = self.standard()
        http.append(json.dumps({"path": "/mcp/" + self.token, "httpStatus": 410}))
        code, out = self.run_logs(deploy, http)
        self.assertEqual(code, 1, out)
        self.assertIn("found in HTTP log: 1 (MCP_BEARER_TOKEN)", out)
        self.assertIn("RESULT: FAIL", out)
        self.assertNoSecrets(out)

    def test_encoded_secret_in_deploy_log(self):
        deploy = [logline("something " + urllib.parse.quote(self.owner + "/x", safe=""))]
        self.variables["OWNER_SECRET"] = self.owner + "/x"
        code, out = self.run_logs(deploy, [])
        self.assertEqual(code, 1, out)
        self.assertIn("found in deploy log: 1 (OWNER_SECRET)", out)

    def test_loopback_rate_limit_fails(self):
        code, out = self.run_logs([logline("rate limit: POST /consent (per-ip) from 127.0.0.1, retry in 5s")], [])
        self.assertEqual(code, 1, out)
        self.assertIn("loopback 1", out)
        self.assertIsNone(IPV4_RE.search(out.replace("http://127.0.0.1 ", "")))

    def test_private_caddy_ip_fails(self):
        code, out = self.run_logs([caddy("/mcp", client_ip="100.64.3.4")], [])
        self.assertEqual(code, 1, out)
        self.assertIn("private 1", out)

    def test_location_and_query_and_headers_fail(self):
        for line, why in ((caddy("/consent", resp={"Location": ["https://claude.ai/cb?code=x"]}),
                           "Caddy kept a Location header"),
                          (caddy("/authorize?state=abc"), "a query string is not redacted"),
                          (caddy("/mcp", headers={"Authorization": ["Bearer x"]}), "Caddy kept request headers"),
                          (logline('1.2.3.4:5 - "GET /authorize?state=abc HTTP/1.1" 302'),
                           "a query string is not redacted")):
            with self.subTest(why=why):
                code, out = self.run_logs([line], [])
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL " + why, out)
                self.assertNotIn("state=abc", out)

    def test_sensitive_query_name(self):
        http = [json.dumps({"path": "/cb?code=abc&access_token=x&state=1", "httpStatus": 200})]
        code, out = self.run_logs([], http)
        self.assertEqual(code, 1, out)
        self.assertIn("sensitive names (code, token, secret, password, key): 2", out)
        self.assertIn("query parameter names: access_token, code, state", out)

    def test_railway_health_checks_are_set_aside(self):
        # Live test R1 (17 Nov 2026): Railway's deploy health check reaches Caddy
        # from 100.64.0.0/10 with no edge header (client_ip = remote_ip).
        deploy = [caddy("/healthz", client_ip="100.64.0.3", remote_ip="100.64.0.3"),
                  caddy("/healthz", client_ip="100.64.0.9", remote_ip="100.64.0.9"),
                  caddy("/mcp")]
        code, out = self.run_logs(deploy, [])
        self.assertEqual(code, 0, out)
        self.assertIn("entries: 3 | client_ip: public 1, documentation 0, private 0, loopback 0 |", out)
        self.assertIn("| Railway health checks: 2 (GET /healthz from its own network; not in client_ip)", out)
        self.assertNotIn("FAIL a Caddy client_ip is loopback or private", out)

    def test_private_client_ip_still_fails_outside_the_health_check(self):
        cases = {
            "another path": caddy("/mcp", client_ip="100.64.0.3", remote_ip="100.64.0.3"),
            "a query on /healthz": caddy("/healthz?x=1", client_ip="100.64.0.3", remote_ip="100.64.0.3"),
            "not GET": caddy("/healthz", client_ip="100.64.0.3", remote_ip="100.64.0.3", method="POST"),
            "client_ip from a header": caddy("/healthz", client_ip="10.1.2.3", remote_ip="100.64.0.3"),
            "loopback on another path": caddy("/readyz", client_ip="127.0.0.1", remote_ip="127.0.0.1"),
        }
        for why, line in cases.items():
            with self.subTest(why):
                code, out = self.run_logs([line], [])
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL a Caddy client_ip is loopback or private", out)
                self.assertIn("| Railway health checks: 0 (", out)

    def test_attribute_shape_caddy(self):
        entry = {"timestamp": "t", "message": "handled request", "logger": "http.log.access.log0",
                 "request": {"client_ip": "203.0.113.4", "uri": "/mcp"}}
        code, out = self.run_logs([json.dumps(entry)], [])
        self.assertIn("entries: 1 | client_ip: public 0, documentation 1", out)

    def test_save(self):
        deploy, http, _ = self.standard()
        save = os.path.join(self.tmp, "logs.txt")
        self.set_rules([{"args": ["logs", "--http"], "stdout": "\n".join(http)},
                        {"args": ["logs", "--since"], "stdout": "\n".join(deploy)}])
        code, out = self.lc("logs", "--save", save)
        with open(save) as f:
            saved = f.read()
        self.assertEqual(os.stat(save).st_mode & 0o777, 0o600)
        self.assertIn("RESULT: PASS", saved)
        self.assertIn("== sign-in (deploy log, since 60m)", saved)
        self.assertNotIn("Mozilla", saved)

    def test_logs_cli_failure(self):
        self.set_rules([{"args": ["logs"], "stdout": "", "exit": 1}])
        code, out = self.lc("logs")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL logs: railway logs did not run", out)


class ProbeIpTest(Base):
    def run_probe(self, client_ip, delay_paths=False):
        paths = os.path.join(self.tmp, "paths.txt")
        stub = livefakes.StubServer(paths_file=None if delay_paths else paths)
        self.addCleanup(stub.close)
        self.variables["PUBLIC_URL"] = f"http://127.0.0.1:{stub.port}"
        self.set_rules([{"args": ["logs", "--since", "5m", "--json"],
                         "probe": {"paths_file": paths, "client_ip": client_ip}}])
        code, out = self.lc("probe-ip")
        return code, out, stub

    def test_public_passes(self):
        code, out, stub = self.run_probe("198.18.20.30")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(stub.requests), 3)
        path, headers = stub.requests[0]
        headers = {k.lower(): v for k, v in headers.items()}
        self.assertRegex(path, r"^/probe/[0-9a-f]{16}$")
        self.assertEqual(headers.get("x-real-ip"), "203.0.113.9")
        self.assertEqual(headers.get("x-forwarded-for"), "203.0.113.10")
        self.assertEqual(headers.get("cf-connecting-ip"), "203.0.113.11")
        self.assertIn("INFO probe-ip: 3 requests to a random /probe/ path with planted X-Real-IP, X-Forwarded-For "
                      "and CF-Connecting-IP", out)
        self.assertIn("PASS T15: 3 of 3 requests found in Caddy's access log; client_ip is a planted address: no; "
                      "client_ip class: public", out)
        self.assertNotIn("198.18.20.30", out)

    def test_planted_headers_fail(self):
        for addr, text in (("203.0.113.9", "client_ip is the planted X-Real-IP: yes"),
                           ("203.0.113.10", "client_ip is the planted X-Forwarded-For: yes"),
                           ("203.0.113.11", "client_ip is the planted CF-Connecting-IP: yes")):
            with self.subTest(addr=addr):
                code, out, _ = self.run_probe(addr)
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL T15: 3 of 3 requests found in Caddy's access log; " + text, out)

    def test_private_fails(self):
        code, out, _ = self.run_probe("10.4.5.6")
        self.assertEqual(code, 1, out)
        self.assertIn("client_ip class: private (Railway's internal hop", out)
        self.assertNotIn("10.4.5.6", out)

    def test_not_found(self):
        code, out, _ = self.run_probe("198.18.20.30", delay_paths=True)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL T15: the probe requests were not in the log within 1 s; run probe-ip again", out)


class DataShapeTest(Base):
    def reply(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_table_and_delete(self):
        ell = chr(0x2026)
        zw = chr(0x200B)
        data = {"activities": [
            {"id": 1, "activityName": "Morning run in the park", "description": "x" * 99 + ell},
            {"id": 2, "activityName": "Short", "description": "see [link] and [image] " + zw + " end" + "y" * 10},
            {"id": 3, "activityName": "a" * 100, "description": "z" * 499 + ell, "notes": "short"}]}
        path = self.reply("reply-get_activities.json", data)
        code, out = self.lc("data-shape", path)
        self.assertEqual(code, 0, out)
        self.assertIn("== reply-get_activities.json: isError no, ", out)
        self.assertIn("JSON yes", out)
        rows = {line.split()[0]: line.split()[1:] for line in out.splitlines() if line[:1].isalpha()
                and not line.startswith(("key", "RESULT"))}
        self.assertEqual(rows["activityName"], ["3", "100", "1", "0", "0", "0", "0", "0"])
        self.assertEqual(rows["description"], ["3", "500", "1", "1", "1", "1", "0", "1"])
        self.assertNotIn("notes", rows)
        self.assertIn("RESULT: INFO (numbers only)", out)
        self.assertNotIn("Morning", out)
        self.assertFalse(os.path.exists(path))

    def test_keep_and_non_json(self):
        path = self.reply("reply-get_stats.json", "Error: Garmin login missing or expired. Log in again.\nmore")
        code, out = self.lc("data-shape", path, "--keep")
        self.assertEqual(code, 0, out)
        self.assertIn("== reply-get_stats.json: isError yes, ", out)
        self.assertIn("JSON no", out)
        self.assertNotIn("Garmin login", out)
        self.assertTrue(os.path.exists(path))

    def test_keys_out_compare_require(self):
        a = self.reply("reply-get_daily_status.json", {"date": "2026-11-16", "sources": {"key_mode_note": "x",
                                                                                         "sleep": [{"score": 80}]}})
        b = self.reply("reply-get_weekly_status.json", {"days": [{"date": "d"}]})
        shapes = os.path.join(self.tmp, "shapes.json")
        code, out = self.lc("data-shape", a, b, "--keys-out", shapes, "--keep",
                            "--require-key", "get_daily_status:key_mode_note")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS require-key get_daily_status:key_mode_note", out)
        self.assertEqual(os.stat(shapes).st_mode & 0o777, 0o600)
        with open(shapes) as f:
            saved = json.load(f)
        self.assertEqual(saved["get_daily_status"], ["date", "sources", "sources.key_mode_note", "sources.sleep",
                                                     "sources.sleep[].score"])
        self.assertEqual(saved["get_weekly_status"], ["days", "days[].date"])
        self.assertNotIn("2026-11-16", json.dumps(saved))
        code, out = self.lc("data-shape", b, "--require-key", "get_weekly_status:key_mode_note", "--keep")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL require-key get_weekly_status:key_mode_note", out)
        code, out = self.lc("data-shape", b, "--require-key", "get_stats:key_mode_note", "--keep")
        self.assertEqual(code, 2, out)
        # compare: full mode has more keys
        c = self.reply("reply-get_daily_status.json", {"date": "x", "sources": {"sleep": [{"score": 1, "hrv": 2}]},
                                                      "x": [{"y" + str(i): 1 for i in range(30)}]})
        d = self.reply("reply-get_stats.json", {"a": 1})
        code, out = self.lc("data-shape", c, d, "--compare", shapes)
        self.assertEqual(code, 0, out)
        line = next(x for x in out.splitlines() if x.startswith("compare get_daily_status:"))
        self.assertIn("only here 32 (", line)
        self.assertIn("| only in shapes.json 1 (sources.key_mode_note)", line)
        self.assertLessEqual(line.count("x[].y"), 20)
        self.assertIn("compare get_stats: no earlier shape", out)
        self.assertFalse(os.path.exists(c))


class DeploysTest(Base):
    def test_count_line(self):
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        t1 = (now - __import__("datetime").timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        t0 = (now - __import__("datetime").timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = [{"id": "d1", "status": "SUCCESS", "createdAt": t0, "meta": {"secret": "zzz"}},
                {"id": "d2", "status": "SUCCESS", "createdAt": t1}, {"id": "d3", "status": "weird"}]
        self.set_rules([{"args": ["deployment", "list"], "stdout": json.dumps(data)}])
        code, out = self.lc("deploys")
        self.assertEqual(code, 0, out)
        self.assertIn("deployments: 3 (SUCCESS 2, FAILED 0, other 1) | newest: SUCCESS, 3 min ago", out)
        self.assertNotIn("zzz", out)
        self.assertIn(["deployment", "list", "--json", "--limit", "10"], self.railway.calls())

    def test_unknown_shape(self):
        self.set_rules([{"args": ["deployment", "list"], "stdout": json.dumps({"x": "secretvalue"})}])
        code, out = self.lc("deploys")
        self.assertIn("deployments: other", out)
        self.assertNotIn("secretvalue", out)

    def test_wait(self):
        for final, want in (("SUCCESS", 0), ("FAILED", 1), ("CRASHED", 1), ("REMOVED", 1)):
            with self.subTest(final=final):
                seq = [{"stdout": json.dumps({"deployments": [{"status": "BUILDING"}]})},
                       {"stdout": json.dumps({"deployments": [{"status": final}]})}]
                self.set_rules([{"args": ["deployment", "list"], "seq": seq}])
                for f in os.listdir(self.tmp):
                    if ".count" in f:
                        os.remove(os.path.join(self.tmp, f))
                code, out = self.lc("deploys", "--wait")
                self.assertEqual(code, want, out)
        self.set_rules([{"args": ["deployment", "list"], "stdout": json.dumps([{"status": "BUILDING"}])}])
        code, out = self.lc("deploys", "--wait")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL deploys: no SUCCESS within", out)


class HealthcheckTest(Base):
    def check(self, data):
        self.set_rules([{"args": ["environment", "config", "--json"], "stdout": json.dumps(data)}])
        return self.lc("healthcheck")

    def test_pass(self):
        planted = secrets.token_hex(12)
        code, out = self.check({"services": {"s1": {"deploy": {"healthcheckPath": "/healthz"},
                                                    "variables": {"OWNER_SECRET": {"value": planted}}}}})
        self.assertEqual(code, 0, out)
        self.assertIn("PASS healthcheck: deploy.healthcheckPath is /healthz", out)
        self.assertNotIn(planted, out)

    def test_fail(self):
        planted = secrets.token_hex(12)
        for data, text in (({"deploy": {}}, "0 healthcheckPath keys, values: none"),
                           ({"a": {"healthcheckPath": "/healthz"}, "b": {"healthcheckPath": "/healthz"}},
                            "2 healthcheckPath keys, values: /healthz, /healthz"),
                           ({"deploy": {"healthcheckPath": "/readyz"}}, "1 healthcheckPath keys, values: /readyz"),
                           ({"deploy": {"healthcheckPath": "/" + planted}}, "1 healthcheckPath keys, values: other")):
            with self.subTest(text=text):
                code, out = self.check(data)
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL healthcheck: " + text, out)
                self.assertNotIn(planted, out)


class MetricsTest(Base):
    def check(self, data):
        self.set_rules([{"args": ["metrics"], "stdout": json.dumps(data)}])
        return self.lc("metrics")

    MONTHLY = ("monthly at this use (from about 3 hours of use, not a monthly bill): memory $1.80 + CPU $0.20 + "
               "volume $0.00 = $2.00; Railway Hobby is $5 a month and includes $5 of usage")

    def test_series_lists(self):
        data = {"memoryUsageGb": [{"ts": 1, "value": 0.12}, {"ts": 2, "value": 0.24}, {"ts": 3, "value": 0.18}],
                "cpuUsage": [{"ts": 1, "value": 0.0}, {"ts": 2, "value": 0.2}, {"ts": 3, "value": -0.17}],
                "volumeUsageGb": [{"ts": 1, "value": 0.002}, {"ts": 2, "value": 0.003}]}
        code, out = self.check(data)
        self.assertEqual(code, 0, out)
        self.assertIn("metrics (since 3h): memory avg 180 MB, max 240 MB | CPU avg 0.01 vCPU, max 0.20 | "
                      "volume 3 MB", out)
        self.assertIn(self.MONTHLY, out)

    def test_averages(self):
        data = {"measurements": [{"measurement": "MEMORY_USAGE_GB", "avg": 0.18, "max": 0.24},
                                 {"measurement": "CPU_USAGE", "avg": 0.01, "max": 0.2},
                                 {"measurement": "DISK_USAGE_GB", "avg": 0.003, "max": 0.003}]}
        code, out = self.check(data)
        self.assertEqual(code, 0, out)
        self.assertIn("memory avg 180 MB, max 240 MB | CPU avg 0.01 vCPU, max 0.20 | volume 3 MB", out)
        self.assertIn(self.MONTHLY, out)

    def test_empty(self):
        code, out = self.check({"memoryUsageGb": [], "cpuUsage": [], "volumeUsageGb": []})
        self.assertEqual(code, 1, out)
        self.assertIn("metrics: memory not readable from the CLI; read it in the dashboard (service > Metrics)", out)

    def test_unknown_shape(self):
        code, out = self.check({"foo": "secretvalue"})
        self.assertEqual(code, 0, out)
        self.assertIn("INFO metrics: metrics shape not recognised", out)
        self.assertNotIn("secretvalue", out)


FIND_CLEAN = "/app/ext/launch.py\n/usr/bin/python3\n/data/tokens/garmin_tokens.json\n"
FIND_HIT = FIND_CLEAN + "/root/.garminconnect/garmin_tokens.json\n/tmp/garmin_token.json\n"


class SshTest(Base):
    def ssh_rules(self, stat_out="", find_seq=None, ls_out=""):
        rules = [{"args": ["ssh", "--", "stat"], "stdout": stat_out, "exit": 0 if stat_out.count("\n") >= 2 else 1},
                 {"args": ["ssh", "--", "rm"], "stdout": ""},
                 {"args": ["ssh", "--", "ls"], "stdout": ls_out}]
        if find_seq is not None:
            rules.append({"args": ["ssh", "--", "find"], "seq": [{"stdout": x} for x in find_seq]})
        self.set_rules(rules)

    def ssh_calls(self):
        return [c for c in self.railway.calls() if c[:1] == ["ssh"]]

    def test_cache_modes(self):
        self.ssh_rules("700:/data/garmin-key\n600:/data/garmin-key/token.json\n")
        code, out = self.lc("ssh", "cache-modes")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS ssh cache-modes: /data/garmin-key 700, /data/garmin-key/token.json 600", out)
        self.assertEqual(self.ssh_calls(), [["ssh", "--", "stat", "-c", "%a:%n", "/data/garmin-key",
                                             "/data/garmin-key/token.json"]])
        calls = self.railway.calls()
        self.assertEqual(calls[0], ["status", "--json"])
        for stat_out in ("755:/data/garmin-key\n600:/data/garmin-key/token.json\n",
                         "700:/data/garmin-key\n644:/data/garmin-key/token.json\n", "700:/data/garmin-key\n"):
            with self.subTest(stat_out=stat_out):
                self.ssh_rules(stat_out)
                code, out = self.lc("ssh", "cache-modes")
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL ssh cache-modes", out)

    def test_token_copies(self):
        self.ssh_rules(find_seq=[FIND_CLEAN])
        code, out = self.lc("ssh", "token-copies")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS ssh token-copies: no Garmin token file outside /data (3 files checked)", out)
        self.assertEqual(self.ssh_calls(), [["ssh", "--", "find", "/", "-xdev", "-type", "f"]])
        self.ssh_rules(find_seq=[FIND_HIT])
        code, out = self.lc("ssh", "token-copies")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL ssh token-copies: 2 Garmin token files outside /data (5 files checked), in: "
                      "/root/.garminconnect, /tmp", out)

    def test_ssh_not_run(self):
        self.set_rules([{"args": ["ssh"], "stdout": "", "stderr": "no ssh key", "exit": 1}])
        code, out = self.lc("ssh", "token-copies")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL ssh: railway ssh did not run (", out)
        self.assertIn("https://docs.railway.com/cli/ssh", out)
        self.assertNotIn("no ssh key", out)

    def test_wipe_needs_yes(self):
        self.ssh_rules(find_seq=[FIND_HIT])
        code, out = self.lc("ssh", "wipe-garmin")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.railway.calls(), [])

    def test_wipe(self):
        self.ssh_rules(find_seq=[FIND_HIT, FIND_CLEAN.replace("/data/tokens/garmin_tokens.json\n", "")],
                       ls_out="auth.sqlite\n")
        code, out = self.lc("ssh", "wipe-garmin", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS ssh wipe-garmin: /data/tokens and /data/garmin-key are gone and no token file is left "
                      "outside /data", out)
        rm = [c for c in self.ssh_calls() if c[2] == "rm"]
        self.assertEqual(rm, [["ssh", "--", "rm", "-rf", "/data/tokens", "/data/garmin-key",
                               "/root/.garminconnect/garmin_tokens.json", "/tmp/garmin_token.json"]])
        # every ssh call comes after a guard
        calls = self.railway.calls()
        first_ssh = next(i for i, c in enumerate(calls) if c[0] == "ssh")
        self.assertIn(["status", "--json"], calls[:first_ssh])

    def test_wipe_left_over(self):
        self.ssh_rules(find_seq=[FIND_CLEAN, FIND_CLEAN], ls_out="garmin-key\nauth.sqlite\n")
        code, out = self.lc("ssh", "wipe-garmin", "--yes")
        self.assertEqual(code, 1, out)
        self.ssh_rules(find_seq=[FIND_HIT, FIND_HIT], ls_out="")
        code, out = self.lc("ssh", "wipe-garmin", "--yes")
        self.assertEqual(code, 1, out)
        self.assertIn("2 token files outside /data", out)

    def test_wipe_refuses_odd_paths(self):
        for odd in ("/tmp/a b/garmin_tokens.json", "/tmp/x;rm/garmin_tokens.json"):
            with self.subTest(odd=odd):
                self.ssh_rules(find_seq=[FIND_CLEAN + odd + "\n"])
                code, out = self.lc("ssh", "wipe-garmin", "--yes")
                self.assertEqual(code, 2, out)
                self.assertFalse([c for c in self.ssh_calls() if c[2] == "rm"])
                self.assertNotIn("x;rm", out)


class StaticTest(unittest.TestCase):
    def test_docstring_and_python310(self):
        with open(livefakes.LIVE_CHECK, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith('#!/usr/bin/env python3\n"""Staging helper for maintainers: checks a '
                                        'throwaway Railway deployment of\ncoach-mcp-for-garmin without showing any '
                                        'secret.\n'))
        self.assertIn("Standard library only, so any python3 3.10 or newer runs it.", text)
        ast.parse(text, feature_version=(3, 10))
        for ch in text:
            self.assertFalse(0x202A <= ord(ch) <= 0x202E or 0x2066 <= ord(ch) <= 0x2069 or ord(ch) == 0x200B)

    def test_help(self):
        code, out = livefakes.run_script(livefakes.LIVE_CHECK, ["--help"])
        self.assertEqual(code, 0, out)
        for sub in ("guard", "url", "run", "logs", "deploys", "healthcheck", "metrics", "ssh", "probe-ip",
                    "data-shape"):
            self.assertIn(sub, out)


if __name__ == "__main__":
    unittest.main()
