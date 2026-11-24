#!/usr/bin/env python3
"""Staging helper for maintainers: checks a throwaway Railway deployment of
coach-mcp-for-garmin without showing any secret.

    python3 scripts/live-check.py guard [--allow-unlinked]
    python3 scripts/live-check.py url
    python3 scripts/live-check.py run [--fill NAME=FILE:KEY] -- <command> [args...]
    python3 scripts/live-check.py logs [--since 60m] [--save FILE]
    python3 scripts/live-check.py deploys [--wait]
    python3 scripts/live-check.py healthcheck
    python3 scripts/live-check.py metrics [--since 3h] [--save FILE]
    python3 scripts/live-check.py ssh cache-modes|token-copies|wipe-garmin
    python3 scripts/live-check.py probe-ip
    python3 scripts/live-check.py data-shape FILE [FILE...] [--keep]
        [--keys-out SHAPES] [--compare SHAPES] [--require-key LABEL:KEY]

It works only on the throwaway Railway projects named in LIVE_CHECK_PROJECTS
(a comma list; default coach-mcp-test), as linked in --dir (default: this
repository). Before any Railway command it reads `railway status --json` and
stops unless the linked project has that name, and `run` refuses a command
that names any other host. Never list your real server's project there. It prints
counts, check results and fixed names only: never a secret value, an IP
address, a client ID, a user agent or Garmin data.
Standard library only, so any python3 3.10 or newer runs it.

Common options (before or after the subcommand): --dir PATH (the folder whose
Railway link is used) and --project NAME (one of LIVE_CHECK_PROJECTS; default
the first). Exit codes: 0 pass, 1 a check failed, 2 refused
or usage error.
"""

import argparse
import ast
import datetime
import fnmatch
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_ENV = "LIVE_CHECK_PROJECTS"  # the throwaway projects this script may touch
DEFAULT_PROJECTS = ("coach-mcp-test",)
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ELLIPSIS = chr(0x2026)  # ext/text_hygiene.py's cut mark
MARKS = ("[link]", "[image]", "[html]")  # ext/text_hygiene.py's replacements

SECRET_NAMES = ("OWNER_SECRET", "MCP_BEARER_TOKEN", "GARMIN_PERSONAL_API_KEY")
SECRET_SUFFIXES = ("_SECRET", "_PASSWORD", "_API_KEY", "_BEARER_TOKEN")
MIN_SECRET_LEN = 8
REDACTED = "[redacted]"

AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic", "private_key_jwt")
GRANT_TYPES = ("authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer",
               "client_credentials")
SCOPES = ("offline_access",)
REDIRECT_HOSTS = ("https://claude.ai", "https://claude.com", "http://localhost", "http://127.0.0.1")
GROUPS = ("login", "bad_input", "rate_limited", "unavailable", "no_data", "internal")
KEY_GROUPS = ("login", "rate_limited", "bad_input", "unavailable", "no_data", "internal")
REVOKE_REASONS = (("revoked by client", "client"), ("refresh token reuse", "refresh reuse"),
                  ("authorization code reuse", "code reuse"), ("owner secret changed", "secret changed"),
                  ("client evicted", "evicted"))
DEPLOY_STATUSES = ("SUCCESS", "FAILED", "CRASHED", "REMOVED", "BUILDING", "DEPLOYING", "INITIALIZING",
                   "QUEUED", "WAITING", "SLEEPING", "SKIPPED", "REMOVING")
DEPLOY_DONE_BAD = ("FAILED", "CRASHED", "REMOVED")

NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,64}$")
KNOWN_CLIENT_NAMES = frozenset(("claude", "claude code", "claude-code", "claude desktop", "claude.ai",
                                "claude for chrome", "mcp inspector", "mcp-inspector", "test-signin"))
CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,40}$")
URL_IN_ARG_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#'\"]+")
# http:host, https:/host, https:///host or https:\\host: curl may still take a host from them,
# so an http(s) scheme not followed by "//" and a host is refused outright.
LOOSE_URL_RE = re.compile(r"(?i)(?<![A-Za-z0-9+.-])https?:(?!//[^\s/?#'\"\\])")
NETLOC_RE = re.compile(r"^[A-Za-z0-9.\-]+(:[0-9]{1,5})?$|^\[[0-9A-Fa-f:.]+\](:[0-9]{1,5})?$")
PLACEHOLDER_RE = re.compile(r"@[A-Za-z_][A-Za-z0-9_]*@")
DIGITS_RE = re.compile(r"^[0-9]{1,20}$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
QUERY_NAME_RE = re.compile(r"^[a-z_]{1,32}$")
FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
FOLDER_RE = re.compile(r"^/[A-Za-z0-9_./-]{0,200}$")
MODE_RE = re.compile(r"^[0-7]{3,4}$")
IPV4_LIKE_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")
TOKEN_LIKE_RE = re.compile(r"[A-Za-z0-9_-]{16,}")
LEVEL_PREFIX = r"^\s*(?:(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\b[:\s]*(?:[\w.]+:\s*)?)?"

DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
                                              "2001:db8::/32")]
PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                  "100.64.0.0/10", "fc00::/7", "fe80::/10", "169.254.0.0/16")]
LOOPBACK_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128")]
ADDRESS_CLASSES = ("public", "documentation", "private", "loopback")

PLANTED = (("203.0.113.9", "X-Real-IP"), ("203.0.113.10", "X-Forwarded-For"), ("203.0.113.11", "CF-Connecting-IP"))

TOKEN_PATTERNS = ("garmin_tokens*", ".garminconnect*", "*garminconnect_base64*")
TOKEN_PATH_PATTERN = "*garmin*token.json"

PRICE_GB_RAM = 10.0
PRICE_VCPU = 20.0
PRICE_GB_VOLUME = 0.15


class Refused(Exception):
    """Exit 2 after printing the message."""


class Failed(Exception):
    """Exit 1 after printing the message."""


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "")) if os.environ.get(name) else default
    except ValueError:
        return default


def timeout_for(default):
    """Railway call timeout; tests shorten it with LIVE_CHECK_TEST_TIMEOUT."""
    return _env_float("LIVE_CHECK_TEST_TIMEOUT", default)


# ---------------------------------------------------------------- railway calls

