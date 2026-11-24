"""Flag and helper tests for the maintainer scripts: test-connector.sh,
test-protocol.py, test-signin.py and e2e-local.py.

Standard library only; no network beyond a loopback stub server on port 0,
no Docker, no server of the project started. The test-protocol tests need the
mcp client (its import block exits without it): they skip with "needs mcp 2.x"
and fail instead under REQUIRE_UPSTREAM=1.
"""

import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
ROOT = os.path.dirname(SCRIPTS)
CONNECTOR = os.path.join(SCRIPTS, "test-connector.sh")
PROTOCOL = os.path.join(SCRIPTS, "test-protocol.py")
SIGNIN = os.path.join(SCRIPTS, "test-signin.py")
E2E = os.path.join(SCRIPTS, "e2e-local.py")
ENABLED_TOOLS = os.path.join(ROOT, "config", "enabled-tools.txt")
REQUIRE_UPSTREAM = os.environ.get("REQUIRE_UPSTREAM") == "1"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def has_mcp():
    try:
        return importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        return False


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def names_in(path):
    names = {"".join(line.split("#", 1)[0].split()) for line in read(path).splitlines()}
    names.discard("")
    return names


# --------------------------------------------------------------------------
# test-connector.sh against a loopback stub


class Stub(BaseHTTPRequestHandler):
    readyz = 200  # set per test on the class the server uses

    def log_message(self, *args):  # keep the test output quiet
        pass

    def base(self):
        return "http://" + self.headers["Host"]

    def answer(self, code, body=b"", ctype="application/json", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = self.base()
        if self.path == "/healthz":
            self.answer(200, b"ok", "text/plain")
        elif self.path == "/readyz":
            self.answer(self.readyz, b"", "text/plain")
        elif self.path == "/.well-known/oauth-protected-resource/mcp":
            self.answer(200, json.dumps({"resource": base + "/mcp", "authorization_servers": [base]}).encode())
        elif self.path == "/.well-known/oauth-authorization-server":
            self.answer(200, json.dumps({"issuer": base, "registration_endpoint": base + "/register"}).encode())
        else:
            self.answer(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/mcp":
            meta = f'Bearer resource_metadata="{self.base()}/.well-known/oauth-protected-resource/mcp"'
            self.answer(401, b"", headers=(("WWW-Authenticate", meta),))
        elif self.path.endswith("/mcp"):
            self.answer(410)
        else:
            self.answer(404)


@unittest.skipUnless(shutil.which("bash") and shutil.which("curl"), "needs bash and curl on PATH")
class ConnectorExpectUnready(unittest.TestCase):
    def serve(self, readyz):
        handler = type("StubHandler", (Stub,), {"readyz": readyz})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def run_script(self, *args):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(), "LC_ALL": "C"}
        return subprocess.run([shutil.which("bash"), CONNECTOR, *args], env=env, capture_output=True, text=True,
                              timeout=120)

    def test_503_with_the_flag_passes(self):
        result = self.run_script("--expect-unready", self.serve(503))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: GET /readyz returned 503 (not ready: no Garmin login yet, or Garmin refused the key; "
                      "as --expect-unready expects)", result.stdout)
        self.assertIn("RESULT: PASS", result.stdout)

    def test_503_without_the_flag_fails_with_the_mode_neutral_text(self):
        result = self.run_script(self.serve(503))
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL: GET /readyz returned 503: Garmin login not done yet, or still loading (wait 30 s after "
                      "garmin-mcp-auth and retry); in key mode, Garmin refused the key at startup (make a new key, "
                      "set it, redeploy)", result.stdout)

    def test_200_with_the_flag_fails(self):
        result = self.run_script("--expect-unready", self.serve(200))
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL: GET /readyz returned 200 (--expect-unready expects 503: no Garmin login yet, "
                      "or a refused key)", result.stdout)

    def test_200_without_the_flag_passes(self):
        result = self.run_script(self.serve(200))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PASS: GET /readyz returned 200 (Garmin login or key accepted)", result.stdout)

    def test_usage_errors(self):
        for args in ((), ("--expect-unready",), ("a", "b"), ("--expect-unready", "a", "b")):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage: ", result.stderr)
                self.assertIn("[--expect-unready] <base-url>", result.stderr)

    def test_help_prints_the_usage_and_exits_0(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                result = self.run_script(flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("[--expect-unready] <base-url>", result.stdout)
                self.assertNotIn("FAIL", result.stdout + result.stderr)

    def test_a_base_that_is_not_https_or_loopback_http_is_refused_before_any_request(self):
        seen = []
        handler = type("CountingHandler", (Stub,), {
            "readyz": 200,
            "do_GET": lambda h: (seen.append(h.path), Stub.do_GET(h)),
            "do_POST": lambda h: (seen.append(h.path), Stub.do_POST(h))})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        bearer = "fake-" + os.urandom(12).hex()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(), "LC_ALL": "C",
               "MCP_BEARER_TOKEN": bearer}
        for base in (f"127.0.0.1:{port}", f"localhost:{port}/mcp", f"https:/127.0.0.1:{port}",
                     f"http://127.0.0.1:{port}@127.0.0.1:{port}", f"http://127.0.0.1.invalid:{port}",
                     f"http://localhost.invalid:{port}", "https://", f"ftp://127.0.0.1:{port}"):
            for args in ((base,), ("--expect-unready", base)):
                with self.subTest(args=args):
                    result = subprocess.run([shutil.which("bash"), CONNECTOR, *args], env=env,
                                            capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("[--expect-unready] <base-url>", result.stderr)
                    self.assertNotIn(bearer, result.stdout + result.stderr)
        self.assertEqual(seen, [])
        # The loopback forms still run (and reach the stub).
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host=host):
                result = self.run_script(f"http://{host}:{port}/")
                self.assertIn("PASS: GET /healthz returned 200", result.stdout)
        self.assertIn("/healthz", seen)


class ConnectorText(unittest.TestCase):
    def test_code_never_uses_the_word_read(self):
        code = "\n".join(line for line in read(CONNECTOR).splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\bread\b")
        self.assertIn('EXPECT_UNREADY=0', code)

    def test_header_names_the_flag(self):
        self.assertIn("#   scripts/test-connector.sh --expect-unready <base-url>   before the Garmin login (and in CI): "
                      "/readyz must be 503", read(CONNECTOR))


# --------------------------------------------------------------------------
# test-protocol.py


def needs_mcp(cls):
    if has_mcp() or REQUIRE_UPSTREAM:
        return cls
    return unittest.skip("needs mcp 2.x (the official client) for scripts/test-protocol.py")(cls)


def tool(name):
    return types.SimpleNamespace(name=name, annotations=types.SimpleNamespace(read_only_hint=True))


@needs_mcp
class ProtocolFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tp = load(PROTOCOL, "test_protocol_script")

    def setUp(self):
        self.tp.results.clear()
        self.tp.skipped.clear()
        self.tp.report_details.clear()
        self.tmp = tempfile.mkdtemp(prefix="tp-flags-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def args(self, *argv):
        return self.tp.parse_args(["--url", "http://127.0.0.1:9/mcp", *argv])

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_preview_default_is_loopback_only(self):
        for url, want in (("http://127.0.0.1:8000/mcp", True), ("http://[::1]:8000/mcp", True),
                          ("http://localhost:8000/mcp", True), ("http://LOCALHOST/mcp", True),
                          ("https://app.example.org/mcp", False), ("http://10.0.0.5:8000/mcp", False),
                          ("http://127.0.0.2/mcp", False), ("http://localhost.example.org/mcp", False)):
            with self.subTest(url=url):
                self.assertIs(self.tp.preview_default(url), want)
                self.assertIs(self.tp.parse_args(["--url", url]).preview, want)

    def test_preview_flags_override(self):
        self.assertIs(self.tp.parse_args(["--url", "https://app.example.org/mcp", "--preview"]).preview, True)
        self.assertIs(self.tp.parse_args(["--url", "http://127.0.0.1:1/mcp", "--no-preview"]).preview, False)

    def test_save_reply_needs_a_tool_call(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            with open(os.devnull, "w") as null:
                old, sys.stderr = sys.stderr, null
                try:
                    self.args("--save-reply", os.path.join(self.tmp, "r.json"))
                finally:
                    sys.stderr = old
        self.assertEqual(cm.exception.code, 2)

    def test_tools_file_reads_names_as_enabled_tools_does(self):
        path = self.write("tools.txt", "# comment\nget_stats\n  get_activities   # trailing\n\nget_stats\n")
        args = self.args("--tools-file", path)
        self.assertEqual(args.tool_names, {"get_stats", "get_activities"})
        self.assertEqual(args.expect_tools, 2)
        self.assertEqual(self.args("--tools-file", path, "--expect-tools", "5").expect_tools, 5)
        self.assertEqual(self.args().tool_names, names_in(ENABLED_TOOLS))

    def test_expect_version_default_reads_ext_version_with_ast(self):
        one = self.write("a/ext/version.py", 'VERSION = "1.2.3"\n')
        self.assertEqual(self.tp.checkout_version(one), "1.2.3")
        doc = self.write("b/ext/version.py", '"""The version."""\nraise SystemExit("never imported")\n'
                                             'VERSION = "4.5.6"\n')
        self.assertEqual(self.tp.checkout_version(doc), "4.5.6")
        self.assertIsNone(self.tp.checkout_version(os.path.join(self.tmp, "none", "ext", "version.py")))
        for text in ('VERSION = "1"\nVERSION = "2"\n', "VERSION = 1\n", "OTHER = 'x'\n", "VERSION = (\n"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.tp.checkout_version(self.write("c/ext/version.py", text))

    def test_expect_version_default_is_this_checkouts(self):
        want = self.tp.checkout_version()
        self.assertEqual(self.args().expect_version, want)
        self.assertEqual(self.args("--expect-version", "any").expect_version, "any")

    def eras(self, modern_version="1.0.0", legacy_version="1.0.0", name="coach-mcp-for-garmin",
             call_text=None, call_error=False, tools=("get_daily_status", "get_stats")):
        def era(version):
            data = {"tools": [tool(n) for n in tools], "resources": [], "instructions": None,
                    "server_info": types.SimpleNamespace(name=name, version=version),
                    "bad_date": None}
            if call_text is not None:
                data["call"] = {"is_error": call_error, "text": call_text}
            return data
        return era(modern_version), era(legacy_version)

    def check(self, *argv, **eras):
        args = self.args("--expect-resources", "", *argv)
        args.tool_names = {"get_daily_status", "get_stats"}
        args.expect_tools = 2
        with redirect_stdout(io.StringIO()) as out:
            self.tp.check_eras(*self.eras(**eras), args)
        return {n: (ok, d) for n, ok, d in self.tp.results}, out.getvalue()

    def test_server_version_and_name(self):
        got, _ = self.check("--expect-version", "1.0.0")
        self.assertEqual(got["server/version"],
                         (True, "serverInfo version: modern 1.0.0, legacy 1.0.0 (expected 1.0.0)"))
        self.assertTrue(got["server/name"][0])
        self.tp.results.clear()
        got, _ = self.check("--expect-version", "1.0.0", modern_version="2.3.0", legacy_version="2.3.0")
        self.assertEqual(got["server/version"][0], False)
        self.assertIn("(expected 1.0.0: redeploy this checkout, or pass --expect-version 2.3.0)",
                      got["server/version"][1])
        self.tp.results.clear()
        got, _ = self.check("--expect-version", "1.0.0", name="other")
        self.assertFalse(got["server/name"][0])

    def test_any_needs_the_same_version_in_both_eras(self):
        got, _ = self.check("--expect-version", "any", modern_version="9.9.9", legacy_version="9.9.9")
        self.assertTrue(got["server/version"][0])
        self.tp.results.clear()
        got, _ = self.check("--expect-version", "any", modern_version="9.9.9", legacy_version="9.9.8")
        self.assertFalse(got["server/version"][0])

    def test_no_version_file_gives_skip(self):
        args = self.args("--expect-resources", "")
        args.expect_version = None
        args.tool_names = {"get_daily_status", "get_stats"}
        args.expect_tools = 2
        with redirect_stdout(io.StringIO()):
            self.tp.check_eras(*self.eras(), args)
        self.assertIn(("server/version", "no ext/version.py in this checkout"), self.tp.skipped)

    def test_tools_record_checks_the_names(self):
        got, _ = self.check("--expect-version", "1.0.0", tools=("get_daily_status", "get_other"))
        self.assertFalse(got["tools/modern"][0])
        self.assertIn("not in the tools file: get_other", got["tools/modern"][1])
        self.assertIn("missing: get_stats", got["tools/modern"][1])

    def test_call_state(self):
        self.assertEqual(self.tp.call_state(False, '{"steps": 1}'), "data")
        self.assertEqual(self.tp.call_state(False, "No data: nothing recorded"), "no-data")
        self.assertEqual(self.tp.call_state(True, "Error: Garmin login missing or expired."),
                         "error Error: Garmin login")

    def test_no_preview_hides_the_reply_and_the_report_never_holds_it(self):
        secret_text = "resting heart rate 47 on a made-up day"
        got, printed = self.check("--expect-version", "1.0.0", "--tool-call", "get_stats", "--no-preview",
                                  call_text=secret_text)
        self.assertEqual(got["tool-call/modern"], (True, f"get_stats: data, {len(secret_text)} chars"))
        self.assertNotIn(secret_text, printed)
        self.tp.results.clear()
        self.tp.report_details.clear()
        got, printed = self.check("--expect-version", "1.0.0", "--tool-call", "get_stats", "--preview",
                                  call_text=secret_text)
        self.assertIn(secret_text, printed)
        report = self.tp.report_json("https://app.example.org/mcp")
        self.assertNotIn(secret_text, json.dumps(report))
        self.assertEqual(set(report), {"script", "url_host", "passed", "failed", "skipped", "results"})
        self.assertEqual(report["script"], "test-protocol")
        self.assertEqual(report["url_host"], "app.example.org")
        for row in report["results"]:
            self.assertEqual(set(row), {"name", "ok", "detail"})

    def test_report_and_reply_files_are_private(self):
        token = "tok-" + os.urandom(12).hex()
        os.environ["MCP_BEARER_TOKEN_UNUSED"] = token  # never read by the script; only a value to look for
        self.addCleanup(os.environ.pop, "MCP_BEARER_TOKEN_UNUSED", None)
        with redirect_stdout(io.StringIO()):
            self.tp.record("auth/x", True, "HTTP 401")
        path = os.path.join(self.tmp, "report.json")
        self.tp.write_private(path, json.dumps(self.tp.report_json("http://127.0.0.1:1/mcp")))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertNotIn(token, read(path))
        reply = os.path.join(self.tmp, "reply.json")
        self.tp.write_private(reply, "{}")
        self.assertEqual(stat.S_IMODE(os.stat(reply).st_mode), 0o600)

    def test_docstring_keeps_the_pins_and_the_ci_sentence(self):
        doc = read(PROTOCOL).split('"""', 2)[1]
        self.assertIn("read -rs MCP_BEARER_TOKEN && export MCP_BEARER_TOKEN", doc)
        self.assertNotRegex(doc, r"\b(OWNER_SECRET|MCP_BEARER_TOKEN)=\S")
        self.assertIn("Without --tool-call the only tool call is get_daily_status with the date 2026-02-30, which "
                      "the server refuses before any Garmin call, so a server with no Garmin login passes (CI uses "
                      "this).", " ".join(doc.split()))


class ProtocolSource(unittest.TestCase):
    """Checks on the source that need no mcp."""

    def test_python_310_fallback_for_base_exception_group(self):
        text = read(PROTOCOL)
        self.assertIn("    from builtins import BaseExceptionGroup  # Python 3.11+\n", text)
        self.assertIn("    from exceptiongroup import BaseExceptionGroup\n", text)
        self.assertIn("while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:", text)

    def test_compiles_on_this_python(self):
        compile(read(PROTOCOL), PROTOCOL, "exec")


# --------------------------------------------------------------------------
# test-signin.py


class SigninFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = load(SIGNIN, "test_signin_script")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ts-flags-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def parse(self, *argv):
        return self.ts.parse_args(["--base", "http://127.0.0.1:1", *argv])[1]

    def check(self, *argv):
        return self.ts.SignInCheck(self.parse(*argv), "owner-" + os.urandom(16).hex(), None)

    def test_protocol_args_are_split_and_appended_after_expect_tools(self):
        check = self.check("--run-protocol", "--protocol-args", "--expect-version 1.0.0 --report 'a b.json'")
        cmd = check.protocol_command()
        i = cmd.index("--expect-tools")
        self.assertEqual(cmd[i + 1], str(len(names_in(ENABLED_TOOLS))))
        self.assertEqual(cmd[-4:], ["--expect-version", "1.0.0", "--report", "a b.json"])
        self.assertGreater(cmd.index("--expect-version"), i)
        self.assertEqual(cmd[cmd.index("--tools-file") + 1], os.path.abspath(ENABLED_TOOLS))

    def test_bad_protocol_args_are_refused(self):
        with open(os.devnull, "w") as null:
            old, sys.stderr = sys.stderr, null
            try:
                with self.assertRaises(SystemExit):
                    self.parse("--protocol-args", "--report 'unclosed")
            finally:
                sys.stderr = old

    def cut_body_server(self, status):
        """A loopback server that answers every request with `status`, a
        Content-Length of 88 and no body, then closes (what Caddy sends when the
        upstream answers 413 early and resets the connection)."""
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        self.addCleanup(srv.close)

        def serve():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                got = b""
                while b"\r\n\r\n" not in got:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    got += chunk
                conn.sendall(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                             "Content-Length: 88\r\nConnection: close\r\n\r\n".encode())
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return f"http://127.0.0.1:{srv.getsockname()[1]}"

    def cut_body_request(self, status):
        base = self.cut_body_server(status)
        args = self.ts.parse_args(["--base", base, "--timeout", "10"])[1]
        check = self.ts.SignInCheck(args, "owner-" + os.urandom(16).hex(), None)
        return check.http("POST", "/register", ip=check.ip(), json_body={"pad": "x" * 70000})

    def test_a_413_whose_body_was_cut_still_counts(self):
        r = self.cut_body_request(413)
        self.assertEqual(r.status, 413)

    def test_a_cut_body_on_any_other_status_still_fails(self):
        import http.client
        with self.assertRaises((http.client.IncompleteRead, ConnectionResetError)):
            self.cut_body_request(400)

    def test_tools_file_sets_the_expected_count(self):
        path = os.path.join(self.tmp, "key.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("get_stats\nget_activities # c\n\nget_hrv_trend\n")
        args = self.parse("--tools-file", path)
        self.assertEqual(args.expect_tools, 3)
        self.assertEqual(self.parse("--tools-file", path, "--expect-tools", "7").expect_tools, 7)
        check = self.ts.SignInCheck(args, "owner-" + os.urandom(16).hex(), None)
        self.assertEqual(check.expect_names, {"get_stats", "get_activities", "get_hrv_trend"})

    def stubbed_run(self, *argv):
        check = self.check(*argv)
        called = []

        def stub(cid):
            def fn():
                called.append(cid)
                return True, f"stub {cid}"
            return fn
        for cid in self.ts.ORDER:
            setattr(check, cid.lower(), stub(cid))
        with redirect_stdout(io.StringIO()):
            rc = check.run()
        return check, called, rc

    def test_live_with_run_protocol_runs_s46(self):
        _, called, rc = self.stubbed_run("--live", "--run-protocol")
        self.assertEqual(rc, 0)
        self.assertIn("S46", called)
        self.assertEqual(set(called), self.ts.LIVE | {"S46"})
        _, called, _ = self.stubbed_run("--live")
        self.assertNotIn("S46", called)
        self.assertEqual(set(called), self.ts.LIVE)

    def test_report_shape_and_mode(self):
        check, _, _ = self.stubbed_run("--live", "--run-protocol")
        path = os.path.join(self.tmp, "signin.json")
        with redirect_stdout(io.StringIO()):
            check.write_report(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(set(report), {"script", "mode", "counts", "results"})
        self.assertEqual((report["script"], report["mode"]), ("test-signin", "live"))
        self.assertEqual(report["counts"], {"PASS": len(self.ts.LIVE) + 1, "FAIL": 0, "INFO": 0})
        self.assertEqual(report["results"][0], {"id": "S1", "status": "PASS", "detail": "stub S1"})

    def test_s46_fails_when_its_sign_in_is_not_revoked(self):
        check = self.check("--live", "--run-protocol")
        check.client = {"client_id": "c", "client_secret": "s"}
        check.sign_in = lambda ip: {"access_token": "a" * 20, "refresh_token": "r" * 20}
        check.run_protocol = lambda token: (True, "test-protocol.py ...: exit 0")
        for revoked, want in ((True, True), (False, False)):
            with self.subTest(revoked=revoked):
                check.revoke_own = lambda client, tokens, ip, revoked=revoked: revoked
                ok, detail = check.s46()
                self.assertIs(ok, want)
                self.assertTrue(detail.endswith(f"; its sign-in revoked: {'yes' if revoked else 'no'}"))

    def test_s46_revokes_even_when_test_protocol_fails(self):
        check = self.check("--run-protocol")
        check.client = {"client_id": "c", "client_secret": "s"}
        check.sign_in = lambda ip: {"access_token": "a" * 20, "refresh_token": "r" * 20}
        revokes = []

        def boom(token):
            raise OSError("cannot start")
        check.run_protocol = boom
        check.revoke_own = lambda client, tokens, ip: revokes.append(tokens["refresh_token"]) or True
        with self.assertRaises(OSError):
            check.s46()
        self.assertEqual(revokes, ["r" * 20])

    def test_docstring(self):
        text = read(SIGNIN)
        doc = " ".join(text.split('"""', 2)[1].split())
        self.assertIn("--live a real deployment: only the smoke subset (S1-S6, S10, S11, S14, S23, S27, S28, S32, "
                      "S33, and S46 with --run-protocol). It registers three clients, signs in once (twice with "
                      "--run-protocol) and revokes those sign-ins (S32, S46).", doc)
        self.assertIn("asks for OWNER_SECRET at a prompt that does not echo", doc)
        self.assertIn("read -rs MCP_BEARER_TOKEN && export MCP_BEARER_TOKEN", doc)
        self.assertNotRegex(doc, r"\b(OWNER_SECRET|MCP_BEARER_TOKEN)=\S")


# --------------------------------------------------------------------------
# e2e-local.py


class E2EFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e2e = load(E2E, "e2e_local_script")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e-flags-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_server_env_is_full_mode_and_the_bad_env_inherits_it(self):
        args = self.e2e.parse_args(["--work", self.tmp, "--server-port", "8670", "--edge-port", "8671"])
        run = self.e2e.E2E(args)
        env = run.server_env("leg", "http://127.0.0.1:8670")
        self.assertEqual(env["GARMIN_AUTH"], "full")
        self.assertEqual(env["REPORT_TZ"], "UTC")
        self.assertNotIn("GARMIN_AUTH", self.e2e.BAD_ENV)
        bad = dict(env, **self.e2e.BAD_ENV)
        self.assertEqual(bad["GARMIN_AUTH"], "full")
        self.assertEqual([cid for cid, _ in self.e2e.BAD_ENV_LINES], ["A2", "A3", "A5", "A8"])
        self.assertTrue(run.run_dir.startswith(self.tmp + os.sep))

    def test_legs(self):
        self.assertEqual(self.e2e.parse_args(["--work", self.tmp]).legs, ["direct", "caddy", "live"])
        self.assertEqual(self.e2e.parse_args(["--work", self.tmp, "--legs", "live"]).legs, ["live"])
        with open(os.devnull, "w") as null:
            old, sys.stderr = sys.stderr, null
            try:
                for legs in ("bogus", "direct,remote", ""):
                    with self.subTest(legs=legs), self.assertRaises(SystemExit):
                        self.e2e.parse_args(["--work", self.tmp, "--legs", legs])
            finally:
                sys.stderr = old

    def test_live_signin_command(self):
        cmd = self.e2e.live_signin_command("py", "http://127.0.0.1:8670", "/r/signin.json", "/r/p q.json")
        for flag in ("--live", "--run-protocol", "--report", "--protocol-args", "--protocol-python"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--protocol-args") + 1], "--report '/r/p q.json'")
        self.assertNotIn("--secrets-out", cmd)

    def write_json(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_s46_revoked_reads_the_report(self):
        def report(status, detail, mode="live"):
            return self.write_json("s.json", {"script": "test-signin", "mode": mode, "counts": {},
                                              "results": [{"id": "S46", "status": status, "detail": detail}]})
        self.assertTrue(self.e2e.s46_revoked(report("PASS", "exit 0; its sign-in revoked: yes"))[0])
        self.assertFalse(self.e2e.s46_revoked(report("PASS", "exit 0; its sign-in revoked: no"))[0])
        self.assertFalse(self.e2e.s46_revoked(report("FAIL", "exit 1; its sign-in revoked: yes"))[0])
        self.assertFalse(self.e2e.s46_revoked(report("PASS", "its sign-in revoked: yes", mode="local"))[0])
        self.assertFalse(self.e2e.s46_revoked(os.path.join(self.tmp, "missing.json"))[0])

    def test_protocol_version_passed_reads_the_report(self):
        ok = self.write_json("p.json", {"results": [{"name": "server/version", "ok": True, "detail": "x"}],
                                        "skipped": []})
        self.assertTrue(self.e2e.protocol_version_passed(ok)[0])
        skipped = self.write_json("q.json", {"results": [], "skipped": [["server/version", "no ext/version.py"]]})
        result = self.e2e.protocol_version_passed(skipped)
        self.assertFalse(result[0])
        self.assertIn("skipped", result[1])

    def test_docstring_mentions_full_mode_and_the_live_leg(self):
        doc = " ".join(read(E2E).split('"""', 2)[1].split())
        self.assertIn("GARMIN_AUTH=full", doc)
        self.assertIn("--legs direct,caddy,live", doc)
        self.assertIsNone(re.search(r"\b(OWNER_SECRET|MCP_BEARER_TOKEN)=\S", doc))


if __name__ == "__main__":
    unittest.main()
