"""Tests for scripts/ci-smoke-probe.py against a loopback stub of the server (port 0, no Docker, no network).

The stub answers /healthz, /readyz and /mcp (401 without the token, then initialize, tools/list and
tools/call) with the tool list each test sets, and the sign-in (/register, /authorize, /consent, /token) for
--oauth. The Drift tests keep the copies in scripts/ci-smoke.sh and the probe equal to ext/ (launch.py's tool
sets, the consent box, the scope, the refusal reasons). Standard library only.
"""

import ast
import importlib.util
import base64
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(os.path.dirname(HERE), "ci-smoke-probe.py")
TOKEN = "probe-test-" + os.urandom(8).hex()
OWNER = "owner-test-" + os.urandom(8).hex()
OAUTH = "oauth-test-" + os.urandom(8).hex()
ERROR_TEXT = "Error: Garmin Connect did not answer. Try again in a few minutes."
ROOT = os.path.dirname(os.path.dirname(HERE))
EXT = os.path.join(ROOT, "ext")
SMOKE = os.path.join(os.path.dirname(HERE), "ci-smoke.sh")


def load():
    spec = importlib.util.spec_from_file_location("ci_smoke_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_tool(name):
    return {"name": name, "annotations": {"readOnlyHint": True, "destructiveHint": False,
                                          "idempotentHint": True, "openWorldHint": True}}


def write_tool(name, destructive, read_only=False, idempotent=True, open_world=True):
    return {"name": name, "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive,
                                          "idempotentHint": idempotent, "openWorldHint": open_world}}


class Stub(BaseHTTPRequestHandler):
    tools = []
    called = []
    instructions = "the instructions"
    offer_write = True
    state = {}

    def log_message(self, *args):
        pass

    def reply(self, code, body, headers=None, raw=None):
        data = raw.encode() if raw is not None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html" if raw is not None else "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/authorize":
            type(self).state = {"state": query.get("state"), "challenge": query.get("code_challenge")}
            self.reply(302, {}, {"Location": "http://stub/consent?req=r1"})
        elif url.path == "/consent":
            box = ('<label><input type="checkbox" name="plan_write" value="yes">'
                   "Also allow this app to change my Garmin calendar</label>") if type(self).offer_write else ""
            self.reply(200, None, raw=f"<form>{box}</form>")
        else:
            self.reply(200, {"ok": True})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = urlsplit(self.path).path
        if path == "/register":
            self.reply(201, {"client_id": "c1", "client_secret": "s1"})
            return
        if path == "/consent":
            fields = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            if fields.get("secret") != OWNER or self.headers.get("Sec-Fetch-Site") != "same-origin":
                self.reply(403, {})
                return
            type(self).state["write"] = fields.get("plan_write") == "yes" and type(self).offer_write
            self.reply(303, {}, {"Location": "https://claude.ai/api/mcp/auth_callback?code=k1&state="
                                             + type(self).state["state"]})
            return
        if path == "/token":
            fields = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            digest = hashlib.sha256(fields.get("code_verifier", "").encode()).digest()
            ok = (fields.get("code") == "k1" and fields.get("client_secret") == "s1"
                  and base64.urlsafe_b64encode(digest).decode().rstrip("=") == type(self).state["challenge"])
            scope = "offline_access plan:write" if type(self).state.get("write") else "offline_access"
            self.reply(200 if ok else 400, {"access_token": OAUTH, "scope": scope} if ok else {})
            return
        body = json.loads(raw or b"{}")
        auth = self.headers.get("Authorization")
        if auth not in (f"Bearer {TOKEN}", f"Bearer {OAUTH}"):
            self.reply(401, {}, {"WWW-Authenticate": 'Bearer resource_metadata="http://x/.well-known"'})
            return
        method, rid = body.get("method"), body.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}, "instructions": type(self).instructions,
                      "serverInfo": {"name": "coach-mcp-for-garmin", "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": type(self).tools}
        elif method == "tools/call":
            name = body["params"]["name"]
            type(self).called.append(name)
            if name == "plan_commit":
                oauth_write = auth == f"Bearer {OAUTH}" and type(self).state.get("write")
                reason = "unknown_token" if oauth_write else "no_write_permission"
                result = {"content": [{"type": "text", "text": json.dumps({"status": "refused", "reason": reason})}]}
            else:
                result = {"isError": True, "content": [{"type": "text", "text": ERROR_TEXT}]}
        else:
            self.reply(202, {})
            return
        self.reply(200, {"jsonrpc": "2.0", "id": rid, "result": result})


