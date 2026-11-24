"""Static checks of the files around the server: the Caddyfile, scripts/deploy.sh,
scripts/test-connector.sh, compose.yaml, the ignore files, .env.example, and the
docs (README.md, docs/, SECURITY.md, CHANGELOG.md, ext/README.md, recipes/).

    python -m unittest discover -s ext -p "test_edge_files*.py" -v

Standard library only. When a caddy binary is found, `caddy validate` and
`caddy adapt` also run on the Caddyfile (neither starts a listener); without
one those tests are skipped. deploy.sh never runs: its new_owner_secret and
public_origin functions are taken out with a regex and run on their own
with bash, so no railway command can run. Nothing here binds a port or uses
the network. A generated secret is compared in memory and never printed.
The docs checks read Markdown as text: every relative link and anchor
resolves, the must-keep texts are where the docs say, and the docs agree
with the config files they describe (tool lists, .env.example, RULES_VERSION,
VERSION, the pinned garminconnect).
"""

from __future__ import annotations

import ast
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import unicodedata
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
CADDYFILE = os.path.join(ROOT, "Caddyfile")
DEPLOY = os.path.join(ROOT, "scripts", "deploy.sh")
CONNECTOR = os.path.join(ROOT, "scripts", "test-connector.sh")
ENV_EXAMPLE = os.path.join(ROOT, ".env.example")
README = os.path.join(ROOT, "README.md")
EXT_README = os.path.join(HERE, "README.md")
COMPOSE = os.path.join(ROOT, "compose.yaml")
DOCKERIGNORE = os.path.join(ROOT, ".dockerignore")
GITIGNORE = os.path.join(ROOT, ".gitignore")
DOCS_DIR = os.path.join(ROOT, "docs")
SECURITY_MD = os.path.join(ROOT, "SECURITY.md")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
CONSTRAINTS = os.path.join(ROOT, "constraints.txt")
VERSION_PY = os.path.join(HERE, "version.py")
KEY_MODE_PY = os.path.join(HERE, "key_mode.py")
TOOLS_FULL = os.path.join(ROOT, "config", "enabled-tools.txt")
TOOLS_KEY = os.path.join(ROOT, "config", "enabled-tools-key.txt")
# The thirteen pages of docs/ (DOCS contract D2, plus evals); README links each of them.
DOC_PAGES = ("deploy-railway", "deploy-docker", "garmin-login", "connect-claude", "tools", "daily-status",
             "weekly-status", "maintenance", "updates", "protocol", "how-it-works", "troubleshooting", "evals")

