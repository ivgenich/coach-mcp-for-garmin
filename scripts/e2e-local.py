#!/usr/bin/env python3
"""End-to-end check of the built-in sign-in on this machine, with fake secrets.

    .venv/bin/python scripts/e2e-local.py --work /path/to/scratch/e2e
        [--server-port 8340] [--edge-port 8341] [--caddy /opt/homebrew/bin/caddy] [--legs direct,caddy,live]

Run it with a python that has mcp 2.3.0 and upstream garmin-mcp installed: the
server and scripts/test-protocol.py need them (this file itself is standard
library only). It talks to 127.0.0.1 only, never to a deployment, and leaves
no process running.

 1. Makes fresh fake OWNER_SECRET and MCP_BEARER_TOKEN values, and a new run
    folder under --work with its own AUTH_DB, HOME and an empty GARMINTOKENS
    folder for each leg (no Garmin login is possible). Every server runs in
    full mode (GARMIN_AUTH=full), REPORT_TZ=UTC.
 2. ext/launch.py --check-config: a good environment must print exactly
    "launch: config OK (<n> read-only tools)" and create AUTH_DB with mode
    0600; a bad one must print exactly the A2, A3, A5 and A8 lines.
 3. Direct leg: the server on --server-port with PUBLIC_URL=http://127.0.0.1:<port>;
    scripts/test-signin.py --run-protocol, then scripts/test-protocol.py with
    the static bearer.
 4. Caddy leg: the server again, behind `caddy run` on --edge-port with a
    copy of the repo's Caddyfile that adds one line, `default_bind 127.0.0.1`
    after `admin off`, so the edge listens on loopback only. The run checks
    that this line is the only difference and that `caddy adapt` of the copy
    listens on 127.0.0.1 only. Then test-signin.py --via-caddy --run-protocol,
    test-protocol.py with the static bearer through Caddy, and one POST /mcp
    that carries the static bearer only in other headers (X-Api-Key and the
    like), which Caddy's access log must not keep.
 5. Live leg: a fresh server (its own AUTH_DB and folders, same port), then
    scripts/test-signin.py --live --run-protocol, as an owner runs it against
    a deployment (no prompt: OWNER_SECRET is in its environment), with
    --report and --protocol-args "--report ..."; its report must show S46
    PASS with "its sign-in revoked: yes", and test-protocol's report
    server/version PASS.
 6. Greps every server and Caddy log, and the test scripts' own output, for
    every secret value of the run (8+ characters): raw, percent-encoded and
    JSON-escaped, plus base64(client_id:client_secret) for every confidential
    client, in the text as is and with all whitespace removed. Then the
    positive controls: Caddy's access log has /authorize?REDACTED and no
    Location field, the server log has ?[redacted], and every "rate limit:"
    line in the server log names a caller's own address (203.0.113.x), never
    Caddy's 127.0.0.1 or the stand-in edge hop that S47 sends. Each server
    log also has the "tool error:" lines of failed tool calls: 1+ login (S45's
    get_activities), 2+ bad_input (test-protocol's bad date), no internal.
    The live leg runs neither S36-S38, S45 nor S47, so there it needs no
    rate-limit or login line; any rate-limit line must still name a caller.

Every process is stopped by its PID (SIGTERM, then SIGKILL after 10 s), and
each one's stdout and stderr go to their own files in the run folder. Only
statuses and counts are printed, never a secret. Exit status 0 means every
step passed and the logs are clean.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import shlex
from urllib.parse import quote, quote_plus

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SIGNIN = os.path.join(HERE, "test-signin.py")
PROTOCOL = os.path.join(HERE, "test-protocol.py")
PREFIX = "launch: config error: "
# What --check-config prints for the bad environment below.
BAD_ENV_LINES = (
    ("A2", "PUBLIC_URL must start with https:// (http:// only for 127.0.0.1, localhost or [::1], and never on Railway)"),
    ("A3", "PUBLIC_URL must be an origin only (scheme, host, optional port): no path, query, fragment or user info"),
    ("A5", "OWNER_SECRET must be 32 to 256 printable ASCII characters, with no space at the start or end"),
    ("A8", "MCP_BEARER_TOKEN (optional) must be 32 to 512 characters of A-Z a-z 0-9 . _ ~ -"),
)
BAD_ENV = {"PUBLIC_URL": "http://example.com/mcp", "OWNER_SECRET": "short", "MCP_BEARER_TOKEN": "bad token"}
LOOPBACK_BIND = "default_bind 127.0.0.1"  # the one line the Caddy leg adds to the repo's Caddyfile
# A "rate limit:" line of ext/auth_http.py; the address is the bucket's caller.
RATE_LIMIT_LINE = re.compile(r"rate limit: \S+ \S+ \([^)]*\) from (\S+), retry in \d+s")
CALLER_ADDRESS = re.compile(r"203\.0\.113\.\d{1,3}|160\.79\.104\.9")  # test-signin.py's callers
# The "tool error:" log lines of ext/launch.py's tool-error wrapper: S45's call
# without a Garmin login, test-protocol.py's impossible date, and the internal-error marker.
TOOL_ERROR_LOGIN = "tool error: tool=get_activities group=login:"
TOOL_ERROR_BAD_INPUT = "tool error: tool=get_daily_status group=bad_input:"
TOOL_ERROR_INTERNAL = "group=internal"

results: list[tuple[str, bool]] = []
SECRET_VALUES: list[str] = []  # this run's fake secrets, scrubbed from anything echoed


def yn(flag) -> str:
    return "yes" if flag else "no"


def scrub(text: str) -> str:
    for v in SECRET_VALUES:
        for form in secret_forms(v):
            text = text.replace(form, "<redacted>")
    return text


def step(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'} e2e/{name} {scrub(detail)}", flush=True)


def info(name: str, detail: str) -> None:
    print(f"INFO e2e/{name} {scrub(detail)}", flush=True)


def secret_forms(value: str) -> set:
    """The ways a value can appear in a log: raw, percent-encoded (path and form
    style) and JSON-escaped (Python's, and Go's HTML-safe style that Caddy uses)."""
    py_json = json.dumps(value)[1:-1]
    forms = {
        value,
        quote(value, safe=""),
        quote_plus(value),
        py_json,
        json.dumps(value, ensure_ascii=False)[1:-1],
        py_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"),
    }
    return {f for f in forms if len(f) >= 8}


def fake_owner_secret() -> str:
    # Printable ASCII (A5) with characters that change under percent-encoding and
    # JSON escaping, so the log grep's encoded forms are really exercised.
    return "TEST-ONLY " + secrets.token_urlsafe(24) + ' +/=%&<>"\\!'


def fake_bearer() -> str:
    return "TEST-ONLY-" + secrets.token_urlsafe(32)  # A8: only A-Z a-z 0-9 . _ ~ -


def tool_count(project: str):
    try:
        with open(os.path.join(project, "config", "enabled-tools.txt"), encoding="utf-8") as f:
            names = {"".join(line.split("#", 1)[0].split()) for line in f}
    except OSError:
        return None
    names.discard("")
    return len(names)


def port_in_use(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def get_status(port: int, path: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def post_status(port: int, path: str, headers: dict) -> int:
    """The status of one POST with an empty JSON object and these headers."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", path, body=b"{}", headers={"Content-Type": "application/json", **headers})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def wait_up(port: int, proc: subprocess.Popen, timeout: float = 90.0):
    """(True, seconds) once GET /healthz on the port answers 200; (False, why) otherwise."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if proc.poll() is not None:
            return False, f"the process exited with {proc.returncode}"
        try:
            if get_status(port, "/healthz") == 200:
                return True, f"{time.monotonic() - t0:.1f}s"
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.3)
    return False, f"no 200 from /healthz in {timeout:.0f}s"


def start(cmd, env, out_path, err_path, cwd) -> subprocess.Popen:
    out = open(out_path, "w", encoding="utf-8")
    err = open(err_path, "w", encoding="utf-8")
    try:
        return subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    finally:
        out.close()  # the child keeps its own copies
        err.close()


def stop(proc: subprocess.Popen, label: str) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)  # this PID only
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    info("stop", f"{label}: pid {proc.pid} stopped (exit {proc.returncode})")


def tail(path: str, n: int = 15) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()[-n:]
    except OSError:
        return "(no log)"
    return "\n".join("      " + scrub(line)[:300] for line in lines)


def loopback_caddyfile(source: str, dest: str):
    """Write `source` to `dest` with LOOPBACK_BIND added after the global
    `admin off` line, at its indentation. (True, detail) when that one added
    line is the whole difference, by difflib; (False, why) otherwise."""
    with open(source, encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)
    out, added = [], []
    for line in lines:
        out.append(line)
        if line.strip() == "admin off" and not added:
            added.append(line[:len(line) - len(line.lstrip())] + LOOPBACK_BIND + "\n")
            out.append(added[0])
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(out)
    changes = [d for d in difflib.unified_diff(lines, out, n=0)
               if d[:1] in "+-" and not d.startswith(("+++ ", "--- "))]
    if not added:
        return False, "no `admin off` line to put the bind after"
    if changes != ["+" + added[0]]:
        return False, f"{len(changes)} changed lines (want exactly the one added line)"
    return True, f"the copy differs from the repo's Caddyfile by exactly one added line, '{LOOPBACK_BIND}'"


def run_streamed(cmd, env, out_path, cwd, timeout: float = 1800.0) -> int:
    """Run a test script; echo its output (indented, scrubbed) and keep it raw in out_path."""
    with open(out_path, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

        def pump():
            for line in proc.stdout:
                out.write(line)
                out.flush()
                print("    " + scrub(line.rstrip("\n")), flush=True)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(proc, os.path.basename(cmd[1]))
            return -1
        finally:
            reader.join(timeout=10)
        return proc.returncode


def live_signin_command(python: str, public: str, signin_report: str, protocol_report: str) -> list:
    """The live leg's test-signin.py command: no secret on it (OWNER_SECRET is in the environment)."""
    return [python, SIGNIN, "--live", "--base", public, "--run-protocol", "--protocol-python", python,
            "--protocol-args", "--report " + shlex.quote(protocol_report), "--report", signin_report]


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def s46_revoked(path: str):
    """(ok, detail): test-signin's --report shows S46 PASS with "its sign-in revoked: yes"."""
    report = read_json(path)
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        return False, "no test-signin --report file, or not its JSON shape"
    rows = [r for r in report["results"] if isinstance(r, dict) and r.get("id") == "S46"]
    if not rows:
        return False, "S46 is not in the report (did --live --run-protocol run it?)"
    row = rows[-1]
    revoked = "its sign-in revoked: yes" in str(row.get("detail", ""))
    return (row.get("status") == "PASS" and revoked and report.get("mode") == "live",
            f"report mode {report.get('mode')}; S46 {row.get('status')}; its sign-in revoked: {yn(revoked)}")