class Probe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = load()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def run_probe(self, tools, names, *extra):
        Stub.tools, Stub.called, Stub.state = tools, [], {}
        self.probe.failed.clear()
        argv = ["--base", self.base, "--expect-ready", "200", "--expect-names", names, "--expect-version", "1.0.0",
                "--call", "get_stats", "--expect-error", "Garmin Connect did not answer", *extra]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MCP_BEARER_TOKEN": TOKEN, "OWNER_SECRET": OWNER}), redirect_stdout(out):
            code = self.probe.main(argv)
        lines = {line.split(":", 1)[0].split("probe/")[-1]: line for line in out.getvalue().splitlines()
                 if "probe/" in line}
        for secret in (TOKEN, OWNER, OAUTH):
            self.assertNotIn(secret, out.getvalue())
        return code, lines

    def test_default_run_needs_every_tool_read_only(self):
        code, lines = self.run_probe([read_tool("get_stats"), read_tool("get_activities")],
                                     "get_stats,get_activities")
        self.assertEqual(code, 0, lines)
        self.assertIn("2 tools (want 2); all readOnlyHint: yes", lines["tools"])
        self.assertNotIn("write-tools", lines)
        code, lines = self.run_probe([read_tool("get_stats"), write_tool("plan_commit", False)],
                                     "get_stats,plan_commit")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  probe/tools", lines["tools"])

    def test_write_tools_pass_with_their_hints_and_are_never_called(self):
        tools = [read_tool("get_stats"), write_tool("plan_commit", False), write_tool("plan_cancel", True)]
        code, lines = self.run_probe(tools, "get_stats,plan_commit,plan_cancel",
                                     "--write-tools", "plan_commit,plan_cancel")
        self.assertEqual(code, 0, lines)
        self.assertIn("all but the write tools readOnlyHint: yes", lines["tools"])
        self.assertIn("PASS  probe/write-tools: 2 write tool(s): plan_cancel, plan_commit; listed, "
                      "readOnlyHint=false, hints of their kind", lines["write-tools"])
        self.assertEqual(Stub.called, ["get_stats"])

    def test_write_tools_fail_on_wrong_hints_or_a_missing_tool(self):
        tools = [read_tool("get_stats"), write_tool("plan_commit", True), write_tool("plan_cancel", True,
                                                                                     idempotent=False)]
        code, lines = self.run_probe(tools, "get_stats,plan_commit,plan_cancel",
                                     "--write-tools", "plan_commit,plan_cancel")
        self.assertEqual(code, 1)
        self.assertIn("plan_commit: destructiveHint True (want false)", lines["write-tools"])
        self.assertIn("plan_cancel: idempotentHint False (want true)", lines["write-tools"])
        code, lines = self.run_probe([read_tool("get_stats")], "get_stats", "--write-tools", "plan_commit")
        self.assertEqual(code, 1)
        self.assertIn("not listed: plan_commit", lines["write-tools"])

    def test_a_read_tool_without_read_only_fails_even_with_write_tools(self):
        loose = dict(read_tool("get_activities"), annotations={"readOnlyHint": False})
        code, lines = self.run_probe([read_tool("get_stats"), loose, write_tool("plan_commit", False)],
                                     "get_stats,get_activities,plan_commit", "--write-tools", "plan_commit")
        self.assertEqual(code, 1)
        self.assertIn("all but the write tools readOnlyHint: no", lines["tools"])

    def test_log_feedback_must_be_local_only(self):
        tools = [read_tool("get_stats"), write_tool("log_feedback", False, open_world=False)]
        code, lines = self.run_probe(tools, "get_stats,log_feedback", "--write-tools", "log_feedback")
        self.assertEqual(code, 0, lines)
        tools = [read_tool("get_stats"), write_tool("log_feedback", False)]
        code, lines = self.run_probe(tools, "get_stats,log_feedback", "--write-tools", "log_feedback")
        self.assertEqual(code, 1)
        self.assertIn("log_feedback: openWorldHint True (want false)", lines["write-tools"])

    def test_instructions_must_match_the_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(Stub.instructions)
        self.addCleanup(os.unlink, f.name)
        code, lines = self.run_probe([read_tool("get_stats")], "get_stats", "--instructions", f.name)
        self.assertEqual(code, 0, lines)
        self.assertIn("PASS  probe/instructions", lines["instructions"])
        with mock.patch.object(Stub, "instructions", "No tool changes any data."):
            code, lines = self.run_probe([read_tool("get_stats")], "get_stats", "--instructions", f.name)
        self.assertEqual(code, 1)
        self.assertIn("FAIL  probe/instructions", lines["instructions"])

    def all_on(self, *extra):
        tools = [read_tool("get_stats"), write_tool("plan_commit", False), write_tool("plan_cancel", True)]
        return self.run_probe(tools, "get_stats,plan_commit,plan_cancel", "--write-tools", "plan_commit,plan_cancel",
                              "--refused-call", "plan_commit", "--refused-args", '{"plan_id": "x"}',
                              "--expect-refusal", "no_write_permission", *extra)

    def test_static_bearer_write_refused_and_oauth_with_the_box(self):
        code, lines = self.all_on("--oauth", "yes", "--oauth-refusal", "unknown_token")
        self.assertEqual(code, 0, lines)
        for name in ("static-bearer-write", "oauth/consent-box", "oauth/grant", "oauth/tools", "oauth/write-tools",
                     "oauth/same-hints", "oauth-write"):
            self.assertTrue(lines[name].startswith("PASS"), lines[name])
        self.assertIn("plan:write: yes (want yes)", lines["oauth/grant"])
        self.assertEqual(Stub.called, ["get_stats", "plan_commit", "plan_commit"])

    def test_an_oauth_grant_that_cannot_write_fails(self):
        code, lines = self.all_on("--oauth", "no", "--oauth-refusal", "unknown_token")
        self.assertEqual(code, 1)
        self.assertTrue(lines["oauth/consent-box"].startswith("FAIL"), lines["oauth/consent-box"])
        self.assertTrue(lines["oauth-write"].startswith("FAIL"), lines["oauth-write"])

    def test_oauth_no_box_means_no_write_scope(self):
        with mock.patch.object(Stub, "offer_write", False):
            code, lines = self.run_probe([read_tool("get_stats")], "get_stats", "--oauth", "no")
            self.assertEqual(code, 0, lines)
            self.assertIn("plan:write: no (want no)", lines["oauth/grant"])
            code, lines = self.run_probe([read_tool("get_stats")], "get_stats", "--oauth", "yes")
        self.assertEqual(code, 1)
        self.assertTrue(lines["oauth/consent-box"].startswith("FAIL"))
        self.assertTrue(lines["oauth/grant"].startswith("FAIL"))

    def test_usage_errors(self):
        for extra in (("--write-tools", "delete_workout"), ("--write-tools", "get_stats")):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                self.probe.main(["--expect-ready", "200", "--expect-names", "a", "--call", "get_stats",
                                 "--expect-error", "x", *extra])
            self.assertEqual(cm.exception.code, 2)
        base = ["--expect-ready", "200", "--expect-names", "a", "--call", "get_stats", "--expect-error", "x"]
        for extra in (["--call", "plan_commit", "--write-tools", "plan_commit"],
                      ["--refused-call", "plan_commit", "--expect-refusal", "x"],
                      ["--write-tools", "plan_commit", "--refused-call", "plan_commit"],
                      ["--oauth-refusal", "x"]):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                self.probe.main(base + extra)
            self.assertEqual(cm.exception.code, 2)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _frozensets(path):
    """NAME -> set for every top-level `NAME = frozenset({...})` of literal strings in a module."""
    out = {}
    for node in ast.parse(_read(path)).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and getattr(
                node.value.func, "id", None) == "frozenset" and node.value.args:
            try:
                out[node.targets[0].id] = set(ast.literal_eval(node.value.args[0]))
            except ValueError:
                pass
    return out