BASH = shutil.which("bash") or "/bin/bash"
# The OWNER_SECRET alphabet: no 0, 1, i, l or o.
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
SECRET_SHAPE = re.compile(r"[2-9a-hjkmnp-z]{5}(?:-[2-9a-hjkmnp-z]{5}){5}")
# The one line of deploy.sh that may expand the new owner secret.
SECRET_PIPE = """printf '%s' "$SECRET" | railway variable set OWNER_SECRET --stdin --skip-deploys >/dev/null"""
REAL_ROUTES = (
    "/mcp", "/mcp/", "/healthz", "/readyz", "/authorize", "/consent", "/register", "/token", "/revoke",
    "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def code_lines(text):
    """Shell or Caddyfile lines that are not blank and not whole-line comments."""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def caddy_block(code, opener):
    """The stripped lines inside the first `<opener> {` ... matching `}` block."""
    lines = [line.strip() for line in code]
    start = lines.index(opener + " {")
    depth, body = 1, []
    for line in lines[start + 1:]:
        depth += line.endswith("{") - (line == "}")
        if depth == 0:
            return body
        body.append(line)
    raise AssertionError("unclosed block: " + opener)


def shell_function(text, name):
    """A bash function written as `name() {` ... `}` at the start of a line."""
    match = re.search(r"(?ms)^%s\(\) \{\n.*?^\}\n" % re.escape(name), text)
    if match is None:
        raise AssertionError("no function %s() in the script" % name)
    return match.group(0)


def run_bash(script, *args, path="/usr/bin:/bin", extra_env=None):
    env = {"PATH": path}
    env.update(extra_env or {})
    return subprocess.run([BASH, "-c", script, "bash", *args], env=env, capture_output=True, text=True,
                          timeout=60)


def paragraphs(text):
    """The paragraphs, list items and table rows of a Markdown text."""
    return [p for p in re.split(r"\n\s*\n|\n(?=- |\| )", text) if p.strip()]


def find_caddy():
    for candidate in (os.environ.get("CADDY"), shutil.which("caddy"), "/opt/homebrew/bin/caddy",
                      "/usr/local/bin/caddy", "/usr/bin/caddy"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


CADDY = find_caddy()


class Caddyfile(unittest.TestCase):
    """Caddy is the edge only: it logs safely, caps bodies, answers 410 for the
    removed URL-token form and passes every token to Python."""

    def setUp(self):
        self.code = code_lines(read(CADDYFILE))
        self.text = "\n".join(self.code)

    def test_holds_no_secret_and_never_strips_authorization(self):
        # Comments may name the variables (to say Caddy never gets them); code may not.
        for needle in ("MCP_BEARER_TOKEN", "OWNER_SECRET", "-Authorization", "@authorized"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.text)

    def test_access_log_drops_what_could_replay_a_sign_in(self):
        log = caddy_block(self.code, "log")
        self.assertIn("output stdout", log)
        # Every request header goes (V1-5): a token sent in a header with any
        # name, not only Authorization, stays out of `railway logs`.
        self.assertEqual(caddy_block(self.code, "format filter"), [
            'request>uri regexp "\\?.*$" "?REDACTED"',
            "request>headers delete",
            "resp_headers>Location delete",
            "resp_headers>Set-Cookie delete",
        ])

    def test_removed_token_path_answers_410_unlogged_and_unproxied(self):
        self.assertIn("@legacy path_regexp legacy ^/[A-Za-z0-9._~-]{32,}(/|$)", self.text)
        self.assertEqual(caddy_block(self.code, "handle @legacy"), ["log_skip", "respond 410"])

    def test_legacy_pattern_spares_every_real_route(self):
        pattern = re.search(r"@legacy path_regexp legacy (\S+)", self.text).group(1)
        legacy = re.compile(pattern)  # RE2 and Python agree on this pattern
        for path in REAL_ROUTES:
            with self.subTest(path=path):
                self.assertIsNone(legacy.search(path))
        for path in ("/" + "0" * 64 + "/mcp", "/" + "a" * 32, "/" + "A1._~-" * 6 + "/x"):
            with self.subTest(path=path):
                self.assertIsNotNone(legacy.search(path))
        self.assertIsNone(legacy.search("/" + "a" * 31 + "/mcp"))

    def test_sign_in_request_bodies_are_capped(self):
        for path, size in (("/consent", "16KB"), ("/authorize", "16KB"), ("/register", "64KB")):
            with self.subTest(path=path):
                self.assertEqual(caddy_block(self.code, "request_body " + path), ["max_size " + size])

    def test_proxy_target_and_headers(self):
        proxy = caddy_block(self.code, "reverse_proxy 127.0.0.1:{$GARMIN_MCP_PORT:8000}")
        for line in ("header_up Host {upstream_hostport}", "header_up -Origin",
                     "header_up X-Client-IP {client_ip}", "header_up -X-Forwarded-For",
                     "header_down -Server", "flush_interval -1"):
            with self.subTest(line=line):
                self.assertIn(line, proxy)
        servers = caddy_block(self.code, "servers")
        # The caller-address header: X-Real-IP on Railway, CF-Connecting-IP behind a
        # Cloudflare Tunnel (compose.yaml sets EDGE_CLIENT_IP_HEADER; launch.py checks it).
        self.assertEqual(servers, ["trusted_proxies static 0.0.0.0/0 ::/0",
                                   "client_ip_headers {$EDGE_CLIENT_IP_HEADER:X-Real-IP}"])

    def test_error_answers_carry_no_banner(self):
        # The site's `header -Server` does not reach error routes: without these
        # lines a 502 (server restarting) or Caddy's own 413 says "Server: Caddy".
        self.assertEqual(caddy_block(self.code, "handle_errors"),
                         ["header -Server", "header -Via", 'respond "" {err.status_code}'])


@unittest.skipUnless(CADDY, "no caddy binary found")
class CaddyBinary(unittest.TestCase):
    """The Caddyfile parses, and the adapted JSON proxies the way the sign-in needs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="caddy-check-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def caddy(self, command, port="8080", upstream="8000", extra_env=None):
        env = {"PATH": "/usr/bin:/bin", "PORT": port, "GARMIN_MCP_PORT": upstream}
        env.update(extra_env or {})
        for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            env[name] = os.path.join(self.tmp, name.lower())
            os.makedirs(env[name], exist_ok=True)
        return subprocess.run([CADDY, command, "--config", CADDYFILE, "--adapter", "caddyfile"], env=env,
                              cwd=self.tmp, capture_output=True, text=True, timeout=60)

    def test_validate(self):
        result = self.caddy("validate")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("Valid configuration", result.stdout + result.stderr)

    def test_adapt(self):
        result = self.caddy("adapt", port="8123", upstream="8456")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertNotIn("not formatted", result.stderr)
        config = json.loads(result.stdout)
        self.assertTrue(config["admin"]["disabled"])
        (server,) = config["apps"]["http"]["servers"].values()
        self.assertEqual(server["listen"], [":8123"])
        self.assertEqual(server["trusted_proxies"], {"source": "static", "ranges": ["0.0.0.0/0", "::/0"]})
        self.assertEqual(server["client_ip_headers"], ["X-Real-IP"])
        proxies = list(handlers(server["routes"], "reverse_proxy"))
        self.assertEqual(len(proxies), 3)  # /healthz, /readyz, everything else
        for proxy in proxies:
            self.assertEqual(proxy["upstreams"], [{"dial": "127.0.0.1:8456"}])
            request = proxy["headers"]["request"]
            # V1-1: no X-Forwarded-For reaches Python; X-Client-Ip is the caller.
            self.assertEqual(request["delete"], ["Origin", "X-Forwarded-For"])
            self.assertEqual(request["set"], {"Host": ["{http.reverse_proxy.upstream.hostport}"],
                                              "X-Client-Ip": ["{http.vars.client_ip}"]})
            self.assertNotIn("Authorization", json.dumps(proxy))  # passed through untouched
        caps = {tuple(route["match"][0]["path"]): route["handle"][0] for route in server["routes"]
                if route["handle"][0].get("handler") == "request_body"}
        self.assertEqual(caps, {("/consent",): {"handler": "request_body", "max_size": 16000},
                                ("/authorize",): {"handler": "request_body", "max_size": 16000},
                                ("/register",): {"handler": "request_body", "max_size": 64000}})
        # The access log: every request header deleted, the query string masked.
        (access,) = [log for name, log in config["logging"]["logs"].items() if name != "default"]
        self.assertEqual(access["writer"], {"output": "stdout"})
        self.assertEqual(access["encoder"]["fields"], {
            "request>headers": {"filter": "delete"},
            "request>uri": {"filter": "regexp", "regexp": "\\?.*$", "value": "?REDACTED"},
            "resp_headers>Location": {"filter": "delete"},
            "resp_headers>Set-Cookie": {"filter": "delete"},
        })
        # Error answers (502, Caddy's own 413) drop the banners too.
        deleted = set()
        for handler in handlers(server["errors"]["routes"], "headers"):
            deleted.update(handler.get("response", {}).get("delete", []))
        self.assertTrue({"Server", "Via"} <= deleted, sorted(deleted))

    def test_adapt_behind_cloudflare(self):
        # compose.yaml sets EDGE_CLIENT_IP_HEADER=CF-Connecting-IP; nothing else changes.
        result = self.caddy("adapt", extra_env={"EDGE_CLIENT_IP_HEADER": "CF-Connecting-IP"})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        (server,) = json.loads(result.stdout)["apps"]["http"]["servers"].values()
        self.assertEqual(server["client_ip_headers"], ["CF-Connecting-IP"])
        self.assertEqual(server["trusted_proxies"], {"source": "static", "ranges": ["0.0.0.0/0", "::/0"]})


def handlers(node, name):
    if isinstance(node, dict):
        if node.get("handler") == name:
            yield node
        for value in node.values():
            yield from handlers(value, name)
    elif isinstance(node, list):
        for value in node:
            yield from handlers(value, name)


class DeployScript(unittest.TestCase):
    """deploy.sh generates OWNER_SECRET without ever showing it, reads the domain
    before the build and prints the URL Claude must be given."""

    def setUp(self):
        self.text = read(DEPLOY)
        self.code = code_lines(self.text)

    def index(self, pattern):
        return next(i for i, line in enumerate(self.code) if re.search(pattern, line))

    def test_owner_secret_goes_to_railway_on_stdin_only(self):
        stripped = [line.strip() for line in self.code]
        made = next(i for i, line in enumerate(stripped) if 'SECRET="$(new_owner_secret)"' in line)
        pipe = stripped.index(SECRET_PIPE)
        self.assertLess(made, pipe)
        self.assertLessEqual(pipe - made, 4)  # only the "could not generate" branch in between
        self.assertEqual(stripped[pipe + 1], 'SECRET=""')
        self.assertIn("OWNER_SECRET set (not shown). See it with: railway variable list --kv | grep ^OWNER_SECRET=",
                      self.text)

    def test_the_owner_secret_is_never_printed(self):
        expansion = re.compile(r"\$\{?(?:SECRET|OWNER_SECRET)\b")
        for line in self.code:
            if expansion.search(line):
                self.assertEqual(line.strip(), SECRET_PIPE)
        # The generator prints the secret, so its output is only ever captured.
        calls = [line for line in self.code if "new_owner_secret" in line and "()" not in line]
        self.assertTrue(calls)
        for line in calls:
            self.assertRegex(line, r'SECRET="\$\(new_owner_secret\)"')
        for line in self.code:  # no heredoc that expands variables
            if "<<" in line and "<<<" not in line:
                self.assertRegex(line, r"<<-?'[A-Z]+'")

    def test_no_url_token_form_in_the_output(self):
        for line in self.code:
            with self.subTest(line=line.strip()):
                self.assertNotRegex(line, r"\$\{?TOKEN\}?/mcp")
                if "<token>/mcp" in line:  # only the notice that it is gone
                    self.assertIn("is gone (410)", line)

    def test_bearer_token_only_on_request(self):
        mint = self.index(r"openssl rand")
        self.assertRegex(self.code[mint - 1], r"^if \[\[ \$ROTATE_TOKEN == true \]\]; then$")
        self.assertIn("--rotate-token) ROTATE_TOKEN=true ;;", self.text)
        self.assertIn("--rotate-secret) ROTATE_SECRET=true ;;", self.text)

    def test_domain_step_runs_before_the_build(self):
        build = self.index(r"^\s*railway up\b")
        self.assertLess(self.index(r'^say "Public domain"'), build)
        self.assertLess(self.index(r"railway domain 2>&1"), build)
        self.assertLess(self.index(r"PUBLIC_URL on Railway must be"), build)
        self.assertIn("create one in the dashboard (Settings > Networking > Generate Domain) or set PUBLIC_URL, "
                      "then re-run. Nothing was deployed.", self.text)

    def test_public_url_wins_in_the_done_section(self):
        done = "\n".join(self.code[self.index(r'^say "Done"'):])
        branch = done.index("if [[ -n $PUBLIC_URL ]]; then")
        self.assertLess(branch, done.index('echo "  Service URL    : $PUBLIC_URL '))
        self.assertLess(branch, done.index('echo "  Connector URL  : $PUBLIC_URL/mcp"'))
        self.assertLess(done.index('echo "  Connector URL  : $PUBLIC_URL/mcp"'),
                        done.index('echo "  Connector URL  : https://$DOMAIN/mcp"'))
        self.assertIn("with no trailing slash", done)
        # PUBLIC_URL is checked and normalized in the settings step, before anything changes.
        check = self.index(r'if ! PUBLIC_URL="\$\(public_origin "\$PUBLIC_URL"\)"; then')
        self.assertLess(check, self.index(r"railway variable set"))
        self.assertLess(check, self.index(r"^\s*railway up\b"))

    def public_origin(self, url):
        result = run_bash(shell_function(self.text, "public_origin") + 'public_origin "$1"\n', url)
        return result.stdout.rstrip("\n") if result.returncode == 0 else None

    def test_public_url_is_printed_the_way_the_server_normalizes_it(self):
        for given, wanted in (("https://Coach.Example.com/", "https://coach.example.com"),
                              ("HTTPS://coach.example.com:443", "https://coach.example.com"),
                              (" https://coach.example.com:8443/ ", "https://coach.example.com:8443"),
                              ("https://your-app.up.railway.app", "https://your-app.up.railway.app"),
                              ("https://[2001:db8::1]", "https://[2001:db8::1]")):
            with self.subTest(given=given):
                self.assertEqual(self.public_origin(given), wanted)

    def test_public_url_must_be_an_https_origin(self):
        for url in ("http://coach.example.com", "https://coach.example.com/mcp", "https://coach.example.com//",
                    "https://user@coach.example.com", "https://coach.example.com?x=1",
                    "https://coach.example.com#x", "coach.example.com", "https://", "https://a b.example"):
            with self.subTest(url=url):
                self.assertIsNone(self.public_origin(url))

    def test_public_origin_agrees_with_the_server(self):
        """deploy.sh prints PUBLIC_URL + /mcp as the connector URL, and Claude
        needs it to equal the server's resource, so both read PUBLIC_URL alike."""
        import secrets
        import sys

        sys.path.insert(0, HERE)
        try:
            import auth_config
        finally:
            sys.path.remove(HERE)
        for url in ("https://coach.example.com", "https://Coach.Example.com/", "HTTPS://coach.example.com:443",
                    " https://coach.example.com:8443 ", "https://[2001:db8::1]", "http://coach.example.com",
                    "https://coach.example.com/mcp", "https://coach.example.com//", "https://u@coach.example.com",
                    "https://coach.example.com?x", "https://coach.example.com#x", "coach.example.com"):
            environ = {"GARMIN_MCP_TRANSPORT": "streamable-http", "RAILWAY_PROJECT_ID": "test", "PUBLIC_URL": url,
                       "OWNER_SECRET": "TEST-ONLY-" + secrets.token_urlsafe(24)}
            config, errors = auth_config.config_from_environ(environ)
            with self.subTest(url=url):
                self.assertEqual(self.public_origin(url), config.public_url if config else None, errors)

    # DEPLOY N5 and N6 (deploy.sh half): the Garmin key, GARMIN_AUTH and the health check.
    GARMIN_KEY_LINES = (
        'GARMIN_KEY="${GARMIN_KEY#"${GARMIN_KEY%%[![:space:]]*}"}"',
        'GARMIN_KEY="${GARMIN_KEY%"${GARMIN_KEY##*[![:space:]]}"}"',
        "if [[ -z $GARMIN_KEY || $GARMIN_KEY =~ [[:space:]] || ${#GARMIN_KEY} -gt 512 ]]; then",
        "printf '%s' \"$GARMIN_KEY\" | railway variable set GARMIN_PERSONAL_API_KEY --stdin --skip-deploys >/dev/null",
        # The too-short check (live test B1) reads only the key's length.
        "if [[ ${#GARMIN_KEY} -lt 20 ]]; then",
        "KEY_LEN=${#GARMIN_KEY}",
    )

    def railway_set_lines(self):
        """Code lines that run `railway variable set` (not a hint inside a message)."""
        return [line.strip() for line in self.code
                if re.search(r"(?:^|\|)\s*railway variable set\b", line.strip())]

    def test_the_garmin_key_is_only_trimmed_checked_and_piped(self):
        stripped = [line.strip() for line in self.code]
        expanding = [line for line in stripped if re.search(r"\$\{?#?GARMIN_KEY\b", line)]
        self.assertTrue(expanding)
        for line in expanding:
            with self.subTest(line=line):
                self.assertIn(line, self.GARMIN_KEY_LINES)
        pipe = stripped.index(self.GARMIN_KEY_LINES[3])
        self.assertEqual(stripped[pipe + 1], 'GARMIN_KEY=""')
        (read_line,) = [line for line in stripped if re.search(r"\bread\b.*\bGARMIN_KEY\b", line)]
        self.assertIn("read -rsp", read_line)
        self.assertIn("--set-garmin-key) SET_GARMIN_KEY=true ;;", self.text)

    def test_garmin_auth_is_checked_before_anything_changes(self):
        check = self.index(r"^\s*if \[\[ -z \$AUTH_MODE \]\]; then$")
        first_set = next(i for i, line in enumerate(self.code)
                         if re.search(r"(?:^|\|)\s*railway variable set\b", line.strip()))
        self.assertLess(check, first_set)
        self.assertLess(check, self.index(r"^\s*railway up\b"))
        self.assertIn("GARMIN_AUTH=full does not use GARMIN_PERSONAL_API_KEY", self.text)

    def test_sets_exactly_the_variables_a_user_chooses(self):
        names = set()
        for line in self.railway_set_lines():
            match = re.search(r"railway variable set (?:--skip-deploys )?\"?([A-Z][A-Z0-9_]*)", line)
            self.assertIsNotNone(match, line)
            names.add(match.group(1))
        self.assertEqual(names, {"GARMIN_AUTH", "REPORT_TZ", "GARMIN_PERSONAL_API_KEY", "OWNER_SECRET",
                                 "MCP_BEARER_TOKEN"})

    def test_health_check_is_healthz_never_readyz(self):
        self.assertIn("deploy.healthcheckPath /healthz", self.text)
        for line in self.code:
            if re.search(r"\brailway\b", line):
                with self.subTest(line=line.strip()):
                    self.assertNotIn("/readyz", line)


class NewOwnerSecret(unittest.TestCase):
    """new_owner_secret, run on its own (deploy.sh never runs here)."""

    def setUp(self):
        self.function = shell_function(read(DEPLOY), "new_owner_secret")

    def run_function(self, head_output=None, strict=True):
        """Run it like deploy.sh does (strict: set -euo pipefail). With
        head_output, a stub `head` prints that text instead of random bytes and
        counts its calls."""
        script = ("set -euo pipefail\n" if strict else "") + self.function + "new_owner_secret\n"
        if head_output is None:
            return run_bash(script), None
        tmp = tempfile.TemporaryDirectory(prefix="owner-secret-")
        self.addCleanup(tmp.cleanup)
        calls = os.path.join(tmp.name, "calls")
        stub = os.path.join(tmp.name, "head")
        with open(stub, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho x >>"$CALLS"\nprintf \'%s\' "$STUB_OUTPUT"\n')
        os.chmod(stub, 0o755)
        result = run_bash(script, path=tmp.name + ":/usr/bin:/bin",
                          extra_env={"CALLS": calls, "STUB_OUTPUT": head_output})
        with open(calls, encoding="utf-8") as f:
            return result, len(f.read().split())

    def test_no_railway_call_inside(self):
        self.assertNotIn("railway", self.function)

    def test_shape_and_strength(self):
        outputs = []
        for strict in (True, False):
            result, _ = self.run_function(strict=strict)
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            value = result.stdout.rstrip("\n")
            # Never show the value itself, only its length.
            self.assertTrue(SECRET_SHAPE.fullmatch(value), "not 6 groups of 5 from the alphabet (length %d)" % len(value))
            self.assertEqual(len(value), 35)
            self.assertGreaterEqual(len(set(value.replace("-", ""))), 12)
            outputs.append(value)
        self.assertNotEqual(outputs[0], outputs[1], "two runs gave the same secret")

    def test_keeps_only_the_alphabet_in_order(self):
        noise = "0O1lIioL \n" + "".join(chr(c) for c in range(33, 127) if chr(c) not in ALPHABET)
        result, calls = self.run_function(noise + ALPHABET * 3)
        self.assertEqual((result.returncode, calls), (0, 1))
        self.assertEqual(result.stdout, "23456-789ab-cdefg-hjkmn-pqrst-uvwxy\n")

    def test_twelve_different_characters_are_enough(self):
        result, calls = self.run_function(ALPHABET[:12] * 400)
        self.assertEqual((result.returncode, calls), (0, 1))
        self.assertEqual(len(set(result.stdout.strip().replace("-", ""))), 12)

    def test_retries_five_times_then_fails(self):
        for name, output in (("eleven different characters", ALPHABET[:11] * 400), ("too short", ALPHABET[:29]),
                             ("nothing", "")):
            with self.subTest(name):
                result, calls = self.run_function(output)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(calls, 5)


class ConnectorScript(unittest.TestCase):
    """test-connector.sh checks what Claude's sign-in starts with, and has no
    URL-token mode and no prompt."""

    def setUp(self):
        self.text = read(CONNECTOR)
        self.code = "\n".join(code_lines(self.text))

    def test_no_url_mode_and_no_prompt(self):
        self.assertNotRegex(self.code, r"\bread\b")
        self.assertNotRegex(self.code, r"\$\{?TOKEN\}?/mcp")
        self.assertNotIn("-K -", self.code)  # the removed mode fed the token URL to curl as a config file

    def test_checks_the_sign_in_discovery(self):
        for needle in ('want="resource_metadata=\\"$BASE/.well-known/oauth-protected-resource/mcp\\""',
                       '"$BASE/.well-known/oauth-protected-resource/mcp")',
                       'json_has "$body" resource "$BASE/mcp"',
                       '"$BASE/.well-known/oauth-authorization-server")',
                       'json_has "$body" issuer "$BASE"',
                       '"registration_endpoint"',
                       'echo "SKIP: MCP_BEARER_TOKEN not set (optional)"',
                       "code=$(printf 'Authorization: Bearer %s\\n' \"$TOKEN\" | curl ",
                       "\"$BASE/$(printf '%064d' 0)/mcp\"",
                       'if [ "$code" = "410" ]',
                       "python3 scripts/test-signin.py --live --base $BASE"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.code)

    def test_no_here_string_carries_the_token(self):
        # bash 3.2 (macOS) writes here-string text to a temporary file, so the
        # token reaches curl through a pipe. A command continued with "\" counts
        # as one line.
        commands = self.code.replace("\\\n", " ").splitlines()
        with_token = [line for line in commands if "TOKEN" in line]
        self.assertTrue(with_token)
        for line in with_token:
            with self.subTest(line=line.strip()):
                self.assertNotIn("<<<", line)
                self.assertNotRegex(line, r"<<-?\s*['\"]?[A-Z]")  # no heredoc either

    def test_static_bearer_reaches_curl_on_stdin_only(self):
        # The script against a stand-in curl that logs its arguments and stdin
        # (the header file) and answers 200 with an InitializeResult only when
        # the headers come from stdin.
        token = "TEST-ONLY-" + secrets.token_hex(16)
        with tempfile.TemporaryDirectory(prefix="connector-") as tmp:
            curl = os.path.join(tmp, "curl")
            with open(curl, "w", encoding="utf-8") as f:
                f.write('#!/usr/bin/env bash\n'
                        'printf "%s\\n" "$*" >>"$CURL_ARGS"\n'
                        'out=/dev/null; prev=""\n'
                        'for a in "$@"; do [ "$prev" = -o ] && out="$a"; prev="$a"; done\n'
                        'case " $* " in\n'
                        '  *" -H @- "*) cat >>"$CURL_STDIN"\n'
                        '    printf \'{"result":{"protocolVersion":"x","serverInfo":{}}}\' >"$out"; printf 200 ;;\n'
                        '  *) printf 000 ;;\n'
                        'esac\n')
            os.chmod(curl, 0o755)
            args, stdin = os.path.join(tmp, "args"), os.path.join(tmp, "stdin")
            result = run_bash('bash "$1" "$2"', CONNECTOR, "https://coach.example.com",
                              path=tmp + ":/usr/bin:/bin",
                              extra_env={"MCP_BEARER_TOKEN": token, "CURL_ARGS": args, "CURL_STDIN": stdin})
            self.assertIn("PASS: Static bearer (Authorization: Bearer) returned an InitializeResult",
                          result.stdout)
            self.assertNotIn(token, read(args), "the token is on a curl command line")
            self.assertEqual(read(stdin), "Authorization: Bearer " + token + "\n")

    def test_json_match_is_exact_and_whitespace_tolerant(self):
        helpers = "".join(line + "\n" for line in self.text.splitlines()
                          if line.startswith(("re_escape() ", "json_has() ")))
        self.assertEqual(len(helpers.splitlines()), 2)
        with tempfile.TemporaryDirectory(prefix="json-has-") as tmp:
            doc = os.path.join(tmp, "doc.json")
            for body, value, ok in (('{"resource":"http://127.0.0.1:8080/mcp"}', "http://127.0.0.1:8080/mcp", True),
                                    ('{ "resource" :\n "http://[::1]:8080/mcp" }', "http://[::1]:8080/mcp", True),
                                    ('{"resource":"http://127x0x0x1:8080/mcp"}', "http://127.0.0.1:8080/mcp", False),
                                    ('{"resource":"http://127.0.0.1:8080/mcp/x"}', "http://127.0.0.1:8080/mcp", False),
                                    ('{"resource":"https://a.example/mcp"}', "https://a.example", False)):
                with self.subTest(body=body, value=value):
                    with open(doc, "w", encoding="utf-8") as f:
                        f.write(body)
                    result = run_bash(helpers + 'json_has "$1" resource "$2"\n', doc, value)
                    self.assertEqual(result.returncode == 0, ok)


class ShellSyntax(unittest.TestCase):
    def test_bash_parses_both_scripts(self):
        for path in (DEPLOY, CONNECTOR):
            with self.subTest(path=os.path.basename(path)):
                result = subprocess.run([BASH, "-n", path], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)


class EnvExample(unittest.TestCase):
    def test_owner_secret_and_the_optional_sign_in_settings(self):
        text = read(ENV_EXAMPLE)
        self.assertRegex(text, r"(?m)^OWNER_SECRET=\S+$")
        self.assertNotRegex(text, r"(?m)^\s*MCP_BEARER_TOKEN=")
        for name in ("PUBLIC_URL", "MCP_BEARER_TOKEN", "AUTH_DB", "ALLOWED_REDIRECT_URIS"):
            with self.subTest(name=name):
                self.assertRegex(text, r"(?m)^# %s=" % name)
        # Required, with no default: an example value would start the server on a wrong zone or mode.
        self.assertRegex(text, r"(?m)^REPORT_TZ=$")
        self.assertRegex(text, r"(?m)^GARMIN_AUTH=$")

    def test_the_garmin_key_is_only_a_commented_placeholder(self):
        text = read(ENV_EXAMPLE)
        self.assertRegex(text, r"(?m)^# GARMIN_PERSONAL_API_KEY=$")
        self.assertNotRegex(text, r"(?m)^\s*GARMIN_PERSONAL_API_KEY=")
        # Refused (the base URL) or internal (the token cache): never offered as a setting.
        for name in ("GARMIN_PERSONAL_API_BASE_URL", "GARMIN_KEY_TOKEN_CACHE"):
            with self.subTest(name=name):
                self.assertNotIn(name, text)


# sh, and dash (the image's /bin/sh) where it is installed.
SHELLS = [name for name in ("sh", "dash") if shutil.which(name)]


@unittest.skipUnless(SHELLS, "needs sh on PATH")
class EntrypointPort(unittest.TestCase):
    """entrypoint.sh refuses a PORT Caddy cannot listen on, or the internal
    garmin-mcp port, before anything starts. Its settings block, from the PORT
    line to the Garmin credentials check, runs on its own with each shell."""

    def setUp(self):
        text = read(os.path.join(ROOT, "entrypoint.sh"))
        start = text.index('PORT="${PORT:-8080}"')
        self.block = text[start:text.index("# --- Garmin credentials", start)]
        self.code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]

    def run_block(self, port):
        """(return code, stdout, stderr), the same from every shell in SHELLS."""
        env = {"PATH": os.environ.get("PATH", "")}
        if port is not None:
            env["PORT"] = port
        script = ('set -eu\nlog() { echo "entrypoint: $*" >&2; }\ndie() {\n\tlog "FATAL: $*"\n\texit 1\n}\n'
                  + self.block + 'printf "%s" "$PORT"\n')
        results = {shell: subprocess.run([shell, "-c", script], env=env, stdin=subprocess.DEVNULL,
                                         capture_output=True, text=True, timeout=30) for shell in SHELLS}
        outcomes = {(p.returncode, p.stdout, p.stderr) for p in results.values()}
        self.assertEqual(len(outcomes), 1, results)
        return results[SHELLS[0]]

    def test_good_ports_start(self):
        for port, used in ((None, "8080"), ("", "8080"), ("8080", "8080"), ("1", "1"), ("443", "443"),
                           ("7999", "7999"), ("8001", "8001"), ("65535", "65535")):
            with self.subTest(port=port):
                proc = self.run_block(port)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, used, ""))

    def test_bad_ports_stop_with_a_line_naming_port(self):
        for port in ("0", "65536", "99999", "123456", "08080", "-1", "+80", "80 80", " 8080", "8080\n", "abc",
                     "8080x", "1e3"):
            with self.subTest(port=port):
                proc = self.run_block(port)
                self.assertEqual((proc.returncode, proc.stdout), (1, ""))
                self.assertEqual(proc.stderr, "entrypoint: FATAL: PORT must be a whole number from 1 to 65535. "
                                 "Unset it (the default is 8080) or fix it (Railway: service variables; "
                                 "Docker: compose.yaml).\n")

    def test_the_internal_port_is_refused(self):
        proc = self.run_block("8000")
        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertTrue(proc.stderr.startswith("entrypoint: FATAL: PORT must not be 8000: that is garmin-mcp's "
                                               "internal port"), proc.stderr)

    def test_checked_before_anything_starts(self):
        def first(needle):
            return next(i for i, line in enumerate(self.code) if needle in line)

        check = first('[ "$PORT" -ne "$GARMIN_MCP_PORT" ] ||')
        for later in ("launch.py --check-config", "while :", "caddy run"):
            with self.subTest(later=later):
                self.assertLess(check, first(later))