class Railway:
    """Runs the `railway` CLI in one folder. Every call after the first needs a passed guard."""

    def __init__(self, folder, project):
        self.folder = folder
        self.project = project
        self.guarded = False

    def call(self, args, timeout=60, need_guard=True):
        """(exit code, stdout, stderr); raises Failed with a reason class on a missing CLI or a timeout."""
        if need_guard and not self.guarded:
            raise Refused("live-check: internal error: a Railway command before the guard. Nothing was run.")
        exe = shutil.which("railway")
        if exe is None:
            raise Failed("CLI missing")
        try:
            proc = subprocess.run([exe] + list(args), cwd=self.folder, capture_output=True, text=True,
                                  timeout=timeout_for(timeout), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise Failed("timeout") from None
        except OSError:
            raise Failed("CLI did not start") from None
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def guard(self, allow_unlinked=False, quiet=False):
        """Prints PASS/FAIL. Returns True when linked to the expected project (or unlinked and allowed)."""
        def say(line):
            if not quiet or not line.startswith("PASS"):
                print(line)
        try:
            code, out, err = self.call(["status", "--json"], timeout=30, need_guard=False)
        except Failed as e:
            say(f"FAIL guard: cannot confirm the linked project ({e}). Nothing was run.")
            return False
        if "no linked project" in (out + "\n" + err).lower():
            if allow_unlinked:
                say("PASS guard: this folder is not linked to any Railway project (first deploy)")
                return True
            say("FAIL guard: this folder is not linked to a Railway project. Nothing was run.")
            return False
        if code != 0:
            say(f"FAIL guard: cannot confirm the linked project (railway exit {code}). Nothing was run.")
            return False
        try:
            data = json.loads(out)
        except ValueError:
            say("FAIL guard: cannot confirm the linked project (not JSON). Nothing was run.")
            return False
        name = project_name(data)
        if name is None:
            say("FAIL guard: cannot confirm the linked project (no project name). Nothing was run.")
            return False
        if name != self.project:
            shown = name if NAME_RE.match(name) else "another project"
            say(f'FAIL guard: this folder is linked to project "{shown}", not {self.project}. Nothing was run.')
            return False
        self.guarded = True
        say(f"PASS guard: linked to Railway project {name}")
        return True

    def variables(self):
        """{NAME: value} from `railway variable list --kv`; raises Failed when not readable."""
        code, out, _ = self.call(["variable", "list", "--kv"], timeout=60)
        if code != 0:
            raise Failed(f"railway exit {code}")
        found = {}
        for line in out.splitlines():
            name, sep, value = line.partition("=")
            if sep and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                found[name] = value
        return found


def project_name(data):
    """The first string at name, project.name or projectName."""
    if not isinstance(data, dict):
        return None
    for path in (("name",), ("project", "name"), ("projectName",)):
        node = data
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, str) and node:
            return node
    return None


def require_guard(rw):
    if not rw.guard(quiet=False):
        raise SystemExit(2)


def public_url(variables):
    url = (variables.get("PUBLIC_URL") or "").strip()
    if url:
        return url.rstrip("/")
    domain = (variables.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if domain:
        return "https://" + domain.strip("/")
    return None


def secret_values(variables):
    """{name: value} for the secret variables whose value is long enough to search for."""
    out = {}
    for name, value in variables.items():
        if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES):
            if len(value) >= MIN_SECRET_LEN:
                out[name] = value
    return out


def secret_forms(value):
    forms = {value, urllib.parse.quote(value, safe=""), json.dumps(value)[1:-1]}
    return sorted((f for f in forms if f), key=len, reverse=True)


class Redactor:
    def __init__(self, values):
        forms = set()
        for v in values:
            forms.update(secret_forms(v))
        self.forms = sorted(forms, key=len, reverse=True)

    def __call__(self, text):
        for f in self.forms:
            if f in text:
                text = text.replace(f, REDACTED)
        return text


# ---------------------------------------------------------------- guard, url

def cmd_guard(rw, args):
    return 0 if rw.guard(allow_unlinked=args.allow_unlinked) else 2


def read_variables_or_exit(rw, what):
    try:
        return rw.variables()
    except Failed as e:
        print(f"FAIL {what}: cannot read the Railway variables ({e}). Nothing was run.")
        raise SystemExit(2) from None


def cmd_url(rw, args):
    require_guard(rw)
    url = public_url(read_variables_or_exit(rw, "url"))
    if url is None:
        print("FAIL url: neither PUBLIC_URL nor RAILWAY_PUBLIC_DOMAIN is set on Railway")
        return 2
    print(url)
    return 0


# ---------------------------------------------------------------- run

def hosts_ok(argv, allowed_host):
    """True when every URL in argv names allowed_host (case-insensitive)."""
    for arg in argv:
        if LOOSE_URL_RE.search(arg):
            return False
        for m in URL_IN_ARG_RE.finditer(arg):
            netloc = m.group(0).split("://", 1)[1]
            if "@" in netloc or not NETLOC_RE.match(netloc):
                return False
            try:
                host = urllib.parse.urlsplit(m.group(0)).hostname
            except ValueError:
                return False
            if not host or allowed_host is None or host.lower() != allowed_host.lower():
                return False
    return True


def find_digits(node, key):
    """The first value of `key` (depth first) that is an integer or a 1-20 digit string, as text."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                if isinstance(v, int) and not isinstance(v, bool) and v >= 0 and DIGITS_RE.match(str(v)):
                    return str(v)
                if isinstance(v, str) and DIGITS_RE.match(v):
                    return v
            found = find_digits(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = find_digits(v, key)
            if found is not None:
                return found
    return None


def parse_fill(spec):
    name, sep, rest = spec.partition("=")
    path, sep2, key = rest.rpartition(":")
    if not sep or not sep2 or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name) or not path or not key:
        raise Refused(f"FAIL run: --fill must be NAME=FILE:KEY (got {len(spec)} characters). Nothing was run.")
    return name, path, key


def fill_value(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    value = find_digits(data, key) if data is not None else None
    if value is None:
        raise Refused(f"FAIL run: no {key} with digits in {os.path.basename(path)} (not shown). Nothing was run.")
    return value


def _pump(stream, out, redact):
    for line in iter(stream.readline, ""):
        out.write(redact(line))
        out.flush()
    stream.close()


def cmd_run(rw, args):
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("FAIL run: no command after --. Nothing was run.")
        return 2
    fills = [parse_fill(s) for s in args.fill or []]
    require_guard(rw)
    variables = read_variables_or_exit(rw, "run")
    token = variables.get("MCP_BEARER_TOKEN", "")
    if not token:
        print(f"FAIL run: no MCP_BEARER_TOKEN on Railway. Create one: DEPLOY_SERVICE={rw.project} "
              "./scripts/deploy.sh --rotate-token")
        return 2
    url = public_url(variables)
    allowed = urllib.parse.urlsplit(url).hostname if url else None
    if not hosts_ok(command, allowed):
        print(f"FAIL run: the command names a host that is not {rw.project}'s URL (not shown). Nothing was run.")
        return 2
    filled = []
    for name, path, key in fills:
        value = fill_value(path, key)
        filled.append(value)
        command = [a.replace(f"@{name}@", value) for a in command]
    for a in command:
        m = PLACEHOLDER_RE.search(a)
        if m:
            print(f"FAIL run: {m.group(0)} was not filled. Nothing was run.")
            return 2
    secrets_ = secret_values(variables)
    redact = Redactor(list(secrets_.values()) + [token] + filled)
    env = {k: v for k, v in os.environ.items()
           if k not in ("OWNER_SECRET", "MCP_BEARER_TOKEN") and not k.startswith("GARMIN_")}
    env["MCP_BEARER_TOKEN"] = token
    env["PYTHONUNBUFFERED"] = "1"
    n = len(set(secrets_.values()) | {token})
    print(f"INFO run: MCP_BEARER_TOKEN from Railway is in the command's environment (not shown); "
          f"output filtered for {n} secret values", flush=True)
    try:
        proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
    except OSError:
        print("FAIL run: the command could not be started. Nothing was run.")
        return 2
    threads = [threading.Thread(target=_pump, args=(proc.stdout, sys.stdout, redact)),
               threading.Thread(target=_pump, args=(proc.stderr, sys.stderr, redact))]
    for t in threads:
        t.start()
    code = proc.wait()
    for t in threads:
        t.join()
    print(f"INFO run: command exit {code}", flush=True)
    return code if 0 <= code < 256 else 1


# ---------------------------------------------------------------- logs

def address_class(text):
    try:
        ip = ipaddress.ip_address(str(text).strip().strip("[]"))
    except ValueError:
        return "other"
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if any(ip in n for n in LOOPBACK_NETS if n.version == ip.version):
        return "loopback"
    if any(ip in n for n in DOC_NETS if n.version == ip.version):
        return "documentation"
    if any(ip in n for n in PRIVATE_NETS if n.version == ip.version):
        return "private"
    return "public"


def literal(text):
    try:
        return ast.literal_eval(text.strip())
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def shown_client_name(value):
    """A DCR client_name is caller text: shown only when it is a known client's name."""
    if value is None:
        return "none"
    if (isinstance(value, str) and CLIENT_NAME_RE.match(value) and not IPV4_LIKE_RE.search(value)
            and not TOKEN_LIKE_RE.search(value) and value.lower() in KNOWN_CLIENT_NAMES):
        return value
    return "other"