def _strings(path):
    """NAME -> value for every top-level `NAME = "literal"` of a module."""
    return {node.targets[0].id: node.value.value for node in ast.parse(_read(path)).body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)}


class Drift(unittest.TestCase):
    """The smoke test's copies of ext/'s names stay equal to ext/."""

    @classmethod
    def setUpClass(cls):
        cls.launch = _frozensets(os.path.join(EXT, "launch.py"))
        cls.smoke = dict(re.findall(r'^(PLAN_[A-Z_]+_TOOLS)="([^"]*)"$', _read(SMOKE), re.M))
        cls.probe = load()

    def test_smoke_tool_sets_are_launch_py_sets(self):
        garmin_writes = self.launch["PLAN_WRITE_TOOLS"] | self.launch["PLAN_APPLY_TOOLS"]
        for var, want in (("PLAN_HISTORY_TOOLS", self.launch["HISTORY_TOOLS"]),
                          ("PLAN_READ_TOOLS", self.launch["PLAN_READ_TOOLS"]),
                          ("PLAN_REVIEW_TOOLS", self.launch["PLAN_REVIEW_TOOLS"]),
                          ("PLAN_WRITE_TOOLS", self.launch["OWN_WRITE_TOOLS"]),
                          ("PLAN_GARMIN_WRITE_TOOLS", garmin_writes)):
            with self.subTest(var=var):
                self.assertEqual(set(self.smoke[var].split()), want)

    def test_probe_write_tools_are_the_own_write_tools(self):
        own = self.launch["OWN_WRITE_TOOLS"]
        self.assertEqual(set(self.probe.WRITE_TOOL_DESTRUCTIVE), own)
        self.assertEqual(set(self.probe.WRITE_TOOL_OPEN_WORLD), own)
        local = {n for n, world in self.probe.WRITE_TOOL_OPEN_WORLD.items() if not world}
        self.assertEqual(local, self.launch["PLAN_TRACKING_TOOLS"])

    def test_probe_sign_in_names_are_ext_names(self):
        self.assertIn(self.probe.WRITE_BOX_TEXT, _read(os.path.join(EXT, "owner_auth.py")))
        self.assertEqual(_strings(os.path.join(EXT, "owner_auth.py"))["WRITE_FIELD"], self.probe.WRITE_FIELD)
        self.assertEqual(_strings(os.path.join(EXT, "plan_confirm.py"))["WRITE_SCOPE"], self.probe.WRITE_SCOPE)
        self.assertIn(self.probe.CALLBACK, _read(os.path.join(EXT, "auth_config.py")))

    def test_smoke_refusal_reasons_are_plan_writes_reasons(self):
        writes, smoke = _read(os.path.join(EXT, "plan_writes.py")), _read(SMOKE)
        for reason in ("no_write_permission", "unknown_token"):
            with self.subTest(reason=reason):
                self.assertIn(f'"{reason}":', writes)
                self.assertIn(reason, smoke)


if __name__ == "__main__":
    unittest.main()