def flat(text):
    """Whitespace collapsed, so a pinned text may wrap anywhere."""
    return " ".join(text.split())


def rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def doc(name):
    return os.path.join(DOCS_DIR, name + ".md")


def doc_files():
    """The doc set: README, SECURITY, CHANGELOG, docs/*.md, ext/README.md and recipes/**/*.md, those that
    exist. A filled-in `*.local.md` copy (git ignores it) is the user's own file and is never read."""
    paths = [README, SECURITY_MD, CHANGELOG, EXT_README]
    paths += sorted(glob.glob(os.path.join(DOCS_DIR, "*.md")))
    paths += sorted(glob.glob(os.path.join(ROOT, "recipes", "**", "*.md"), recursive=True))
    return [p for p in paths if os.path.isfile(p) and not p.endswith(".local.md")]


def tool_names(path):
    """Tool names of a list file: one per line, '#' starts a comment."""
    names = set()
    for line in read(path).splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.add(name)
    return names


def key_mode_tools():
    """The key-mode list: config/enabled-tools-key.txt, or else KEY_MODE_TOOLS in ext/key_mode.py
    (read with ast, never imported). None when neither exists."""
    if os.path.isfile(TOOLS_KEY):
        return tool_names(TOOLS_KEY)
    if os.path.isfile(KEY_MODE_PY):
        for node in ast.walk(ast.parse(read(KEY_MODE_PY))):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(getattr(target, "id", None) == "KEY_MODE_TOOLS" for target in targets):
                    value = node.value
                    if isinstance(value, ast.Call) and value.args:  # frozenset({...})
                        value = value.args[0]
                    return set(ast.literal_eval(value))
    return None