def redirect_origin(origin):
    """An allowlisted redirect origin name for a scheme://host[:port] text."""
    if not isinstance(origin, str):
        return "other"
    try:
        parts = urllib.parse.urlsplit(origin)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return "other"
    name = f"{parts.scheme.lower()}://{host}"
    if name not in REDIRECT_HOSTS:
        return "other"
    return name + (" (port given)" if port is not None else "")


def authorize_host(text):
    host = text.strip().lower()
    base = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return {"claude.ai": "https://claude.ai", "claude.com": "https://claude.com", "localhost": "http://localhost",
            "127.0.0.1": "http://127.0.0.1"}.get(base, "other")


def counted(counter, names=None, zero_ok=True):
    """'a 1, b 2' (names first in their order, then the rest sorted), or '0'."""
    items = []
    keys = list(names or []) + sorted(k for k in counter if not names or k not in names)
    for k in keys:
        v = counter.get(k, 0)
        if v or (names and k in names and zero_ok):
            items.append(f"{k} {v}")
    return ", ".join(items) if items else "0"


def bump(counter, key, n=1):
    counter[key] = counter.get(key, 0) + n


def load_tool_names():
    names = set()
    try:
        with open(os.path.join(ROOT, "config", "enabled-tools.txt"), encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if re.match(r"^[a-z_][a-z0-9_]{0,63}$", line):
                    names.add(line)
    except OSError:
        pass
    return names


def _rx(body):
    return re.compile(LEVEL_PREFIX + body)


RX = {
    "dcr_request": _rx(r"dcr: request client_name=(.*) auth_method=(.*) grant_types=(.*) scope=(.*) "
                       r"redirect_hosts=(.*) dropped=(.*) rewritten=(.*)$"),
    "dcr_registered": _rx(r"dcr: registered client "),
    "authorize": _rx(r"authorize: client \S+, redirect host (\S+)"),
    "consent_approved": _rx(r"consent: approved for client "),
    "consent_denied": _rx(r"consent: denied for client "),
    "consent_wrong": _rx(r"consent: wrong owner secret \("),
    "code_exchanged": _rx(r"token: code exchanged for client \S+ \(auth method (\S+?)\)"),
    "refresh": _rx(r"token: refresh for client "),
    "reused": _rx(r"token: refresh token reused within "),
    "revoked": _rx(r"token: grant revoked for client \S+ \((.*)\)\s*$"),
    "secret_changed": _rx(r"sign-in: the owner secret changed; every sign-in was revoked"),
    "signin_on": _rx(r"sign-in: on, issuer \S+, static bearer (on|off)\b"),
    "tool_error": _rx(r"tool error: (?:ref=\S+ )?tool=(\S+) group=([A-Za-z_]+):"),
    "rate_limit": _rx(r"rate limit: \S+ \S+ \(\S+\) from (\S+), retry in "),
}
KEY_RX = {
    "on": re.compile(LEVEL_PREFIX + r"garmin key mode: on\b", re.I),
    "new": re.compile(LEVEL_PREFIX + r"garmin key mode: new access token", re.I),
    "cache": re.compile(LEVEL_PREFIX + r"garmin key mode: access token from the cache", re.I),
    "refused401": re.compile(LEVEL_PREFIX + r"garmin key mode: garmin refused the access token \(http 401\)", re.I),
    "expiry": re.compile(LEVEL_PREFIX + r"garmin key mode: the personal api key expires at", re.I),
    "key_refused": re.compile(LEVEL_PREFIX + r"garmin key mode: garmin refused the personal api key "
                              r"\(http ([0-9]{3})\)", re.I),
    "first_failed": re.compile(LEVEL_PREFIX + r"garmin key mode: the first token request failed \(([a-z_]+)\)",
                               re.I),
    "later_failed": re.compile(LEVEL_PREFIX + r"garmin key mode: token request failed \(([a-z_]+)\)", re.I),
    "mode_fixed": re.compile(LEVEL_PREFIX + r"garmin key mode: token cache file mode fixed to 0600", re.I),
}
APP_ACCESS_RE = re.compile(r'"(?:GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH) (\S+) HTTP/[0-9.]+"\s+[0-9]{3}')


def log_entries(text):
    """[(message text, line object or None)] from `railway logs --json` output."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            msg = obj.get("message")
            out.append((msg if isinstance(msg, str) else "", obj))
        else:
            out.append((line, None))
    return out


def caddy_entry(message, obj):
    """The Caddy access-log record in a log line, or None."""
    candidates = []
    if isinstance(obj, dict):
        candidates.append(obj)
        if isinstance(obj.get("attributes"), dict):
            candidates.append(obj["attributes"])
    if message.lstrip().startswith("{"):
        try:
            parsed = json.loads(message)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            candidates.append(parsed)
    for c in candidates:
        logger = c.get("logger")
        if isinstance(logger, str) and logger.startswith("http.log.access"):
            return c
    return None


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def is_railway_health_check(req, uri, address_cls):
    """Railway's deploy health check (deploy.healthcheckPath): GET /healthz sent
    from inside Railway's network straight to the container, so Caddy has no
    edge header and its client_ip is the TCP peer (seen on Railway: from
    100.64.0.0/10). A caller's request through the edge never looks like
    this, and a broken client_ip would show on every other path too."""
    return (address_cls in ("private", "loopback") and req.get("method") == "GET" and uri == "/healthz"
            and req.get("client_ip") == req.get("remote_ip"))


def scan_secrets(text, values):
    """Names of the secrets found in text (any of the three forms)."""
    found = []
    for name, value in values.items():
        if any(f in text for f in secret_forms(value)):
            found.append(name)
    return found


def summarize_logs(deploy_text, http_text, secrets_, since, tools):
    """(lines, failed) for the logs subcommand. Never puts raw log text in a line."""
    c = {k: {} for k in ("client_name", "auth_method", "grants", "scope", "dcr_redirect", "authorize_hosts",
                          "exchange_methods", "revoke_reasons", "rate_classes", "caddy_classes")}
    n = dict.fromkeys(("dcr_request", "dcr_registered", "authorize", "consent_approved", "consent_denied",
                       "consent_wrong", "code_exchanged", "refresh", "reused", "revoked", "secret_changed",
                       "rate_limit", "caddy", "caddy_health", "caddy_redacted_authorize", "caddy_other_query",
                       "location",
                       "req_headers", "app", "app_query", "app_bad_query"), 0)
    bearer = set()
    tool_errors = {g: {} for g in GROUPS}
    other_group = 0
    key = dict.fromkeys(KEY_RX, 0)
    key_codes, first_groups, later_groups = {}, {}, {}
    failed = []

    for message, obj in log_entries(deploy_text):
        caddy = caddy_entry(message, obj)
        if caddy is not None:
            n["caddy"] += 1
            req = _as_dict(caddy.get("request"))
            uri = req.get("uri") if isinstance(req.get("uri"), str) else ""
            address_cls = address_class(req.get("client_ip", ""))
            if is_railway_health_check(req, uri, address_cls):
                n["caddy_health"] += 1
            else:
                bump(c["caddy_classes"], address_cls)
            if "?" in uri:
                path, _, query = uri.partition("?")
                if query == "REDACTED":
                    if path == "/authorize":
                        n["caddy_redacted_authorize"] += 1
                else:
                    n["caddy_other_query"] += 1
            headers = req.get("headers")
            if headers:
                n["req_headers"] += 1
            resp = _as_dict(caddy.get("resp_headers"))
            if any(str(k).lower() == "location" for k in resp):
                n["location"] += 1
            continue
        text = message
        m = RX["dcr_request"].search(text)
        if m:
            n["dcr_request"] += 1
            bump(c["client_name"], shown_client_name(literal(m.group(1))))
            method = literal(m.group(2))
            bump(c["auth_method"], method if method in AUTH_METHODS else ("unset" if method is None else "other"))
            grants = literal(m.group(3))
            for g in grants if isinstance(grants, list) else ([] if grants is None else [grants]):
                bump(c["grants"], g if g in GRANT_TYPES else "other")
            scope = literal(m.group(4))
            if scope is None or scope == "":
                bump(c["scope"], "none")
            else:
                for s in str(scope).split():
                    bump(c["scope"], s if s in SCOPES else "other")
            hosts = literal(m.group(5))
            for h in hosts if isinstance(hosts, list) else []:
                bump(c["dcr_redirect"], redirect_origin(h))
            continue
        if RX["dcr_registered"].search(text):
            n["dcr_registered"] += 1
            continue
        m = RX["authorize"].search(text)
        if m:
            n["authorize"] += 1
            bump(c["authorize_hosts"], authorize_host(m.group(1)))
            continue
        for k in ("consent_approved", "consent_denied", "consent_wrong", "refresh", "reused", "secret_changed"):
            if RX[k].search(text):
                n[k] += 1
                break
        else:
            m = RX["code_exchanged"].search(text)
            if m:
                n["code_exchanged"] += 1
                bump(c["exchange_methods"], m.group(1) if m.group(1) in AUTH_METHODS else "other")
                continue
            m = RX["revoked"].search(text)
            if m:
                n["revoked"] += 1
                reason = dict(REVOKE_REASONS).get(m.group(1).strip(), "other")
                bump(c["revoke_reasons"], reason)
                continue
            m = RX["signin_on"].search(text)
            if m:
                bearer.add(m.group(1))
                continue
            m = RX["tool_error"].search(text)
            if m:
                tool, group = m.group(1), m.group(2)
                if group in tool_errors:
                    bump(tool_errors[group], tool if tool in tools else "other")
                else:
                    other_group += 1
                continue
            m = RX["rate_limit"].search(text)
            if m:
                n["rate_limit"] += 1
                bump(c["rate_classes"], address_class(m.group(1)))
                continue
            matched_key = False
            for k, rx in KEY_RX.items():
                km = rx.search(text)
                if km:
                    key[k] += 1
                    matched_key = True
                    if k == "key_refused":
                        bump(key_codes, km.group(1))
                    elif k == "first_failed":
                        g = km.group(1).lower()
                        bump(first_groups, g if g in KEY_GROUPS else "other")
                    elif k == "later_failed":
                        g = km.group(1).lower()
                        bump(later_groups, g if g in KEY_GROUPS else "other")
                    break
            if matched_key:
                continue
            m = APP_ACCESS_RE.search(text)
            if m:
                n["app"] += 1
                path = m.group(1)
                if "?" in path:
                    n["app_query"] += 1
                    if path.partition("?")[2] != "[redacted]":
                        n["app_bad_query"] += 1

    # Railway HTTP log
    http = log_entries(http_text)
    fields, statuses, qnames = set(), {}, {}
    n_http, n_http_query, sensitive = 0, 0, 0
    for _, obj in http:
        if not isinstance(obj, dict):
            continue
        n_http += 1
        for k in obj:
            if isinstance(k, str) and FIELD_RE.match(k):
                fields.add(k)
        status = obj.get("httpStatus", obj.get("status"))
        try:
            bump(statuses, f"{int(status) // 100}xx")
        except (TypeError, ValueError):
            bump(statuses, "other")
        path = obj.get("path")
        if isinstance(path, str) and "?" in path:
            n_http_query += 1
            for name, _ in urllib.parse.parse_qsl(path.partition("?")[2], keep_blank_values=True):
                lname = name.lower()
                if (lname == "code" or lname == "key" or lname.endswith("_key")
                        or any(w in lname for w in ("token", "secret", "password"))):
                    sensitive += 1
                bump(qnames, name if QUERY_NAME_RE.match(name) else "other")

    # leak scan on the raw text
    in_deploy = scan_secrets(deploy_text, secrets_)
    in_http = scan_secrets(http_text, secrets_)

    L = []
    L.append(f"== sign-in (deploy log, since {since})")
    L.append(f"dcr requests: {n['dcr_request']} | client_name: {counted(c['client_name'])} | auth_method: "
             f"{counted(c['auth_method'])} | grant_types: "
             f"{counted(c['grants'], ['authorization_code', 'refresh_token', 'other'])} | scope: "
             f"{counted(c['scope'])} | redirect hosts: {counted(c['dcr_redirect'])}")
    L.append(f"dcr registered: {n['dcr_registered']} | authorize: {n['authorize']} (redirect hosts: "
             f"{counted(c['authorize_hosts'], list(REDIRECT_HOSTS) + ['other'])})")
    L.append(f"consent: approved {n['consent_approved']}, denied {n['consent_denied']}, "
             f"wrong secret {n['consent_wrong']}")
    reasons = counted(c["revoke_reasons"])
    L.append(f"token: code exchanged {n['code_exchanged']} (auth method: {counted(c['exchange_methods'])}) | "
             f"refresh {n['refresh']} | reused {n['reused']} | revoked {n['revoked']} "
             f"(reason: {'none' if reasons == '0' else reasons})")
    if bearer:
        signin = "sign-in: on, static bearer " + " and ".join(sorted(bearer))
    else:
        signin = "sign-in: no start line in this window"
    L.append(f"{signin} | owner secret changed: {n['secret_changed']}")
    L.append("== tool errors")
    parts = [f"{g}: {counted(tool_errors[g])}" for g in GROUPS]
    if other_group:
        parts.append(f"other group: {other_group}")
    L.append(" | ".join(parts))
    L.append("== key mode (KEY 4.11 lines)")

    def codes(d, word):
        return f"{word}: " + (", ".join(sorted(d)) if d else "none")
    L.append(f"on: {key['on']} | new access token: {key['new']} | access token from the cache: {key['cache']} | "
             f"access token refused (401): {key['refused401']} | key refused: {key['key_refused']} "
             f"({codes(key_codes, 'HTTP codes')}) | first token request failed: {key['first_failed']} "
             f"({codes(first_groups, 'groups')}) | later token request failed: {key['later_failed']} "
             f"({codes(later_groups, 'groups')}) | key expiry warning: {'yes' if key['expiry'] else 'no'} | "
             f"cache file mode fixed: {key['mode_fixed']}")
    L.append("== rate limits")
    L.append(f"lines: {n['rate_limit']} | caller address: {counted(c['rate_classes'], list(ADDRESS_CLASSES))}")
    L.append("== edge (Caddy access log)")
    L.append(f"entries: {n['caddy']} | client_ip: {counted(c['caddy_classes'], list(ADDRESS_CLASSES))} | "
             f"/authorize with ?REDACTED: {n['caddy_redacted_authorize']}, with any other query: "
             f"{n['caddy_other_query']} | Location kept: {n['location']} | request headers kept: {n['req_headers']}"
             f" | Railway health checks: {n['caddy_health']} (GET /healthz from its own network; not in client_ip)")
    L.append("== app access lines")
    L.append(f"lines: {n['app']} | with a query: {n['app_query']} | every query shown as ?[redacted]: "
             f"{'yes' if not n['app_bad_query'] else 'no'}")
    L.append("== Railway HTTP log (T11)")
    shown_fields = sorted(fields)
    field_text = ", ".join(shown_fields[:12]) + (", ..." if len(shown_fields) > 12 else "")
    names_text = ", ".join(sorted(k for k in qnames if k != "other"))
    if qnames.get("other"):
        names_text = (names_text + ", " if names_text else "") + f"other {qnames['other']}"
    L.append(f"entries: {n_http} | fields: {field_text or 'none'} | status: "
             f"{counted(statuses, ['2xx', '3xx', '4xx', '5xx'])} | paths with a query: {n_http_query} | "
             f"query parameter names: {names_text or 'none'} | sensitive names (code, token, secret, password, "
             f"key): {sensitive if sensitive else 'none'}")
    L.append("== leak scan")
    names = ", ".join(secrets_) if secrets_ else "none"
    L.append(f"secret values checked: {len(secrets_)} ({names}) | found in deploy log: {len(in_deploy)}"
             f"{' (' + ', '.join(in_deploy) + ')' if in_deploy else ''} | found in HTTP log: {len(in_http)}"
             f"{' (' + ', '.join(in_http) + ')' if in_http else ''}")

    if in_deploy or in_http:
        failed.append("a secret value is in a log")
    if c["rate_classes"].get("loopback") or c["rate_classes"].get("private"):
        failed.append("a rate-limit caller address is loopback or private")
    if c["caddy_classes"].get("loopback") or c["caddy_classes"].get("private"):
        failed.append("a Caddy client_ip is loopback or private")
    if n["location"]:
        failed.append("Caddy kept a Location header")
    if n["req_headers"]:
        failed.append("Caddy kept request headers")
    if n["caddy_other_query"] or n["app_bad_query"]:
        failed.append("a query string is not redacted")
    if sensitive:
        failed.append("a sensitive query parameter name is in Railway's HTTP log")
    if tool_errors["internal"]:
        L.append(f"WARN internal tool errors: {sum(tool_errors['internal'].values())} "
                 "(see the Railway log; refs only)")
    for why in failed:
        L.append(f"FAIL {why}")
    L.append("RESULT: FAIL" if failed else "RESULT: PASS")
    return L, bool(failed)


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def emit(lines, save, redact):
    lines = [redact(x) for x in lines]
    for x in lines:
        print(x)
    if save:
        write_private(save, "\n".join(lines) + "\n")


def cmd_logs(rw, args):
    require_guard(rw)
    try:
        variables = rw.variables()
        secrets_ = secret_values(variables)
    except Failed as e:
        print(f"FAIL logs: cannot read the Railway variables for the leak scan ({e})")
        return 1
    try:
        c1, deploy_text, _ = rw.call(["logs", "--since", args.since, "--json"], timeout=120)
        c2, http_text, _ = rw.call(["logs", "--http", "--since", args.since, "--json"], timeout=120)
    except Failed as e:
        print(f"FAIL logs: railway logs did not run ({e})")
        return 1
    if c1 != 0 or c2 != 0:
        print(f"FAIL logs: railway logs did not run (railway exit {c1 or c2})")
        return 1
    lines, failed = summarize_logs(deploy_text, http_text, secrets_, args.since, load_tool_names())
    emit(lines, args.save, Redactor(secrets_.values()))
    return 1 if failed else 0


# ---------------------------------------------------------------- deploys

def find_entries(data):
    """The deployment entries: the list itself, or the first list of objects inside an object."""
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and any(isinstance(e, dict) for e in v):
                return [e for e in v if isinstance(e, dict)]
            if isinstance(v, dict):
                found = find_entries(v)
                if found:
                    return found
    return None


def entry_status(entry):
    status = entry.get("status")
    if not isinstance(status, str):
        node = entry.get("node") if isinstance(entry.get("node"), dict) else {}
        status = node.get("status")
    if isinstance(status, str) and status.upper() in DEPLOY_STATUSES:
        return status.upper()
    return "other"


def parse_time(text):
    if not isinstance(text, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?$", text.strip())
        if not m:
            return None
        try:
            t = datetime.datetime.fromisoformat(m.group(1) + (m.group(2) or "Z").replace("Z", "+00:00"))
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t


def deploy_summary(text):
    """(line, newest status or None)."""
    try:
        data = json.loads(text)
    except ValueError:
        return "deployments: other (not readable; see the dashboard)", None
    entries = find_entries(data)
    if entries is None:
        return "deployments: other (shape not recognised; see the dashboard)", None
    counts = {}
    for e in entries:
        bump(counts, entry_status(e))
    if not entries:
        return "deployments: 0", None
    timed = [(parse_time(e.get("createdAt")), i, e) for i, e in enumerate(entries)]
    with_time = [t for t in timed if t[0] is not None]
    if with_time:
        when, _, newest = max(with_time, key=lambda t: (t[0], -t[1]))
        minutes = max(0, int((datetime.datetime.now(datetime.timezone.utc) - when).total_seconds() // 60))
        age = f", {minutes} min ago"
    else:
        newest, age = entries[0], ""
    status = entry_status(newest)
    others = sum(v for k, v in counts.items() if k not in ("SUCCESS", "FAILED"))
    line = (f"deployments: {len(entries)} (SUCCESS {counts.get('SUCCESS', 0)}, FAILED {counts.get('FAILED', 0)}, "
            f"other {others}) | newest: {status}{age}")
    return line, status


def cmd_deploys(rw, args):
    require_guard(rw)
    poll = _env_float("LIVE_CHECK_TEST_POLL", 15.0)
    limit = _env_float("LIVE_CHECK_TEST_WAIT", 900.0)
    start = time.monotonic()
    while True:
        try:
            code, out, _ = rw.call(["deployment", "list", "--json", "--limit", "10"], timeout=60)
        except Failed as e:
            print(f"FAIL deploys: railway deployment list did not run ({e})")
            return 1
        line, status = deploy_summary(out if code == 0 else "")
        if not args.wait:
            print(line)
            return 0 if code == 0 and status is not None else 1
        if status == "SUCCESS":
            print(line)
            print("PASS deploys: the newest deployment is SUCCESS")
            return 0
        if status in DEPLOY_DONE_BAD:
            print(line)
            print(f"FAIL deploys: the newest deployment is {status}")
            return 1
        if time.monotonic() - start + poll > limit:
            print(line)
            print(f"FAIL deploys: no SUCCESS within {int(limit // 60)} minutes")
            return 1
        time.sleep(poll)


# ---------------------------------------------------------------- healthcheck

def find_key(node, key, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                out.append(v)
            find_key(v, key, out)
    elif isinstance(node, list):
        for v in node:
            find_key(v, key, out)
    return out


def cmd_healthcheck(rw, args):
    require_guard(rw)
    try:
        code, out, _ = rw.call(["environment", "config", "--json"], timeout=60)
    except Failed as e:
        print(f"FAIL healthcheck: railway environment config did not run ({e})")
        return 1
    try:
        data = json.loads(out) if code == 0 else None
    except ValueError:
        data = None
    if data is None:
        print("FAIL healthcheck: the environment config is not readable")
        return 1
    values = find_key(data, "healthcheckPath", [])
    if len(values) == 1 and values[0] == "/healthz":
        print("PASS healthcheck: deploy.healthcheckPath is /healthz")
        return 0
    shown = ", ".join(v if isinstance(v, str) and re.match(r"^/[a-z]{1,20}$", v) else "other" for v in values)
    print(f"FAIL healthcheck: {len(values)} healthcheckPath keys, values: {shown or 'none'}")
    return 1


# ---------------------------------------------------------------- metrics

def _numbers(node):
    """Every number in a series: a list of numbers, or of objects with a value."""
    out = []
    if isinstance(node, bool):
        return out
    if isinstance(node, (int, float)):
        return [float(node)]
    if isinstance(node, list):
        for v in node:
            if isinstance(v, dict):
                for k in ("value", "y", "avg", "average"):
                    if isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool):
                        out.append(float(v[k]))
                        break
            elif isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[1], (int, float)):
                out.append(float(v[1]))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(float(v))
    return out


LABEL_FIELDS = ("measurement", "metric", "name", "type", "key")


def _metric(data, words):
    """(avg, max, unit hint) for the first key (or labelled object) naming one of `words`; (None, None, hint)
    if present but empty; None if there is none."""
    stack = [("", data)]
    while stack:
        key, node = stack.pop(0)
        lk = key.lower()
        if isinstance(node, dict):
            label = next((node[f] for f in LABEL_FIELDS if isinstance(node.get(f), str)
                          and any(w in node[f].lower() for w in words)), None)
            if label is not None:
                lk = label.lower()
        if any(w in lk for w in words):
            hint = "gb" if "gb" in lk else "mb" if "mb" in lk else "byte" if "byte" in lk else ""
            if isinstance(node, dict):
                avg = next((node[k] for k in ("avg", "average", "mean", "value") if isinstance(node.get(k), (int, float))),
                           None)
                mx = next((node[k] for k in ("max", "peak") if isinstance(node.get(k), (int, float))), None)
                unit = node.get("unit") if isinstance(node.get("unit"), str) else ""
                hint = hint or ("gb" if unit.lower() == "gb" else "mb" if unit.lower() == "mb"
                                else "byte" if "byte" in unit.lower() else "")
                if avg is None and mx is None:
                    for k in ("values", "series", "points", "data", "measurements"):
                        nums = _numbers(node.get(k))
                        if nums:
                            return sum(nums) / len(nums), max(nums), hint
                    return None, None, hint
                avg = float(avg if avg is not None else mx)
                return avg, float(mx if mx is not None else avg), hint
            nums = _numbers(node)
            if nums:
                return sum(nums) / len(nums), max(nums), hint
            return None, None, hint
        if isinstance(node, dict):
            stack.extend((str(k), v) for k, v in node.items())
        elif isinstance(node, list):
            stack.extend((key if not isinstance(v, dict) else key, v) for v in node if isinstance(v, (dict, list)))
    return None


def to_mb(value, hint):
    if hint == "mb":
        return value
    if hint == "byte":
        return value / 1e6
    return value * 1000.0  # Railway reports memory and volume in GB


def human_since(since):
    m = re.match(r"^(\d+)\s*([smhdw])$", since.strip())
    if not m:
        return since
    n, unit = int(m.group(1)), {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week"}[m.group(2)]
    return f"{n} {unit}{'' if n == 1 else 's'}"


def metrics_lines(text, since):
    """(lines, exit code)."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    words = {"memory": ("memory",), "cpu": ("cpu",), "volume": ("volume", "disk")}
    found = {w: (_metric(data, words[w]) if data is not None else None) for w in words}
    if all(v is None for v in found.values()):
        return ["INFO metrics: metrics shape not recognised; read them in the dashboard (service > Metrics)"], 0
    lines, bad = [], False
    for word, label in (("memory", "memory"), ("cpu", "CPU"), ("volume", "volume")):
        v = found[word]
        if v is None or v[0] is None:
            lines.append(f"metrics: {label} not readable from the CLI; read it in the dashboard (service > Metrics)")
            bad = True
    if bad:
        return lines, 1
    mem_avg, mem_max = (to_mb(x, found["memory"][2]) for x in found["memory"][:2])
    cpu_avg, cpu_max = found["cpu"][:2]
    vol = to_mb(found["volume"][1], found["volume"][2])
    mem_cost = round(mem_avg / 1000.0 * PRICE_GB_RAM, 2)
    cpu_cost = round(cpu_avg * PRICE_VCPU, 2)
    vol_cost = round(vol / 1000.0 * PRICE_GB_VOLUME, 2)
    total = round(mem_avg / 1000.0 * PRICE_GB_RAM + cpu_avg * PRICE_VCPU + vol / 1000.0 * PRICE_GB_VOLUME, 2)
    lines.append(f"metrics (since {since}): memory avg {mem_avg:.0f} MB, max {mem_max:.0f} MB | CPU avg "
                 f"{cpu_avg:.2f} vCPU, max {cpu_max:.2f} | volume {vol:.0f} MB")
    lines.append(f"monthly at this use (from about {human_since(since)} of use, not a monthly bill): memory "
                 f"${mem_cost:.2f} + CPU ${cpu_cost:.2f} + volume ${vol_cost:.2f} = ${total:.2f}; Railway Hobby is "
                 "$5 a month and includes $5 of usage")
    return lines, 0


def cmd_metrics(rw, args):
    require_guard(rw)
    try:
        code, out, _ = rw.call(["metrics", "--cpu", "--memory", "--volume", "--since", args.since, "--json"],
                               timeout=120)
    except Failed as e:
        print(f"FAIL metrics: railway metrics did not run ({e})")
        return 1
    lines, rc = metrics_lines(out if code == 0 else "", args.since)
    emit(lines, args.save, lambda s: s)
    return rc


# ---------------------------------------------------------------- ssh

def token_hits(listing):
    """(files checked, hit paths) from `find / -xdev -type f` output; /data is skipped."""
    checked, hits = 0, []
    for line in listing.splitlines():
        path = line.rstrip("\n")
        if not path.startswith("/"):
            continue
        checked += 1
        if path == "/data" or path.startswith("/data/"):
            continue
        segments = [s for s in path.split("/") if s]
        if (any(fnmatch.fnmatchcase(s, p) for s in segments for p in TOKEN_PATTERNS)
                or fnmatch.fnmatchcase(path, TOKEN_PATH_PATTERN)):
            hits.append(path)
    return checked, hits


def ssh(rw, words, timeout=120):
    """(exit code, stdout); raises Failed with a reason class."""
    code, out, _ = rw.call(["ssh", "--"] + words, timeout=timeout)
    return code, out


def ssh_fail(reason):
    print(f"FAIL ssh: railway ssh did not run ({reason}); see https://docs.railway.com/cli/ssh")
    return 1


FIND_WORDS = ["find", "/", "-xdev", "-type", "f"]


def folders(paths):
    out = []
    for p in paths:
        d = os.path.dirname(p)
        out.append(d if FOLDER_RE.match(d) else "(folder not shown)")
    return ", ".join(sorted(set(out)))


def cmd_ssh(rw, args):
    if args.probe == "wipe-garmin" and not args.yes:
        print("FAIL ssh wipe-garmin: it deletes the Garmin tokens; run it again with --yes. Nothing was run.")
        return 2
    require_guard(rw)
    try:
        if args.probe == "cache-modes":
            code, out = ssh(rw, ["stat", "-c", "%a:%n", "/data/garmin-key", "/data/garmin-key/token.json"])
            modes = {}
            for line in out.splitlines():
                mode, sep, path = line.strip().partition(":")
                if sep and path in ("/data/garmin-key", "/data/garmin-key/token.json"):
                    modes[path] = mode if MODE_RE.match(mode) else "?"
            if not modes and code != 0:
                return ssh_fail(f"exit {code}")
            folder = modes.get("/data/garmin-key", "missing")
            token = modes.get("/data/garmin-key/token.json", "missing")
            ok = folder == "700" and token == "600"
            print(f"{'PASS' if ok else 'FAIL'} ssh cache-modes: /data/garmin-key {folder}, "
                  f"/data/garmin-key/token.json {token} (expected 700 and 600)")
            return 0 if ok else 1
        code, out = ssh(rw, FIND_WORDS, timeout=300)
        checked, hits = token_hits(out)
        if checked == 0:
            return ssh_fail(f"exit {code}, no file listed")
        if args.probe == "token-copies":
            if hits:
                print(f"FAIL ssh token-copies: {len(hits)} Garmin token files outside /data ({checked} files "
                      f"checked), in: {folders(hits)}")
                return 1
            print(f"PASS ssh token-copies: no Garmin token file outside /data ({checked} files checked)")
            return 0
        # wipe-garmin
        bad = [h for h in hits if not SAFE_PATH_RE.match(h)]
        if bad:
            print(f"FAIL ssh wipe-garmin: {len(bad)} token file paths have unexpected characters; nothing was "
                  "removed (delete them by hand)")
            return 2
        if not rw.guard(quiet=True):
            return 2
        ssh(rw, ["rm", "-rf", "/data/tokens", "/data/garmin-key"] + hits)
        code, listing = ssh(rw, ["ls", "-A", "/data"])
        left = {x.strip() for x in listing.splitlines()} & {"tokens", "garmin-key"}
        code2, out2 = ssh(rw, FIND_WORDS, timeout=300)
        checked2, hits2 = token_hits(out2)
        if code != 0 or checked2 == 0:
            print("FAIL ssh wipe-garmin: the check after the removal did not run; run it again")
            return 1
        if left or hits2:
            print(f"FAIL ssh wipe-garmin: still there: {', '.join(sorted(left)) or 'none'} in /data, "
                  f"{len(hits2)} token files outside /data")
            return 1
        print("PASS ssh wipe-garmin: /data/tokens and /data/garmin-key are gone and no token file is left "
              "outside /data")
        return 0
    except Failed as e:
        return ssh_fail(str(e))


# ---------------------------------------------------------------- probe-ip

def cmd_probe_ip(rw, args):
    require_guard(rw)
    url = public_url(read_variables_or_exit(rw, "probe-ip"))
    if url is None:
        print("FAIL probe-ip: neither PUBLIC_URL nor RAILWAY_PUBLIC_DOMAIN is set on Railway")
        return 2
    path = "/probe/" + secrets.token_hex(8)
    headers = {"X-Real-IP": PLANTED[0][0], "X-Forwarded-For": PLANTED[1][0], "CF-Connecting-IP": PLANTED[2][0],
               "User-Agent": "live-check probe-ip"}
    sent = 0
    for _ in range(3):
        req = urllib.request.Request(url + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout_for(20)) as resp:
                resp.read(1024)
            sent += 1
        except urllib.error.HTTPError as e:
            e.close()
            sent += 1
        except (urllib.error.URLError, OSError, ValueError):
            pass
    print("INFO probe-ip: 3 requests to a random /probe/ path with planted X-Real-IP, X-Forwarded-For and "
          "CF-Connecting-IP")
    if sent == 0:
        print("FAIL probe-ip: the probe requests could not be sent; run probe-ip again")
        return 1
    poll = _env_float("LIVE_CHECK_TEST_POLL", 10.0)
    limit = _env_float("LIVE_CHECK_TEST_WAIT", 90.0)
    start = time.monotonic()
    found = []
    while True:
        try:
            code, out, _ = rw.call(["logs", "--since", "5m", "--json"], timeout=60)
        except Failed:
            code, out = 1, ""
        found = []
        if code == 0:
            for message, obj in log_entries(out):
                entry = caddy_entry(message, obj)
                if entry is None:
                    continue
                req = _as_dict(entry.get("request"))
                if req.get("uri") == path:
                    found.append(str(req.get("client_ip", "")))
        if len(found) >= 3 or time.monotonic() - start + poll > limit:
            break
        time.sleep(poll)
    if not found:
        print(f"FAIL probe-ip: the probe requests were not in the log within {int(limit)} s; run probe-ip again")
        return 1
    head = f"{min(len(found), 3)} of 3 requests found in Caddy's access log"
    for addr, header in PLANTED:
        if addr in found:
            if header == "X-Real-IP":
                print(f"FAIL probe-ip: {head}; client_ip is the planted X-Real-IP: yes (Railway passes a client's own "
                      "X-Real-IP through, so per-caller rate limits can be dodged)")
            else:
                print(f"FAIL probe-ip: {head}; client_ip is the planted {header}: yes (Caddy reads a header the caller "
                      "writes)")
            return 1
    classes = sorted({address_class(a) for a in found})
    for cls in ("private", "loopback"):
        if cls in classes:
            print(f"FAIL probe-ip: {head}; client_ip class: {cls} (Railway's internal hop; every caller would share one "
                  "rate-limit bucket)")
            return 1
    if classes != ["public"]:
        print(f"FAIL probe-ip: {head}; client_ip class: {', '.join(c if c in ADDRESS_CLASSES else 'other' for c in classes)}")
        return 1
    print(f"PASS probe-ip: {head}; client_ip is a planted address: no; client_ip class: public")
    return 0


# ---------------------------------------------------------------- data-shape

def label_of(path):
    base = os.path.basename(path)
    m = re.match(r"^reply-(.+)\.json$", base)
    return m.group(1) if m else os.path.splitext(base)[0]


def key_paths(node, prefix="", out=None):
    out = set() if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            seg = k if isinstance(k, str) and SEGMENT_RE.match(k) else "?"
            p = f"{prefix}.{seg}" if prefix else seg
            out.add(p)
            key_paths(v, p, out)
    elif isinstance(node, list):
        for v in node:
            key_paths(v, prefix + "[]", out)
    return out


def has_key(node, key):
    if isinstance(node, dict):
        return key in node or any(has_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(has_key(v, key) for v in node)
    return False


def strings_by_key(node, out=None):
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and SEGMENT_RE.match(k):
                _collect_strings(v, out.setdefault(k, []))
            strings_by_key(v, out)
    elif isinstance(node, list):
        for v in node:
            strings_by_key(v, out)
    return out


def _collect_strings(v, bucket):
    if isinstance(v, str):
        bucket.append(v)
    elif isinstance(v, list):
        for x in v:
            if isinstance(x, str):
                bucket.append(x)


def invisible_count(s):
    return sum(1 for ch in s if ch not in "\n\t" and unicodedata.category(ch) in ("Cf", "Cc", "Co"))


def at_cap(s, cap, lower):
    return len(s) == cap or (s.endswith(ELLIPSIS) and lower < len(s) <= cap)


def shape_table(name, text):
    """Lines for one reply file and the parsed JSON (or None)."""
    is_error = "yes" if text.lstrip().startswith("Error:") else "no"
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    is_json = data is not None and isinstance(data, (dict, list))
    lines = [f"== {name}: isError {is_error}, {len(text)} chars, JSON {'yes' if is_json else 'no'}"]
    if not is_json:
        return lines, None
    lines.append(f"{'key':<20} {'strings':>8} {'max len':>8} {'at 100':>7} {'at 500':>7} {'[link]':>7} "
                 f"{'[image]':>8} {'[html]':>7} {'invisible':>10}")
    for key, values in sorted(strings_by_key(data).items()):
        if not any(len(s) > 20 for s in values):
            continue
        lines.append(f"{key:<20} {len(values):>8} {max(len(s) for s in values):>8} "
                     f"{sum(at_cap(s, 100, 0) for s in values):>7} {sum(at_cap(s, 500, 100) for s in values):>7} "
                     f"{sum(MARKS[0] in s for s in values):>7} {sum(MARKS[1] in s for s in values):>8} "
                     f"{sum(MARKS[2] in s for s in values):>7} {sum(invisible_count(s) for s in values):>10}")
    return lines, data


def cmd_data_shape(args):
    required = []
    for spec in args.require_key or []:
        label, sep, key = spec.partition(":")
        if not sep or not label or not key:
            print("FAIL data-shape: --require-key must be LABEL:KEY")
            return 2
        required.append((label, key))
    labels = {label_of(p): p for p in args.files}
    missing = [lbl for lbl, _ in required if lbl not in labels]
    if missing:
        print(f"FAIL data-shape: no reply file for {', '.join(sorted(set(missing)))}")
        if not args.keep:
            _delete(args.files)
        return 2
    earlier = None
    if args.compare:
        try:
            with open(args.compare, encoding="utf-8") as f:
                earlier = json.load(f)
        except (OSError, ValueError):
            print(f"FAIL data-shape: {os.path.basename(args.compare)} is not readable")
            if not args.keep:
                _delete(args.files)
            return 2
        if not isinstance(earlier, dict):
            earlier = {}
    shapes, parsed, failed, compare_lines = {}, {}, [], []
    try:
        for path in args.files:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                print(f"== {os.path.basename(path)}: not readable")
                failed.append(f"{os.path.basename(path)} not readable")
                continue
            lines, data = shape_table(os.path.basename(path), text)
            for x in lines:
                print(x)
            label = label_of(path)
            parsed[label] = data
            paths = sorted(key_paths(data)) if data is not None else []
            shapes[label] = paths
            if earlier is not None:
                if label not in earlier or not isinstance(earlier.get(label), list):
                    compare_lines.append(f"compare {label}: no earlier shape")
                else:
                    old = {p for p in earlier[label] if isinstance(p, str)}
                    here = sorted(set(paths) - old)
                    there = sorted(old - set(paths))
                    compare_lines.append(f"compare {label}: only here {len(here)} ({', '.join(here[:20])}) | only "
                                         f"in {os.path.basename(args.compare)} {len(there)} ({', '.join(there[:20])})")
        for x in compare_lines:
            print(x)
        for label, key in required:
            ok = has_key(parsed.get(label), key)
            print(f"{'PASS' if ok else 'FAIL'} require-key {label}:{key}")
            if not ok:
                failed.append(f"{label} has no {key}")
        if args.keys_out:
            write_private(args.keys_out, json.dumps(shapes, indent=1, sort_keys=True) + "\n")
    finally:
        if not args.keep:
            _delete(args.files)
    print("RESULT: FAIL" if failed else "RESULT: INFO (numbers only)")
    return 1 if failed else 0


def _delete(paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------- main

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS, help="folder whose Railway link is used (default: "
                        "this repository)")
    common.add_argument("--project", default=argparse.SUPPRESS,
                        help=f"one of {PROJECTS_ENV} (default: the first; {PROJECTS_ENV} defaults to "
                        f"{','.join(DEFAULT_PROJECTS)})")
    p = argparse.ArgumentParser(prog="live-check.py", parents=[common],
                                description="Staging helper for a throwaway Railway deployment (no secret shown).")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("guard", parents=[common], help="check the folder's Railway link")
    s.add_argument("--allow-unlinked", action="store_true", help="pass when the folder is not linked (first deploy)")
    sub.add_parser("url", parents=[common], help="print the project's public URL")
    s = sub.add_parser("run", parents=[common], help="run a command with MCP_BEARER_TOKEN from Railway")
    s.add_argument("--fill", action="append", metavar="NAME=FILE:KEY",
                   help="replace @NAME@ in the command with the first digits value of KEY in FILE (never shown)")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- <command> [args...]")
    s = sub.add_parser("logs", parents=[common], help="summarize the deploy and HTTP logs")
    s.add_argument("--since", default="60m")
    s.add_argument("--save", metavar="FILE")
    s = sub.add_parser("deploys", parents=[common], help="count deployments")
    s.add_argument("--wait", action="store_true", help="wait until the newest is SUCCESS or failed")
    sub.add_parser("healthcheck", parents=[common], help="check deploy.healthcheckPath")
    s = sub.add_parser("metrics", parents=[common], help="CPU, memory and volume use, and a monthly figure")
    s.add_argument("--since", default="3h")
    s.add_argument("--save", metavar="FILE")
    s = sub.add_parser("ssh", parents=[common], help="one of three fixed probes in the container")
    s.add_argument("probe", choices=("cache-modes", "token-copies", "wipe-garmin"))
    s.add_argument("--yes", action="store_true", help="needed by wipe-garmin")
    sub.add_parser("probe-ip", parents=[common], help="which caller address Caddy logs")
    s = sub.add_parser("data-shape", parents=[common], help="numbers about saved tool replies (then deletes them)")
    s.add_argument("files", nargs="+", metavar="FILE")
    s.add_argument("--keep", action="store_true")
    s.add_argument("--keys-out", metavar="SHAPES")
    s.add_argument("--compare", metavar="SHAPES")
    s.add_argument("--require-key", action="append", metavar="LABEL:KEY")
    return p


def allowed_projects():
    """The project names from LIVE_CHECK_PROJECTS, or None when one is not a plain name."""
    raw = os.environ.get(PROJECTS_ENV, "").strip()
    if not raw:
        return DEFAULT_PROJECTS
    names = tuple(x.strip() for x in raw.split(",") if x.strip())
    if not names or not all(PROJECT_NAME_RE.match(x) for x in names):
        return None
    return names


def main(argv=None):
    args = build_parser().parse_args(argv)
    allowed = allowed_projects()
    if allowed is None:
        print(f"live-check: {PROJECTS_ENV} must be a comma list of Railway project names (letters, digits, "
              "'.', '_', '-')")
        return 2
    project = getattr(args, "project", allowed[0])
    if project not in allowed:
        print(f"live-check: --project must be one of {PROJECTS_ENV}: {', '.join(allowed)}")
        return 2
    if args.cmd == "data-shape":
        return cmd_data_shape(args)
    folder = os.path.abspath(os.path.expanduser(getattr(args, "dir", ROOT)))
    if not os.path.isdir(folder):
        print("live-check: --dir is not a folder")
        return 2
    rw = Railway(folder, project)
    handlers = {"guard": cmd_guard, "url": cmd_url, "run": cmd_run, "logs": cmd_logs, "deploys": cmd_deploys,
                "healthcheck": cmd_healthcheck, "metrics": cmd_metrics, "ssh": cmd_ssh, "probe-ip": cmd_probe_ip}
    try:
        return handlers[args.cmd](rw, args)
    except Refused as e:
        print(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