def protocol_version_passed(path: str):
    """(ok, detail): test-protocol's --report (written through --protocol-args) has server/version PASS."""
    report = read_json(path)
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        return False, "no test-protocol --report file (passed through --protocol-args), or not its JSON shape"
    rows = [r for r in report["results"] if isinstance(r, dict) and r.get("name") == "server/version"]
    if not rows:
        skipped = [s for s in report.get("skipped") or [] if s and s[0] == "server/version"]
        return False, "server/version " + ("skipped: " + str(skipped[0][1]) if skipped else "not in the report")
    return rows[0].get("ok") is True, f"server/version {'PASS' if rows[0].get('ok') else 'FAIL'}: {rows[0].get('detail')}"


class E2E:
    def __init__(self, args):
        self.a = args
        self.owner = fake_owner_secret()
        self.bearer = fake_bearer()
        SECRET_VALUES.extend([self.owner, self.bearer])
        self.run_dir = os.path.join(args.work, time.strftime("run-%Y%m%d-%H%M%S-") + str(os.getpid()))
        self.logs: list[tuple[str, str]] = []  # (label, path) for the grep
        self.secret_files: list[str] = []
        self.caddy_access: list[str] = []
        self.server_logs: dict[str, list[str]] = {}
        self.live_legs: set[str] = set()  # legs that ran test-signin --live (fewer log controls)
        self.launch = os.path.join(args.project, "ext", "launch.py")
        self.caddyfile = os.path.join(args.project, "Caddyfile")

    # --- environments ---------------------------------------------------------------

    def folder(self, *parts, mode=0o700) -> str:
        path = os.path.join(self.run_dir, *parts)
        os.makedirs(path, mode=mode, exist_ok=True)
        return path

    def base_env(self, home: str) -> dict:
        env = {"PATH": "/usr/bin:/bin", "HOME": home, "TMPDIR": self.folder("tmp"),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        if os.environ.get("PYTHONPYCACHEPREFIX"):
            env["PYTHONPYCACHEPREFIX"] = os.environ["PYTHONPYCACHEPREFIX"]
        return env

    def server_env(self, leg: str, public_url: str) -> dict:
        env = self.base_env(self.folder(leg, "home"))
        env.update({
            "GARMIN_MCP_TRANSPORT": "streamable-http",
            "GARMIN_MCP_HOST": "127.0.0.1",
            "GARMIN_MCP_PORT": str(self.a.server_port),
            "GARMIN_MCP_STATELESS_HTTP": "true",
            "REPORT_TZ": "UTC",
            "GARMIN_AUTH": "full",
            "PUBLIC_URL": public_url,
            "OWNER_SECRET": self.owner,
            "MCP_BEARER_TOKEN": self.bearer,
            "AUTH_DB": os.path.join(self.folder(leg), "auth.sqlite"),
            "GARMINTOKENS": self.folder(leg, "garmin-tokens"),  # empty: login fails, no network
            "GARMINTOKENS_BASE64": os.path.join(self.folder(leg), "garmin-tokens-base64-missing"),
        })
        return env

    def client_env(self, leg: str, owner: bool) -> dict:
        """Built from scratch, so no real secret from the caller's environment can leak in."""
        env = self.base_env(self.folder(leg, "client-home"))
        env["MCP_BEARER_TOKEN"] = self.bearer
        if owner:
            env["OWNER_SECRET"] = self.owner
        return env

    # --- steps ------------------------------------------------------------------------

    def check_config(self, tools: int) -> None:
        cwd = self.folder("check-config")
        good = self.server_env("check-config", f"http://127.0.0.1:{self.a.server_port}")
        p = subprocess.run([self.a.python, self.launch, "--check-config"], env=good, cwd=cwd,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        want = f"launch: config OK ({tools} read-only tools)"
        lines = p.stderr.splitlines()
        step("check-config/good-env", p.returncode == 0 and lines == [want],
             f"exit {p.returncode}; stderr is exactly '{want}': {yn(lines == [want])} ({len(lines)} stderr lines)")
        db = good["AUTH_DB"]
        mode = stat.S_IMODE(os.stat(db).st_mode) if os.path.exists(db) else None
        step("check-config/auth-db", mode == 0o600,
             f"AUTH_DB created: {yn(mode is not None)}; mode {oct(mode) if mode is not None else '-'} (want 0o600)")
        good_out = p.stdout + p.stderr

        bad = dict(good, **BAD_ENV, AUTH_DB=os.path.join(cwd, "bad-env.sqlite"))
        p = subprocess.run([self.a.python, self.launch, "--check-config"], env=bad, cwd=cwd,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        lines = p.stderr.splitlines()
        by_text = {PREFIX + text: cid for cid, text in BAD_ENV_LINES}
        seen = [by_text.get(line, "?") for line in lines]
        exact = lines == [PREFIX + text for _, text in BAD_ENV_LINES]
        step("check-config/bad-env", p.returncode == 1 and exact,
             f"exit {p.returncode}; lines: {' '.join(seen) or 'none'} (want exactly A2 A3 A5 A8)")
        step("check-config/bad-env-no-db", not os.path.exists(bad["AUTH_DB"]),
             f"AUTH_DB left alone when the settings are wrong: {yn(not os.path.exists(bad['AUTH_DB']))}")
        bad_out = p.stdout + p.stderr
        leaked = [label for label, hit in (
            ("good-env secret", any(f in good_out + bad_out for v in (self.owner, self.bearer) for f in secret_forms(v))),
            ("bad-env value", "bad token" in bad_out or "short" in bad_out),
        ) if hit]
        step("check-config/no-secret-text", not leaked, "no secret text in the output" if not leaked
             else "secret text in the output: " + ", ".join(leaked))

    def caddy_listens_on_loopback(self, caddyfile: str, cenv: dict, cwd: str, edge: int):
        """`caddy adapt` (no listener) of the copy: every server listens on 127.0.0.1:<edge> only."""
        try:
            p = subprocess.run([self.a.caddy, "adapt", "--config", caddyfile, "--adapter", "caddyfile"], env=cenv,
                               cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            servers = json.loads(p.stdout)["apps"]["http"]["servers"]
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as e:
            return False, f"caddy adapt of the copy failed: {type(e).__name__}"
        listens = sorted({address for server in servers.values() for address in server.get("listen", [])})
        want = [f"127.0.0.1:{edge}"]
        return listens == want, f"caddy adapt: listen {listens} (want {want})"

    def live_leg(self, name: str) -> None:
        """A fresh server, then test-signin.py --live --run-protocol as against a deployment."""
        leg_dir = self.folder(name)
        port = self.a.server_port
        public = f"http://127.0.0.1:{port}"
        if port_in_use(port):
            step(f"{name}/ports-free", False, f"something already answers on 127.0.0.1:{port}")
            return
        server_out, server_err = (os.path.join(leg_dir, f"server.{s}.log") for s in ("stdout", "stderr"))
        self.logs += [(f"{name}/server stdout", server_out), (f"{name}/server stderr", server_err)]
        self.server_logs[name] = [server_out, server_err]
        self.live_legs.add(name)
        server = start([self.a.python, self.launch], self.server_env(name, public), server_out, server_err, leg_dir)
        try:
            up, how = wait_up(port, server)
            step(f"{name}/server-up", up, f"launch.py on 127.0.0.1:{port} (pid {server.pid}): {how}")
            if not up:
                print("    last lines of its stderr (secrets scrubbed):\n" + tail(server_err), flush=True)
                return
            signin_report = os.path.join(leg_dir, "signin.json")
            protocol_report = os.path.join(leg_dir, "protocol.json")
            cmd = live_signin_command(self.a.python, public, signin_report, protocol_report)
            out = os.path.join(leg_dir, "test-signin.out")
            self.logs.append((f"{name}/test-signin output", out))
            info(f"{name}/test-signin", f"test-signin.py --live --base {public} --run-protocol --report ...")
            rc = run_streamed(cmd, self.client_env(name, owner=True), out, leg_dir)
            step(f"{name}/test-signin", rc == 0, f"exit {rc}")
            ok, how = s46_revoked(signin_report)
            step(f"{name}/s46-revoked", ok, how)
            ok, how = protocol_version_passed(protocol_report)
            step(f"{name}/server-version", ok, how)
        finally:
            stop(server, f"{name} server")

    def leg(self, name: str, via_caddy: bool) -> None:
        leg_dir = self.folder(name)
        port, edge = self.a.server_port, self.a.edge_port
        public = f"http://127.0.0.1:{edge if via_caddy else port}"
        busy = [p for p in ([port, edge] if via_caddy else [port]) if port_in_use(p)]
        if busy:
            step(f"{name}/ports-free", False, f"something already answers on 127.0.0.1:{', '.join(map(str, busy))}")
            return
        if via_caddy and not (self.a.caddy and os.access(self.a.caddy, os.X_OK)):
            step(f"{name}/caddy-binary", False, "caddy not found; pass --caddy PATH")
            return
        server_out, server_err = (os.path.join(leg_dir, f"server.{s}.log") for s in ("stdout", "stderr"))
        self.logs += [(f"{name}/server stdout", server_out), (f"{name}/server stderr", server_err)]
        self.server_logs[name] = [server_out, server_err]
        procs = []
        try:
            server = start([self.a.python, self.launch], self.server_env(name, public), server_out, server_err,
                           leg_dir)
            procs.append((server, f"{name} server"))
            up, how = wait_up(port, server)
            step(f"{name}/server-up", up, f"launch.py on 127.0.0.1:{port} (pid {server.pid}): {how}")
            if not up:
                print("    last lines of its stderr (secrets scrubbed):\n" + tail(server_err), flush=True)
                return
            if via_caddy:
                caddy_out, caddy_err = (os.path.join(leg_dir, f"caddy.{s}.log") for s in ("stdout", "stderr"))
                self.logs += [(f"{name}/caddy stdout", caddy_out), (f"{name}/caddy stderr", caddy_err)]
                self.caddy_access.append(caddy_out)
                cenv = {"PATH": "/usr/bin:/bin", "HOME": self.folder(name, "caddy-home"),
                        "XDG_DATA_HOME": self.folder(name, "caddy-data"),
                        "XDG_CONFIG_HOME": self.folder(name, "caddy-config"),
                        "PORT": str(edge), "GARMIN_MCP_PORT": str(port)}
                # The repo's Caddyfile listens on every interface; the copy adds
                # only `default_bind 127.0.0.1`, so the test edge stays on loopback.
                caddyfile = os.path.join(leg_dir, "Caddyfile")
                ok, how = loopback_caddyfile(self.caddyfile, caddyfile)
                step(f"{name}/caddyfile-copy", ok, how)
                if not ok:
                    return
                ok, how = self.caddy_listens_on_loopback(caddyfile, cenv, leg_dir, edge)
                step(f"{name}/caddy-loopback-only", ok, how)
                if not ok:
                    return
                caddy = start([self.a.caddy, "run", "--config", caddyfile, "--adapter", "caddyfile"], cenv,
                              caddy_out, caddy_err, leg_dir)
                procs.append((caddy, f"{name} caddy"))
                up, how = wait_up(edge, caddy)
                step(f"{name}/caddy-up", up, f"caddy on 127.0.0.1:{edge} (pid {caddy.pid}): {how}")
                if not up:
                    print("    last lines of its stderr:\n" + tail(caddy_err), flush=True)
                    return

            secrets_out = os.path.join(leg_dir, "secrets.json")
            self.secret_files.append(secrets_out)
            cmd = [self.a.python, SIGNIN, "--base", public, "--run-protocol", "--secrets-out", secrets_out,
                   "--protocol-python", self.a.python] + (["--via-caddy"] if via_caddy else [])
            out = os.path.join(leg_dir, "test-signin.out")
            self.logs.append((f"{name}/test-signin output", out))
            info(f"{name}/test-signin", f"test-signin.py --base {public}{' --via-caddy' if via_caddy else ''} --run-protocol")
            rc = run_streamed(cmd, self.client_env(name, owner=True), out, leg_dir)
            step(f"{name}/test-signin", rc == 0, f"exit {rc}")

            out = os.path.join(leg_dir, "test-protocol.out")
            self.logs.append((f"{name}/test-protocol output", out))
            info(f"{name}/test-protocol", f"test-protocol.py --url {public}/mcp with the static bearer")
            rc = run_streamed([self.a.python, PROTOCOL, "--url", public + "/mcp"], self.client_env(name, owner=False),
                              out, leg_dir)
            step(f"{name}/test-protocol-static-bearer", rc == 0, f"exit {rc}")

            if via_caddy:
                # A client set up with the wrong header name sends its token in
                # another header. Caddy's access log must drop every request
                # header, not only Authorization: the log grep below checks it.
                names = ("X-Api-Key", "X-Auth-Token", "Api-Key")
                try:
                    status = post_status(edge, "/mcp", {n: self.bearer for n in names})
                except (OSError, http.client.HTTPException) as e:
                    status = f"no answer ({type(e).__name__})"
                step(f"{name}/token-in-other-headers", status == 401,
                     f"POST /mcp with the static bearer in {', '.join(names)} only: HTTP {status} (want 401); "
                     "the log grep covers Caddy's access log")
        finally:
            for proc, label in reversed(procs):
                stop(proc, label)

    def grep(self) -> None:
        groups: dict[str, set] = {"owner_secret": secret_forms(self.owner), "static_bearer": secret_forms(self.bearer)}
        for path in self.secret_files:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                step("log-grep/secrets-file", False, f"cannot read {os.path.relpath(path, self.run_dir)}")
                continue
            for kind, values in data.items():
                if kind == "client_credentials":
                    for client_id, client_secret in values:
                        b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                        groups.setdefault("client_basic_auth", set()).update({b64, b64.rstrip("=")})
                    continue
                for v in values:
                    if isinstance(v, str) and len(v) >= 8:
                        groups.setdefault(kind, set()).update(secret_forms(v))
        counts = {k: len(v) for k, v in groups.items()}
        info("log-grep", f"forms searched per kind: {counts}")
        for label, path in self.logs:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            squeezed = re.sub(r"\s+", "", raw)  # catches values a log handler wrapped across lines
            hits = {}
            for kind, forms in groups.items():
                n = 0
                for form in forms:
                    found = raw.count(form)
                    if not found:
                        bare = re.sub(r"\s+", "", form)
                        found = squeezed.count(bare) if len(bare) >= 8 else 0
                    n += found
                if n:
                    hits[kind] = n
            step(f"log-grep/{label}", not hits,
                 f"{raw.count(chr(10))} lines: " + ("no secret value" if not hits else f"SECRET VALUES FOUND {hits}"))

        for path in self.caddy_access:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            except OSError:
                raw = ""
            n_auth = raw.count("/authorize?REDACTED")
            n_loc = raw.count('"Location"')
            step("control/caddy-access-log", n_auth >= 1 and n_loc == 0,
                 f"/authorize?REDACTED lines: {n_auth} (want 1+); Location fields: {n_loc} (want 0)")
        for leg, paths in self.server_logs.items():
            text = ""
            for path in paths:
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        text += f.read()
                except OSError:
                    pass
            live = leg in self.live_legs
            n = text.count("?[redacted]")
            step(f"control/{leg}-server-log", n >= 1, f"?[redacted] in the server log: {n} (want 1+)")
            # One record, one line: Railway shows each
            # line as its own entry and scripts/live-check.py reads them one by one.
            whole = re.search(r"(?m)^INFO [\w.]+: sign-in: on, issuer \S+, static bearer (?:on|off)$", text)
            step(f"control/{leg}-one-line-records", whole is not None,
                 "startup line 'sign-in: on, issuer ..., static bearer ...' whole on one line: "
                 + ("yes" if whole else "no (the log handler wraps records)"))
            # V1-1: each rate limit names the caller's own bucket, never Caddy's
            # address or the edge hop S47 puts in X-Forwarded-For. The SDK's log
            # handler wraps a record over several lines and puts "file.py:NN" at
            # the end of the first one, so join the lines and drop those first.
            joined = re.sub(r" [A-Za-z_][\w.]*\.py:\d+(?= )", "", re.sub(r"\s+", " ", text))
            named = RATE_LIMIT_LINE.findall(joined)
            unread = joined.count("rate limit:") - len(named)
            other = [ip for ip in named if not CALLER_ADDRESS.fullmatch(ip)]
            want_limits = 0 if live else 4
            step(f"control/{leg}-rate-limit-callers", len(named) >= want_limits and not other and not unread,
                 f"'rate limit:' lines: {len(named)} (want {want_limits}+"
                 f"{'' if live else ', S36-S38 and S47'}), {unread} unreadable; naming a "
                 f"caller's address: {len(named) - len(other)}; naming another address: {len(other)} "
                 f"{sorted(set(other))}")
            # The wrapper logs one line per failed tool call. S45 calls
            # get_activities without a Garmin login; test-protocol.py sends an
            # impossible date (two runs, two eras); none may be an internal error.
            n_login = joined.count(TOOL_ERROR_LOGIN)
            n_bad = joined.count(TOOL_ERROR_BAD_INPUT)
            n_internal = joined.count(TOOL_ERROR_INTERNAL)
            want_login = 0 if live else 1
            step(f"control/{leg}-tool-error-log", n_login >= want_login and n_bad >= 2 and n_internal == 0,
                 f"'{TOOL_ERROR_LOGIN}' lines: {n_login} (want {want_login}+"
                 f"{'' if live else ', S45'}); '{TOOL_ERROR_BAD_INPUT}' lines: "
                 f"{n_bad} (want 2+, test-protocol's bad date); '{TOOL_ERROR_INTERNAL}': {n_internal} (want 0)")

    def run(self) -> int:
        os.makedirs(self.run_dir, mode=0o700)
        info("setup", f"run folder {self.run_dir}; python {self.a.python}; project {self.a.project}; "
                      f"legs {','.join(self.a.legs)}; ports {self.a.server_port}/{self.a.edge_port}")
        tools = tool_count(self.a.project)
        if tools is None:
            step("setup/enabled-tools", False, "cannot read config/enabled-tools.txt under --project")
            return 1
        try:
            self.check_config(tools)
            for leg in self.a.legs:
                if leg == "live":
                    self.live_leg(leg)
                else:
                    self.leg(leg, via_caddy=leg == "caddy")
        finally:
            self.grep()
        failed = [n for n, ok in results if not ok]
        print(f"\ne2e-local: {len(results) - len(failed)}/{len(results)} steps passed"
              + (f"; FAILED: {', '.join(failed)}" if failed else "; logs clean") + f"\nrun folder: {self.run_dir}",
              flush=True)
        return 1 if failed else 0


LEGS = ("direct", "caddy", "live")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", required=True, help="scratch folder; each run makes a new run-* folder inside")
    p.add_argument("--server-port", type=int, default=8340, help="the Python server's port (default 8340)")
    p.add_argument("--edge-port", type=int, default=8341, help="Caddy's port in the caddy leg (default 8341)")
    p.add_argument("--caddy", default=shutil.which("caddy") or "/opt/homebrew/bin/caddy", help="the caddy binary")
    p.add_argument("--legs", default=",".join(LEGS),
                   help="comma-separated: direct, caddy, live (default all three)")
    p.add_argument("--python", default=sys.executable,
                   help="python with mcp 2.3.0 and upstream, for the server and the test scripts (default: this one)")
    p.add_argument("--project", default=REPO,
                   help="the tree whose ext/launch.py, config/ and Caddyfile are used (default: this repo)")
    return p


def parse_args(argv=None) -> argparse.Namespace:
    p = build_parser()
    args = p.parse_args(argv)
    args.legs = [leg.strip() for leg in args.legs.split(",") if leg.strip()]
    if not args.legs or any(leg not in LEGS for leg in args.legs):
        p.error("--legs takes one or more of direct, caddy, live")
    if args.server_port == args.edge_port:
        p.error("--server-port and --edge-port must differ")
    args.work = os.path.abspath(args.work)
    args.project = os.path.abspath(args.project)
    return args


def main() -> int:
    args = parse_args()
    os.makedirs(args.work, mode=0o700, exist_ok=True)

    def on_term(signum, frame):  # stop the children (finally blocks) on SIGTERM too
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    try:
        return E2E(args).run()
    except KeyboardInterrupt:
        print("e2e-local: interrupted; every process it started was stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