def section(text, heading):
    """The lines under the line `heading` up to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    match = re.search(r"(?m)^%s[ \t]*$" % re.escape(heading), text)
    if match is None:
        return None
    rest = text[match.end():]
    end = re.search(r"(?m)^#{1,%d} " % level, rest)
    return rest[:end.start()] if end else rest


# The link and anchor checker (DOCS contract T-LINK, T-HEAD): GitHub's rules, standard library only.
FENCE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$")
INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)
HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
# [text](target) and ![alt](target); the target may be <...>; an optional "title" follows.
LINK = re.compile(r"!?\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*(<[^>\n]*>|[^\s()]+)(?:\s+\"[^\"\n]*\")?\s*\)")
REF_DEF = re.compile(r"(?m)^ {0,3}\[[^\]\n]+\]:\s*(<[^>\n]*>|\S+)")
HEADING = re.compile(r"(?m)^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def strip_code(text):
    """The text without fenced blocks, inline code spans and HTML comments (links there are not links)."""
    text = FENCE.sub("", text)
    text = HTML_COMMENT.sub("", text)
    return INLINE_CODE.sub("", text)


def slug(heading):
    """GitHub's anchor for a heading: lower case; letters, digits, '_', '-' and spaces kept; spaces to '-'."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def headings(text):
    return [m.group(2) for m in HEADING.finditer(FENCE.sub("", text))]


def anchors(text):
    seen, out = {}, set()
    for heading in headings(text):
        base = slug(heading)
        n = seen.get(base, 0)
        out.add(base if n == 0 else "%s-%d" % (base, n))
        seen[base] = n + 1
    return out


def link_targets(text):
    body = strip_code(text)
    found = [m.group(1) for m in LINK.finditer(body)] + [m.group(1) for m in REF_DEF.finditer(body)]
    return [t[1:-1] if t.startswith("<") and t.endswith(">") else t for t in found]


def link_problems(paths, root=ROOT):
    root = os.path.realpath(root)
    problems = []
    for path in paths:
        text = read(path)
        name = rel(path)
        for target in link_targets(text):
            if SCHEME.match(target):
                if not target.lower().startswith(("https://", "mailto:")):
                    problems.append("%s: link is not https: %s" % (name, target))
                elif "coach-mcp-for-garmin" in target.lower():
                    problems.append("%s: absolute link to this repository (use a relative link): %s" % (name, target))
                continue
            part, _, anchor = target.partition("#")
            part = urllib.parse.unquote(part)
            if part.startswith("/"):
                problems.append("%s: root-absolute link: %s" % (name, target))
                continue
            dest = os.path.realpath(os.path.join(os.path.dirname(path), part)) if part else os.path.realpath(path)
            if dest != root and not dest.startswith(root + os.sep):
                problems.append("%s: link leaves the repository: %s" % (name, target))
            elif not os.path.exists(dest):
                problems.append("%s: broken link: %s" % (name, target))
            elif anchor:
                if not (dest.endswith(".md") and os.path.isfile(dest)):
                    problems.append("%s: anchor on a target that is not a Markdown file: %s" % (name, target))
                elif anchor not in anchors(read(dest)):
                    problems.append("%s: no heading for anchor: %s" % (name, target))
    return problems


class Docs(unittest.TestCase):
    """The step 1.3 pins, read from the pages the texts moved to."""

    def test_maintenance_says_a_new_owner_secret_leaves_the_static_bearer_alone(self):
        rotation = [p for p in paragraphs(read(doc("maintenance")))
                    if "--rotate-secret" in p and "`MCP_BEARER_TOKEN` is not affected" in flat(p)]
        self.assertTrue(rotation, "no paragraph on --rotate-secret says MCP_BEARER_TOKEN is not affected")

    def test_usage_text_never_puts_a_secret_on_a_command_line(self):
        # V2-4, V2-5: a real value typed in place of "..." in NAME=... would land
        # in the shell history (and with railway, in the process list).
        for name in ("test-signin.py", "test-protocol.py"):
            text = read(os.path.join(ROOT, "scripts", name)).split('"""', 2)[1]
            with self.subTest(script=name):
                self.assertNotRegex(text, r"\b(OWNER_SECRET|MCP_BEARER_TOKEN)=\S")
                self.assertIn("read -rs MCP_BEARER_TOKEN && export MCP_BEARER_TOKEN", text)
        self.assertIn("asks for OWNER_SECRET at a prompt that does not echo",
                      flat(read(os.path.join(ROOT, "scripts", "test-signin.py"))))
        text = read(doc("deploy-railway"))
        settings = [p for p in paragraphs(text) if p.startswith("Sign-in settings")]
        self.assertEqual(len(settings), 1, "docs/deploy-railway.md: want exactly one 'Sign-in settings' paragraph")
        for needed in ("`OWNER_SECRET` and `MCP_BEARER_TOKEN`", "--rotate-secret", "--rotate-token",
                       "never as `NAME=value`"):
            with self.subTest(needed=needed):
                self.assertIn(needed, flat(settings[0]))
        self.assertIn("""read -rs VALUE && printf '%s' "$VALUE" | railway variable set NAME --stdin""", text)

    def test_protocol_check_builds_only_the_python_it_needs(self):
        # UX-V5: ci-local.sh --setup also fetches linters pinned only for Linux x86_64 and
        # macOS arm64, so it exits 1 elsewhere; the protocol check needs only the full venv.
        text = read(doc("protocol"))
        self.assertIn("python3.12 -m venv ~/.cache/coach-mcp-ci/venv-full", text)
        self.assertIn("~/.cache/coach-mcp-ci/venv-full/bin/python -B scripts/ci-checks.py install-full", text)
        commands = "\n".join(re.findall(r"(?s)```bash\n(.*?)```", text))
        self.assertNotIn("ci-local.sh", commands)
        self.assertNotRegex(flat(text), r"`scripts/ci-local\.sh --setup` makes")

    def test_docker_relogin_restarts_the_server(self):
        # UX-V1: entrypoint.sh restarts garmin-mcp after a login only when
        # /data/tokens was empty at start, so a re-login needs a restart.
        self.assertIn('if [ "$had_tokens" = no ]; then', read(os.path.join(ROOT, "entrypoint.sh")))
        for name in DOC_PAGES:
            with self.subTest(page=name):
                self.assertNotIn("picks up the new tokens by itself", flat(read(doc(name))))
        docker = [p for p in paragraphs(read(doc("maintenance"))) if p.startswith("With Docker")]
        self.assertEqual(len(docker), 1, "docs/maintenance.md: want exactly one 'With Docker' paragraph")
        self.assertIn("garmin-mcp-auth --token-path /data/tokens --force-reauth", flat(docker[0]))
        self.assertIn("docker compose restart app", flat(docker[0]))

    def test_key_to_full_switch_handles_old_tokens(self):
        switch = section(read(doc("garmin-login")), "## Switch modes")
        self.assertIsNotNone(switch, "docs/garmin-login.md: no '## Switch modes' section")
        for needed in ("docker compose restart app", 'kill "$(cat /tmp/garmin-mcp.pid)"', "--force-reauth"):
            with self.subTest(needed=needed):
                self.assertIn(needed, flat(switch))

    def test_deploy_stop_wording_matches_the_script_order(self):
        # UX-V4: deploy.sh may run railway init/add before it checks GARMIN_AUTH.
        text = flat(read(doc("deploy-railway")))
        self.assertNotIn("deploy.sh stops before changing anything", text)
        self.assertIn("may already have created the empty project and service", text)
        code = read(DEPLOY)
        self.assertLess(code.index('railway init "${INIT_ARGS[@]}"'), code.index("if [[ -z $AUTH_MODE ]]; then"))

    def test_ext_readme_lists_the_sign_in_modules(self):
        text = read(EXT_README)
        for needed in ("auth_config.py", "auth_store.py", "owner_auth.py", "auth_http.py", "set_asgi_middleware"):
            with self.subTest(needed=needed):
                self.assertTrue(needed in text, "ext/README.md does not mention " + needed)

    def test_ext_readme_documents_tool_errors_and_the_text_filter(self):
        text = read(EXT_README)
        joined = flat(text)
        at = joined.lower().find("error executing tool")
        self.assertEqual(at, -1, "ext/README.md presents the SDK's error text: " + joined[max(at - 80, 0):at + 80])
        self.assertTrue(re.search(r"(?m)^## Tool errors and text from Garmin$", text),
                        "ext/README.md has no section 'Tool errors and text from Garmin'")
        for module in ("text_hygiene.py", "tool_errors.py", "server_instructions.py"):
            with self.subTest(module=module):
                self.assertTrue(re.search(r"(?m)^\| `%s` \|" % re.escape(module), text),
                                "ext/README.md: the file table has no row for " + module)


class DocLinks(unittest.TestCase):
    """T-LINK and T-HEAD: every relative link and anchor resolves; headings stay plain."""

    def test_every_link_and_anchor_resolves(self):
        problems = link_problems(doc_files())
        self.assertEqual(problems, [], "\n".join(problems))

    def test_headings_hold_no_link_or_emphasis(self):
        for path in doc_files():
            for heading in headings(read(path)):
                with self.subTest(file=rel(path), heading=heading):
                    self.assertNotIn("](", heading)
                    self.assertNotRegex(heading, r"(^|\W)_|_(\W|$)|\*")

    def test_the_checker_finds_what_it_must(self):
        # The checker itself, on a throwaway tree: each broken form is reported, the good ones are not.
        with tempfile.TemporaryDirectory(prefix="doclinks-") as tmp:
            os.makedirs(os.path.join(tmp, "docs"))
            with open(os.path.join(tmp, "docs", "b.md"), "w", encoding="utf-8") as f:
                f.write("# B\n\n## Get `get_daily_status` fields\n\n## Dup\n\n## Dup\n")
            page = os.path.join(tmp, "a.md")
            with open(page, "w", encoding="utf-8") as f:
                f.write("[ok](docs/b.md#get-get_daily_status-fields) [ok](docs/b.md#dup-1) [ok](#a)\n"
                        "`[code](missing.md)`\n\n```\n[fence](missing.md)\n```\n<!-- [c](missing.md) -->\n\n"
                        "# A\n\n[x](missing.md) [x](docs/b.md#nope) [x](docs/b.md#dup-2) [x](/abs.md)\n"
                        "[x](http://example.com) [x](https://github.com/someone/coach-mcp-for-garmin)\n"
                        "[x](../../outside.md)\n")
            problems = link_problems([page], root=tmp)
        self.assertEqual(len(problems), 7, "\n".join(problems))
        for word in ("broken link: missing.md", "no heading for anchor: docs/b.md#nope",
                     "no heading for anchor: docs/b.md#dup-2", "root-absolute", "not https",
                     "absolute link to this repository", "leaves the repository"):
            with self.subTest(word=word):
                self.assertTrue(any(word in p for p in problems), word)


class DocIndex(unittest.TestCase):
    """T-INDEX and T-QS: the thirteen pages, the README links, a one-screen quickstart."""

    def test_docs_holds_exactly_the_thirteen_pages_and_readme_links_them(self):
        present = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(DOCS_DIR, "*.md")))
        self.assertEqual(present, sorted(DOC_PAGES))
        targets = {t.partition("#")[0] for t in link_targets(read(README))}
        for needed in ["docs/%s.md" % page for page in DOC_PAGES] + ["ext/README.md", "SECURITY.md",
                                                                     "CHANGELOG.md", "LICENSE"]:
            with self.subTest(link=needed):
                self.assertIn(needed, targets)

    def test_quickstart_fits_one_screen(self):
        text = read(README)
        match = re.search(r"(?ms)^## Quickstart$.*?(?=^## |\Z)", text)
        self.assertIsNotNone(match, "README has no '## Quickstart'")
        lines = match.group(0).rstrip("\n").splitlines()
        self.assertLessEqual(len(lines), 40, "the quickstart has %d lines" % len(lines))
        for needed in ("`full`", "`key`", "`GARMIN_AUTH`", "Railway", "Docker", "Cloudflare Tunnel", "owner secret"):
            with self.subTest(needed=needed):
                self.assertIn(needed, flat(match.group(0)))


# T-PIN: the must-keep texts and the file each lives in (whitespace collapsed, case kept).
PINS = {
    README: ("Not affiliated with or endorsed by Garmin", "`GARMIN_AUTH` is required and has no default",
             "Sign in now", "Register automatically", "with no trailing slash",
             "On the Free plan you can add one custom connector", "only an Owner can add a custom connector",
             "Not medical advice.", "What leaves your server.", "The server itself calls only Garmin",
             "best-effort basis, with no SLA"),
    doc("connect-claude"): ("Sign in now", "Register automatically", "no trailing slash",
                            "Organization settings → Connectors", "claude mcp logout garmin"),
    doc("deploy-railway"): ("Never add a variable named `GARMIN_MCP_REF`",),
    # DEPLOY N8: the Docker guide's commands and warnings.
    doc("deploy-docker"): ("docker compose exec app garmin-mcp-auth --token-path /data/tokens", "http://app:8080",
                           "docker compose down --volumes --rmi all", "Bot Fight Mode", "--loglevel debug",
                           "PUBLIC_URL"),
    doc("maintenance"): ("Moving from the token-in-URL version",
                         "`GARMIN_AUTH` is required (`full` or `key`, no default)"),
    doc("how-it-works"): ("X-Forwarded", "replaces most links and HTML tags with `[link]` and `[html]`",
                          "sends no analytics or telemetry", "`/data/garmin-key/token.json`"),
    doc("daily-status"): ('The calibration recipe then ends with "calibration failed: no thresholds": '
                          "fix the Garmin login and run it again.",),
    doc("tools"): ("Refresh tools list** the same way",),
    doc("troubleshooting"): ("tool error: tool=<name> group=<group>: <original error>",),
    doc("updates"): ("Semantic Versioning", "`RULES_VERSION`", "`get_daily_status`", "`get_weekly_status`",
                     "`GARMIN_AUTH`", "MAJOR", "MINOR", "PATCH", "best-effort basis"),
    SECURITY_MD: ("Report a vulnerability", "not in a public issue", "Never include real tokens", "Out of scope"),
    CHANGELOG: ("`GARMIN_AUTH` is required (`full` or `key`, no default)",),
}
# Lines that must stand on their own.
PIN_LINES = {
    README: ("## One server per person",),
    doc("updates"): ("## The public API",),
    SECURITY_MD: ("## Supported versions", "## Scope"),
}
# DOCS contract T-BAN: texts of the token-only and pre-sign-in versions, the SDK's error text and the
# Railway template placeholder (DEPLOY N8).
BANNED = ("Sign-in is being reworked", "No sign-in", "No sign in", "Request headers", "TEMPLATE_CODE")
MARKER = re.compile(r"TODO\((?:KEY|DEPLOY|LIVE|CI|DOCS)\)")
# CI's text-scan (CI D17): no control, format, separator, private-use or surrogate character but tab and newline.
HIDDEN_CATEGORIES = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs")


class DocPins(unittest.TestCase):
    def test_pinned_texts(self):
        for path, needles in PINS.items():
            text = flat(read(path)) if os.path.isfile(path) else None
            for needle in needles:
                with self.subTest(file=rel(path), needle=needle):
                    self.assertIsNotNone(text, rel(path) + " is missing")
                    self.assertIn(needle, text)

    def test_pinned_lines(self):
        for path, lines in PIN_LINES.items():
            text = read(path) if os.path.isfile(path) else ""
            for line in lines:
                with self.subTest(file=rel(path), line=line):
                    self.assertRegex(text, r"(?m)^%s$" % re.escape(line))


class DocBans(unittest.TestCase):
    """T-BAN, T-MARK, T-STEP, T-PRIV and CI's text-scan over the doc set."""

    def test_no_text_of_older_versions(self):
        for path in doc_files():
            text = read(path)
            with self.subTest(file=rel(path)):
                for gone in BANNED:
                    self.assertNotIn(gone, text)
                self.assertNotIn("error executing tool", flat(text).lower())
                for paragraph in paragraphs(text):
                    if "<token>/mcp" in paragraph:  # only to say the form is gone
                        self.assertIn("410", paragraph)

    def test_no_todo_marker(self):
        for path in doc_files():
            with self.subTest(file=rel(path)):
                self.assertIsNone(MARKER.search(read(path)))

    def test_no_numbered_step_cross_reference(self):
        # Recipes are exempt: the routine prompts number their own steps.
        for path in [README] + sorted(glob.glob(os.path.join(DOCS_DIR, "*.md"))):
            match = re.search(r"(?i).{0,40}\bstep [0-9]\b", read(path)) if os.path.isfile(path) else None
            with self.subTest(file=rel(path)):
                self.assertIsNone(match, match and match.group(0))

    def test_no_personal_data(self):
        for path in doc_files():
            text = read(path)
            with self.subTest(file=rel(path)):
                for match in re.finditer(r"([A-Za-z0-9<>-]+)\.up\.railway\.app", text):
                    self.assertIn(match.group(1), ("<your-app>", "your-app"))
                for match in re.finditer(r"claude\.ai/(?:code/)?artifact/(\S*)", text):
                    self.assertTrue(match.group(1).startswith("<id>"), "an artifact id: " + match.group(0)[:40])
                for match in re.finditer(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
                    self.assertTrue(match.group(0).endswith("@example.com"), "an email address")
                self.assertNotRegex(text, r"/Users/|/home/[a-z]")

    def test_no_hidden_characters(self):
        for path in doc_files():
            for number, line in enumerate(read(path).splitlines(), 1):
                for ch in line:
                    if ch != "\t" and unicodedata.category(ch) in HIDDEN_CATEGORIES:
                        self.fail("%s:%d has U+%04X" % (rel(path), number, ord(ch)))

    def test_key_mode_texts_stay_as_key_mode_words_them(self):
        # KEY's internal cache variable is never offered as a setting (KEY DOCS-K3), and the docs quote
        # "Personal API" capitalized, as Garmin and KEY's texts do (DOCS-K9).
        for path in [README] + sorted(glob.glob(os.path.join(DOCS_DIR, "*.md"))):
            with self.subTest(file=rel(path)):
                self.assertNotIn("GARMIN_KEY_TOKEN_CACHE", read(path))
        for path in doc_files():
            with self.subTest(file=rel(path)):
                self.assertNotIn("personal API key", flat(read(path)))


class DocsMatchConfig(unittest.TestCase):
    """The docs agree with the files they describe."""

    def test_rules_headings_carry_rules_version(self):
        for module, page, title in (("daily_status.py", "daily-status", "Daily"),
                                    ("weekly_status.py", "weekly-status", "Weekly")):
            match = re.search(r"(?m)^RULES_VERSION = (\d+)$", read(os.path.join(HERE, module)))
            with self.subTest(page=page):
                self.assertIsNotNone(match, module + " has no RULES_VERSION line")
                self.assertRegex(read(doc(page)), r"(?m)^## %s status rules v%s$" % (title, match.group(1)))

    def test_tools_table_equals_the_tool_lists(self):
        text = section(read(doc("tools")), "## Tools in each mode")
        self.assertIsNotNone(text, "docs/tools.md has no '## Tools in each mode'")
        rows = re.findall(r"(?m)^\| `([a-z0-9_]+)` \| [^|\n]+ \| (yes|no) \| (yes|no) \|$", text)
        names = [row[0] for row in rows]
        self.assertEqual(len(names), len(set(names)), "a tool has two rows")
        full, key = tool_names(TOOLS_FULL), key_mode_tools()
        self.assertIsNotNone(key, "no key-mode list: config/enabled-tools-key.txt or KEY_MODE_TOOLS in ext/key_mode.py")
        self.assertEqual({row[0] for row in rows if row[1] == "yes"}, full)
        self.assertEqual({row[0] for row in rows if row[2] == "yes"}, key)
        self.assertEqual(set(names), full | key)
        # The mode texts name VO2max exactly when key mode has get_vo2max_trend (KEY Q4).
        for path, start in ((README, "| `key` |"), (doc("garmin-login"), "| Tools |")):
            found = [line for line in read(path).splitlines() if line.strip().startswith(start)]
            with self.subTest(file=rel(path), row=start):
                self.assertEqual(len(found), 1)
                self.assertEqual("VO2max" in found[0], "get_vo2max_trend" in key)

    def test_every_setting_in_env_example_is_documented(self):
        names = set(re.findall(r"(?m)^#?\s*([A-Z][A-Z0-9_]{2,})=", read(ENV_EXAMPLE)))
        documented = read(README) + "".join(read(p) for p in glob.glob(os.path.join(DOCS_DIR, "*.md")))
        # Internal: set by the image or entrypoint.sh, never by the user.
        for name in sorted(names - {"GARMINTOKENS", "GARMIN_MCP_STATELESS_HTTP", "PORT"}):
            with self.subTest(name=name):
                self.assertIn("`%s`" % name, documented)

    def test_security_names_the_advisory_while_the_pin_has_it(self):
        match = re.search(r"(?m)^garminconnect==(\d+)\.(\d+)\.(\d+)\s*$", read(CONSTRAINTS))
        affected = bool(match) and tuple(int(x) for x in match.groups()) < (0, 3, 5)
        self.assertEqual("PYSEC-2026-3467" in read(SECURITY_MD), affected)

    def test_template_section_names_the_template_variables(self):
        # From 1.0.1 on, once docs/deploy-railway.md has the template section (DEPLOY N6, docs half).
        text = section(read(doc("deploy-railway")), "## Deploy with the Railway template")
        if text is None:
            return
        self.assertEqual(set(re.findall(r"`([A-Z][A-Z0-9_]+)`", text)),
                         {"OWNER_SECRET", "GARMIN_AUTH", "REPORT_TZ", "GARMIN_PERSONAL_API_KEY"})

    def test_no_railway_template_named_before_the_docs_have_it(self):
        # Until docs/deploy-railway.md has the template section, deploy.sh is the only Railway path:
        # no setup text or startup error may send a user to a template.
        if section(read(doc("deploy-railway")), "## Deploy with the Railway template") is not None:
            return
        for path in (ENV_EXAMPLE, DEPLOY, os.path.join(HERE, "auth_config.py"), os.path.join(ROOT, "entrypoint.sh"),
                     README, doc("deploy-railway"), doc("deploy-docker"), doc("troubleshooting")):
            with self.subTest(file=rel(path)):
                self.assertNotRegex(read(path), r"(?i)\btemplate\b")

    def test_ext_readme_has_a_row_for_every_module(self):
        text = read(EXT_README)
        self.assertNotIn("stays the mcp version", flat(text))
        for path in sorted(glob.glob(os.path.join(HERE, "*.py"))):
            name = os.path.basename(path)
            if name.startswith("test_"):
                continue
            with self.subTest(module=name):
                self.assertRegex(text, r"(?m)^\| `%s` \|" % re.escape(name))


PATH_RE = re.compile(r"(?<![\w./-])((?:scripts|ext|config|recipes|docs)/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)")


class DocPaths(unittest.TestCase):
    """T-PATH and T-CODEREF: the repository paths the docs and the code name exist."""

    def test_paths_named_in_the_docs_exist(self):
        for path in doc_files():
            text = read(path)
            for match in PATH_RE.finditer(text):
                name = match.group(1).rstrip(".")
                if text[match.end():match.end() + 1] in ("*", "<") or name.endswith(".local.md"):
                    continue  # a glob or a placeholder
                with self.subTest(file=rel(path), path=name):
                    self.assertTrue(os.path.exists(os.path.join(ROOT, name)))

    def test_code_cites_docs_pages_not_readme_steps(self):
        files = [os.path.join(ROOT, name) for name in ("Caddyfile", "Dockerfile", "entrypoint.sh", ".env.example",
                                                       "compose.yaml", "railway.json", "railway.toml")]
        files += glob.glob(os.path.join(ROOT, "scripts", "*.sh")) + glob.glob(os.path.join(ROOT, "scripts", "*.py"))
        files += [p for p in glob.glob(os.path.join(HERE, "*.py")) if not os.path.basename(p).startswith("test_")]
        files += glob.glob(os.path.join(ROOT, "config", "*"))
        for path in sorted(p for p in files if os.path.isfile(p)):
            text = read(path)
            with self.subTest(file=rel(path)):
                self.assertNotRegex(text, r'README step \d|\(README, Security\)|README "Daily status rules')
                for name in re.findall(r"docs/[\w-]+\.md", text):
                    self.assertTrue(os.path.isfile(os.path.join(ROOT, name)), name)


CHANGELOG_TYPES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")


def code_version():
    """VERSION of ext/version.py, read with ast (never imported), as a tuple of three ints."""
    found = [node.value for node in ast.parse(read(VERSION_PY)).body
             if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["VERSION"]]
    if len(found) != 1 or not isinstance(found[0], ast.Constant) or not isinstance(found[0].value, str):
        raise AssertionError("ext/version.py: want exactly one VERSION = \"X.Y.Z\"")
    if not re.match(r"^\d+\.\d+\.\d+$", found[0].value):
        raise AssertionError("ext/version.py: VERSION is not X.Y.Z")
    return tuple(int(x) for x in found[0].value.split("."))


def changelog_problems(text):
    """T-CL: Keep a Changelog 1.1.0 as this project writes it (DOCS contract D7)."""
    import datetime

    problems = []
    intro = text.split("\n## ", 1)[0]
    for link in ("https://keepachangelog.com/en/1.1.0/", "https://semver.org/spec/v2.0.0.html"):
        if link not in intro:
            problems.append("the intro does not link " + link)
    h2 = re.findall(r"(?m)^## (.*)$", text)
    if not h2 or h2[0] != "[Unreleased]":
        problems.append("the first '## ' line is not '## [Unreleased]'")
    previous = None
    for heading in h2[1:]:
        match = re.match(r"^\[(\d+)\.(\d+)\.(\d+)\] - (\d{4}-\d{2}-\d{2})$", heading)
        if match is None:
            problems.append("not a release heading: ## " + heading)
            continue
        try:
            day = datetime.date.fromisoformat(match.group(4))
        except ValueError:
            problems.append("not a real date: ## " + heading)
            continue
        version = tuple(int(x) for x in match.groups()[:3])
        if previous and not (version < previous[0] and day <= previous[1]):
            problems.append("versions must descend and dates must not rise: ## " + heading)
        previous = (version, day)
    for heading in re.findall(r"(?m)^### (.*)$", text):
        if heading not in CHANGELOG_TYPES:
            problems.append("not a Keep a Changelog type: ### " + heading)
    if re.search(r"(?m)^ {0,3}\[[^\]\n]+\]:\s", text):
        problems.append("link reference definitions (D7: none)")
    return problems


def version_problems(text, version):
    """T-VER: the code version is at least the newest dated release, and while it is ahead (or nothing is
    dated yet) [Unreleased] says what is new."""
    dated = re.findall(r"(?m)^## \[(\d+)\.(\d+)\.(\d+)\] - \d{4}-\d{2}-\d{2}$", text)
    newest = tuple(int(x) for x in dated[0]) if dated else None
    unreleased = (section(text, "## [Unreleased]") or "")
    if newest and version < newest:
        return ["ext/version.py is older than the newest CHANGELOG release"]
    if (newest is None or version > newest) and not re.search(r"(?m)^- ", unreleased):
        return ["ext/version.py is ahead of the CHANGELOG, and [Unreleased] lists nothing"]
    return []


class ChangelogFile(unittest.TestCase):
    def test_format(self):
        problems = changelog_problems(read(CHANGELOG))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_code_version_agrees(self):
        problems = version_problems(read(CHANGELOG), code_version())
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_release_step_keeps_both_checks_green(self):
        # The release step inserts the dated heading under [Unreleased] (DOCS contract 4.6, LIVE R2).
        text = read(CHANGELOG).replace("## [Unreleased]\n", "## [Unreleased]\n\n## [%s] - 2026-11-18\n"
                                       % ".".join(map(str, code_version())), 1)
        self.assertEqual(changelog_problems(text) + version_problems(text, code_version()), [])

    def test_the_checks_catch_bad_headings(self):
        good = read(CHANGELOG)
        for bad in (good.replace("## [Unreleased]", "## Unreleased", 1),
                    good.replace("### Added", "### New", 1),
                    good + "\n## [1.0.0] - 2026-02-30\n",
                    good + "\n## [0.9.0] - 2026-11-01\n\n## [0.9.1] - 2026-10-01\n",
                    good + "\n[1.0.0]: https://example.com\n"):
            with self.subTest(bad=bad[-60:]):
                self.assertTrue(changelog_problems(bad))
        self.assertTrue(version_problems(good + "\n## [9.0.0] - 2026-11-01\n", (1, 0, 0)))
        self.assertTrue(version_problems("## [Unreleased]\n\n## [0.9.0] - 2026-11-01\n", (1, 0, 0)))


def service_lines(code, name):
    """The stripped code lines of one compose service (indented under `  <name>:`)."""
    out, inside, indent = [], False, 0
    for line in code:
        width = len(line) - len(line.lstrip())
        if re.match(r"^\s+%s:\s*$" % re.escape(name), line) and not inside:
            inside, indent = True, width
            continue
        if inside:
            if width <= indent:
                break
            out.append(line.strip())
    return out


class ComposeFile(unittest.TestCase):
    """DEPLOY N1: compose.yaml publishes no port, keeps the tunnel token in the tunnel, and checks /healthz."""

    def setUp(self):
        self.code = code_lines(read(COMPOSE))
        self.stripped = [line.strip() for line in self.code]

    def test_no_port_is_published(self):
        for line in self.stripped:
            with self.subTest(line=line):
                self.assertFalse(line.startswith("ports:"))
                self.assertNotIn("network_mode", line)

    def test_env_files_and_fixed_values(self):
        self.assertIn("env_file: .env", service_lines(self.code, "app"))
        self.assertIn("env_file: tunnel.env", service_lines(self.code, "tunnel"))
        for line in self.code:
            self.assertNotIn("TUNNEL_TOKEN", line)
        self.assertIn('PORT: "8080"', self.stripped)
        self.assertIn("EDGE_CLIENT_IP_HEADER: CF-Connecting-IP", self.stripped)

    def test_health_check_and_volume(self):
        (test,) = [line for line in self.stripped if line.startswith("test:")]
        self.assertIn("/healthz", test)
        self.assertNotIn("/readyz", test)
        self.assertIn("- coach-data:/data", self.stripped)
        self.assertIn("condition: service_healthy", self.stripped)

    def test_cloudflared_is_pinned_and_quiet(self):
        images = [line for line in self.code if re.match(r"^\s*image: cloudflare/cloudflared", line)]
        self.assertEqual(len(images), 1)
        self.assertRegex(images[0], r"^\s*image: cloudflare/cloudflared:\d{4}\.\d+\.\d+@sha256:[0-9a-f]{64}$")
        text = "\n".join(self.code)
        self.assertIn("--no-autoupdate", text)
        self.assertNotIn("--loglevel", text)


def ignore_lines(path):
    return [line.strip() for line in read(path).splitlines() if line.strip() and not line.lstrip().startswith("#")]


def dockerignore_hides(patterns, path):
    """Whether .dockerignore patterns leave `path` (or a folder above it) out of the build context."""
    def regex(pattern):
        out, i = "", 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out, i = out + "(?:.*/)?", i + 3
            elif pattern.startswith("**", i):
                out, i = out + ".*", i + 2
            elif pattern[i] == "*":
                out, i = out + "[^/]*", i + 1
            elif pattern[i] == "?":
                out, i = out + "[^/]", i + 1
            else:
                out, i = out + re.escape(pattern[i]), i + 1
        return re.compile(out)

    parts = path.split("/")
    prefixes = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    hidden = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        compiled = regex(pattern.lstrip("!").strip().strip("/"))
        if any(compiled.fullmatch(prefix) for prefix in prefixes):
            hidden = not negate
    return hidden


class IgnoreFiles(unittest.TestCase):
    """DEPLOY N7: tunnel tokens and Garmin credentials stay out of git and the image; key mode ships."""

    def test_dockerignore(self):
        lines = ignore_lines(DOCKERIGNORE)
        for needed in ("compose.yaml", "tunnel.env", "**/*.env", "**/*garmin_tokens.base64*", "**/garmin-key/"):
            with self.subTest(needed=needed):
                self.assertIn(needed, lines)
        self.assertTrue(dockerignore_hides(lines, "compose.yaml"))
        self.assertTrue(dockerignore_hides(lines, "data/garmin-key/token.json"))
        for shipped in ("ext/key_mode.py", "ext/version.py"):
            with self.subTest(shipped=shipped):
                self.assertFalse(dockerignore_hides(lines, shipped))

    def test_gitignore(self):
        lines = ignore_lines(GITIGNORE)
        for needed in ("tunnel.env", "*garmin_tokens.base64*", "garmin-key/"):
            with self.subTest(needed=needed):
                self.assertIn(needed, lines)


if __name__ == "__main__":
    unittest.main()
