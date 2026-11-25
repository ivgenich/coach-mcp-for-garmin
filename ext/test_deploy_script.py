"""Behaviour tests for scripts/deploy.sh against a fake Railway CLI.

Stdlib only; no network, no port, no real `railway`. Each case copies
deploy.sh into a fresh temporary folder and runs it with a fake `railway`
first on PATH. The fake logs every call, and for values sent on stdin it
logs only a sha1 prefix and the length, never the value. Cases that type the
Garmin key run the script in a pseudo-terminal and type 0.3 s after the
prompt, as a person would (text typed before `read -s` turns echo off is
echoed by the terminal itself).

Every key, secret and token here is built at run time.
"""

import hashlib
import json
import os
import re
import secrets
import select
import shutil
import subprocess
import tempfile
import time
import unittest

try:
    import pty
except ImportError:  # not on Windows
    pty = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
DEPLOY = os.path.join(ROOT, "scripts", "deploy.sh")
BASH = shutil.which("bash")
HAVE_PTY = pty is not None and os.name == "posix" and BASH is not None

# The fake CLI, a python3 script. It keeps the project in a state file and the
# folder links in $HOME/.railway/config.json, as the real CLI does. The
# _Sandbox switches set the starting point: FAKE_UNLINKED=1 (no project and no
# link), FAKE_LINK_PARENT=1 (the link belongs to the parent folder),
# FAKE_LINKED_NAME=<name> (the linked service has another name),
# FAKE_NO_SERVICE_LINK=1 (the project is linked, its service is not),
# FAKE_NO_SERVICE=1 (the project has no service yet), FAKE_NO_VOLUME=1,
# FAKE_LINK_GONE=1 (the linked service is no longer in the project),
# FAKE_OTHER_VOLUME=1 (only another service has a /data volume), FAKE_HC=1
# (the service checks /healthz), FAKE_HC_OTHER=1 (only another service does).
# FAKE_EDIT_NOOP=1: the health check edit applies nothing and exits 0;
# FAKE_SSH_FAIL=1: railway ssh exits 1.
FAKE_RAILWAY = r"""#!/usr/bin/env python3
import hashlib, json, os, sys

LOG = os.environ["FAKE_LOG"]
STATE = LOG + ".state"
CONF = os.path.join(os.environ["HOME"], ".railway", "config.json")


def log(line):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


state, conf = load(STATE), load(CONF) if os.path.exists(CONF) else {}
args = sys.argv[1:]
cwd = os.getcwd()


def flag(name):
    return args[args.index(name) + 1] if name in args else None


def by(key, value):
    return next((s for s in state["services"] if s[key] == value), None)


def link():
    return (conf.get("projects") or {}).get(cwd)


head = " ".join(args[:2])
if args[0] == "--version":
    print("railway 5.70.0")
elif args[0] == "whoami":
    print("Logged in as test")
elif head == "status --json":
    if not state["project"]:
        sys.exit("No linked project")
    print(json.dumps({"id": state["project"]["id"], "name": state["project"]["name"], "workspace": {"name": "w"},
                      "services": {"edges": [{"node": s} for s in state["services"]]}}, indent=2))
elif args[0] == "init":
    state["project"] = {"id": "p-new", "name": flag("--name")}
    conf.setdefault("projects", {})[cwd] = {"projectPath": cwd, "project": "p-new"}
    log("init" + "".join(" [%s]" % a for a in args[1:]))
elif args[0] == "add":
    name = flag("--service")
    state["services"].append({"id": "s-" + name, "name": name})
    link()["service"] = "s-" + name
    log(" ".join(args))
elif head == "service link":
    link()["service"] = by("name", args[2])["id"]
    log(" ".join(args))
elif head == "variable list":
    if flag("--service") and not by("name", flag("--service")):
        sys.exit("Service not found")
    if "--kv" in args:
        with open(os.environ["FAKE_VARS"], encoding="utf-8") as f:
            sys.stdout.write(f.read())
elif head == "variable set":
    if "--stdin" in args:
        v = sys.stdin.read()
        log("variable set %s --stdin sha=%s len=%d svc=%s"
            % (args[2], hashlib.sha1(v.encode()).hexdigest()[:12], len(v), flag("--service")))
    else:
        log(" ".join(args))
elif head == "volume list":
    print(json.dumps({"volumes": [{"mountPath": m, "serviceName": n} for n, m in state["volumes"]]}))
elif args[0] == "volume":
    log(" ".join(args))
elif args[0] == "domain":
    log(" ".join(args))
    print(json.dumps({"domain": "https://test-app.up.railway.app"}))
elif head == "environment config":
    print(json.dumps({"services": {by("name", n)["id"]: {"deploy": {"healthcheckPath": "/healthz"}}
                                   for n in state["hc"]}}))
elif head == "environment edit":
    log(" ".join(args))
    if os.environ.get("FAKE_EDIT_NOOP"):
        print('{"committed":false,"message":"No changes to apply","staged":false}')
    else:
        state["hc"].append(flag("--service-config"))
        print('{"committed":true,"staged":true}')
elif args[0] in ("up", "ssh"):
    log(" ".join(args))
    if args[0] == "ssh" and os.environ.get("FAKE_SSH_FAIL"):
        sys.exit(1)
else:
    log("UNEXPECTED " + " ".join(args))
    sys.exit(3)
save(STATE, state)
if conf:
    save(CONF, conf)
"""

SERVICE = "coach-mcp-for-garmin"

PROMPT = b"Garmin Personal API key"
MODE_PROMPT = b"Garmin mode, full or key"
TZ_PROMPT = b"Time zone, an IANA name"
LOGIN_PROMPT = b"Run the Garmin login now?"
TZ = {"REPORT_TZ": "Europe/London"}


def sha12(text):
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def fake_secret():
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(6))


def fake_key():
    # Shaped like a key, no whitespace, built fresh for each run.
    return "k" + secrets.token_urlsafe(18) + "Z"


class _Sandbox:
    """A temporary copy of deploy.sh with the fake CLI on PATH."""

    def __init__(self, vars_text, extra):
        self.root = tempfile.mkdtemp(prefix="deploy-test-")
        os.makedirs(os.path.join(self.root, "scripts"))
        self.script = os.path.join(self.root, "scripts", "deploy.sh")
        shutil.copy(DEPLOY, self.script)
        # The tool lists deploy.sh reads (key mode checks GARMIN_ENABLED_TOOLS against them).
        shutil.copytree(os.path.join(ROOT, "config"), os.path.join(self.root, "config"))
        bindir = os.path.join(self.root, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "railway")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_RAILWAY)
        os.chmod(fake, 0o755)
        # A curl that never reaches the network: /readyz answers 200 unless FAKE_READYZ=fail.
        curl = os.path.join(bindir, "curl")
        with open(curl, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho "curl $*" >>"$FAKE_LOG"\n[ "$FAKE_READYZ" != fail ]\n')
        os.chmod(curl, 0o755)
        with open(os.path.join(self.root, "vars"), "w", encoding="utf-8") as f:
            f.write(vars_text)
        self.log_path = os.path.join(self.root, "log")
        self._start(extra)
        self.env = {
            "PATH": bindir + os.pathsep + "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": self.root,
            "FAKE_LOG": self.log_path,
            "FAKE_VARS": os.path.join(self.root, "vars"),
            "TERM": "dumb",
            "LC_ALL": "C",
        }
        self.env.update(extra)

    def _start(self, extra):
        """The project and the folder link the fake CLI starts from (see FAKE_RAILWAY)."""
        def on(name):
            return bool(extra.get(name))
        here = os.path.realpath(self.root)
        name = extra.get("FAKE_LINKED_NAME") or extra.get("DEPLOY_SERVICE") or SERVICE
        services = [] if on("FAKE_NO_SERVICE") else [{"id": "s1", "name": name}]
        if extra.get("FAKE_LINKED_NAME") and extra.get("DEPLOY_SERVICE"):
            services.append({"id": "s2", "name": extra["DEPLOY_SERVICE"]})
        services.append({"id": "s9", "name": "other-app"})
        volumes = [] if on("FAKE_NO_VOLUME") else [["other-app", "/data"]] if on("FAKE_OTHER_VOLUME") else [[name, "/data"]]
        hc = ["other-app"] if on("FAKE_HC_OTHER") else [name] if on("FAKE_HC") else []
        state = {"project": {"id": "p1", "name": "test-project"}, "services": services, "volumes": volumes, "hc": hc}
        conf = {"projects": {here: {"projectPath": here, "project": "p1", "service": "s1"}},
                "user": {"token": "not-a-real-token"}}
        if on("FAKE_LINK_PARENT"):
            parent = os.path.dirname(here)
            conf["projects"] = {parent: dict(conf["projects"][here], projectPath=parent)}
        if on("FAKE_NO_SERVICE_LINK") or on("FAKE_NO_SERVICE"):
            del conf["projects"][here]["service"]
        if on("FAKE_LINK_GONE"):
            conf["projects"][here]["service"] = "s-gone"
        if on("FAKE_UNLINKED"):
            state = {"project": None, "services": [], "volumes": [], "hc": []}
            conf = {"user": conf["user"]}
        with open(self.log_path + ".state", "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.makedirs(os.path.join(self.root, ".railway"))
        with open(os.path.join(self.root, ".railway", "config.json"), "w", encoding="utf-8") as f:
            json.dump(conf, f)

    def log(self):
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, encoding="utf-8") as f:
            return f.read()

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def run(vars_text, extra, args=()):
    box = _Sandbox(vars_text, extra)
    try:
        p = subprocess.run([BASH, box.script, *args], env=box.env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr, box.log()
    finally:
        box.close()


def run_pty(vars_text, extra, typed=None, args=(), answers=()):
    """Run deploy.sh with a pseudo-terminal as stdin, stdout and stderr. For
    each (prompt, text) of `answers`, in order, type text and Enter 0.3 s after
    the prompt shows; `typed` answers the key prompt."""
    if typed is not None:
        answers = [(PROMPT, typed)] + list(answers)
    box = _Sandbox(vars_text, extra)
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([BASH, box.script, *args], env=box.env, stdin=slave, stdout=slave,
                                stderr=slave, close_fds=True, start_new_session=True)
        os.close(slave)
        slave = None
        out = b""
        todo = list(answers)
        seen = 0
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # EIO on Linux once the child side is closed
                    break
                if not chunk:
                    break
                out += chunk
                if todo and todo[0][0] in out[seen:]:
                    prompt, text = todo.pop(0)
                    seen = out.index(prompt, seen) + len(prompt)
                    time.sleep(0.3)
                    os.write(master, text.encode() + b"\n")
            elif proc.poll() is not None:
                break
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        return rc, out.decode(errors="replace"), box.log()
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)
        box.close()


@unittest.skipUnless(BASH, "needs bash on PATH")
class DeployScriptFakeRailway(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.secret_line = "OWNER_SECRET=" + fake_secret() + "\n"
        self.old_key = fake_key()
        self.existing_key = ("GARMIN_AUTH=key\nREPORT_TZ=Europe/London\n" + self.secret_line
                             + "GARMIN_PERSONAL_API_KEY=" + self.old_key + "\n")
        self.existing_full = "GARMIN_AUTH=full\nREPORT_TZ=Europe/London\n" + self.secret_line

    def check(self, cond, out, log):
        self.assertTrue(cond, "output:\n" + out[-1500:] + "\nlog:\n" + log[-1500:])

    def test_01_no_garmin_auth_stops(self):
        rc, out, log = run("", dict(TZ))
        self.check(rc == 1 and "GARMIN_AUTH is required" in out and "variable set" not in log, out, log)

    def test_02_bad_garmin_auth_stops_before_any_call(self):
        junk = "fu" + "lll"
        rc, out, log = run("", {"GARMIN_AUTH": junk, **TZ})
        self.check(rc == 1 and "must be full or key" in out and log == "" and junk not in out, out, log)

    def test_03_full_first_deploy(self):
        rc, out, log = run("", {"GARMIN_AUTH": "full", **TZ})
        self.check(rc == 0 and "variable set --skip-deploys GARMIN_AUTH=full" in log
                   and "variable set OWNER_SECRET --stdin" in log and "GARMIN_PERSONAL_API_KEY" not in log
                   and "deploy.healthcheckPath /healthz" in log and "up --ci" in log
                   and "garmin-mcp-auth --token-path /data/tokens" in out and "GARMINTOKENS=" not in log
                   and "GARMIN_MCP_STATELESS_HTTP" not in log, out, log)
        # 3b: the health check step comes before the build.
        self.check(log.find("deploy.healthcheckPath") < log.find("up --ci"), out, log)
        self.check("Set to /healthz." in out and "Could not set it" not in out, out, log)

    def test_03c_health_check_edit_that_applies_nothing_is_not_reported_as_set(self):
        # Railway CLI 5.70 without a terminal: "No changes to apply", exit 0.
        rc, out, log = run("", {"GARMIN_AUTH": "full", **TZ, "FAKE_EDIT_NOOP": "1"})
        self.check(rc == 0 and "deploy.healthcheckPath /healthz" in log and "Set to /healthz." not in out
                   and "Healthcheck Path: /healthz" in out and "up --ci" in log, out, log)

    def test_04_key_mode_without_terminal_stops(self):
        rc, out, log = run("", {"GARMIN_AUTH": "key", **TZ})
        self.check(rc == 1 and "needs GARMIN_PERSONAL_API_KEY" in out and "variable set" not in log, out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_05_key_mode_first_deploy_in_terminal(self):
        key = fake_key()
        rc, out, log = run_pty("", {"GARMIN_AUTH": "key", **TZ}, key)
        self.check(rc == 0 and f"GARMIN_PERSONAL_API_KEY --stdin sha={sha12(key)} len={len(key)}" in log
                   and key not in out and key not in log and "key mode needs no Garmin login" in out
                   and "garmin-mcp-auth" not in out, out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_06_key_with_inner_space_refused(self):
        rc, out, log = run_pty("", {"GARMIN_AUTH": "key", **TZ}, "has space")
        self.check(rc == 1 and "has spaces in it" in out and "variable set" not in log, out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_06b_key_under_20_characters_refused(self):
        """A paste that did not land (a 1-character key) is refused, not stored and deployed."""
        for short in ("v", "x" * 19):
            with self.subTest(len(short)):
                rc, out, log = run_pty("", {"GARMIN_AUTH": "key", **TZ}, short)
                self.check(rc == 1 and f"too short ({len(short)} characters" in out and "Nothing was deployed" in out
                           and "variable set" not in log and "railway up" not in log, out, log)
        key = "x" * 20
        rc, out, log = run_pty("", {"GARMIN_AUTH": "key", **TZ}, key)
        self.check(rc == 0 and f"GARMIN_PERSONAL_API_KEY --stdin sha={sha12(key)} len=20" in log, out, log)

    def test_07_key_mode_redeploy_keeps_key(self):
        rc, out, log = run(self.existing_key, {"FAKE_HC": "1"})
        self.check(rc == 0 and "GARMIN_PERSONAL_API_KEY already set" in out
                   and "GARMIN_PERSONAL_API_KEY --stdin" not in log and "environment edit" not in log
                   and self.old_key not in out, out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_08_set_garmin_key_replaces_key(self):
        key = fake_key()
        rc, out, log = run_pty(self.existing_key, {}, key, args=("--set-garmin-key",))
        self.check(rc == 0 and f"GARMIN_PERSONAL_API_KEY --stdin sha={sha12(key)}" in log
                   and "delete the old key" in out and key not in out, out, log)

    def test_09_set_garmin_key_in_full_mode_stops(self):
        rc, out, log = run(self.existing_full, {}, args=("--set-garmin-key",))
        self.check(rc == 1 and "only for GARMIN_AUTH=key" in out and "variable set" not in log, out, log)

    def test_10_switch_full_to_key_without_terminal_stops(self):
        rc, out, log = run(self.existing_full, {"GARMIN_AUTH": "key"})
        self.check(rc == 1 and "needs GARMIN_PERSONAL_API_KEY" in out and "variable set" not in log, out, log)

    def test_11_bad_garmin_auth_on_railway_stops(self):
        junk = "bog" + "us"
        rc, out, log = run("GARMIN_AUTH=" + junk + "\nREPORT_TZ=Europe/London\n" + self.secret_line, {})
        self.check(rc == 1 and "neither full nor key" in out and "variable set" not in log
                   and junk not in out, out, log)

    def test_12_spaces_and_case_ignored_stored_lowercase(self):
        rc, out, log = run("", {"GARMIN_AUTH": " Full ", **TZ})
        self.check(rc == 0 and "variable set --skip-deploys GARMIN_AUTH=full" in log, out, log)

    def test_13_key_on_railway_is_key_mode_and_kept(self):
        vars_text = self.existing_key.replace("GARMIN_AUTH=key", "GARMIN_AUTH=Key")
        rc, out, log = run(vars_text, {"FAKE_HC": "1"})
        self.check(rc == 0 and "GARMIN_AUTH already set to key" in out and "GARMIN_AUTH=" not in log, out, log)

    def test_14_full_mode_with_key_on_railway_stops(self):
        vars_text = self.existing_full + "GARMIN_PERSONAL_API_KEY=" + self.old_key + "\n"
        rc, out, log = run(vars_text, {})
        self.check(rc == 1 and "railway variable delete GARMIN_PERSONAL_API_KEY" in out
                   and "variable set" not in log and "up --ci" not in log and self.old_key not in out, out, log)

    def test_15_switch_key_to_full_while_key_set_stops(self):
        rc, out, log = run(self.existing_key, {"GARMIN_AUTH": "full"})
        self.check(rc == 1 and "railway variable delete GARMIN_PERSONAL_API_KEY" in out
                   and "variable set" not in log, out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_16_key_surrounding_spaces_dropped(self):
        key = fake_key()
        rc, out, log = run_pty("", {"GARMIN_AUTH": "key", **TZ}, "  " + key + " ")
        self.check(rc == 0 and f"GARMIN_PERSONAL_API_KEY --stdin sha={sha12(key)} len={len(key)}" in log,
                   out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_17_key_over_512_characters_refused(self):
        rc, out, log = run_pty("", {"GARMIN_AUTH": "key", **TZ}, "k" * 513)
        self.check(rc == 1 and "too long" in out and "variable set" not in log, out, log)

    def test_18_empty_key_variable_counts_as_unset_in_full_mode(self):
        rc, out, log = run(self.existing_full + "GARMIN_PERSONAL_API_KEY=\n", {"FAKE_HC": "1"})
        self.check(rc == 0 and "up --ci" in log, out, log)

    def test_19_workspace_on_first_deploy_is_one_argument(self):
        workspace = "My " + "Workspace"
        rc, out, log = run("", {"GARMIN_AUTH": "full", **TZ, "FAKE_UNLINKED": "1",
                                "DEPLOY_WORKSPACE": workspace})
        self.check(rc == 0 and f"init [--name] [coach-mcp-for-garmin] [--workspace] [{workspace}]" in log
                   and workspace not in out, out, log)

    def test_20_no_workspace_plain_init(self):
        rc, out, log = run("", {"GARMIN_AUTH": "full", **TZ, "FAKE_UNLINKED": "1"})
        self.check(rc == 0 and "init [--name] [coach-mcp-for-garmin]\n" in log and "--workspace" not in log,
                   out, log)

    def test_20b_first_deploy_adds_the_service(self):
        # railway init makes a project with no service: the script adds it.
        rc, out, log = run("", {"GARMIN_AUTH": "full", **TZ, "FAKE_UNLINKED": "1"})
        self.check(rc == 0 and "add --service coach-mcp-for-garmin\n" in log and "up --ci" in log
                   and "Could not read" not in out, out, log)

    def test_20c_linked_project_without_the_service_adds_it(self):
        rc, out, log = run("", {"GARMIN_AUTH": "full", **TZ, "FAKE_NO_SERVICE": "1"})
        self.check(rc == 0 and "init" not in log and "add --service coach-mcp-for-garmin\n" in log
                   and "up --ci" in log, out, log)

    def test_21_linked_folder_workspace_unused(self):
        workspace = "ws-" + secrets.token_hex(4)
        rc, out, log = run(self.existing_full, {"DEPLOY_WORKSPACE": workspace})
        self.check(rc == 0 and "init" not in log and "DEPLOY_WORKSPACE is not used" in out
                   and workspace not in out, out, log)

    def test_22_missing_report_tz_hint_keeps_mode(self):
        rc, out, log = run("", {"GARMIN_AUTH": " KEY "})
        self.check(rc == 1 and "GARMIN_AUTH=key REPORT_TZ=Europe/London ./scripts/deploy.sh" in out
                   and "variable set" not in log, out, log)

    def test_22b_junk_garmin_auth_on_railway_never_shown_on_switch(self):
        # A key pasted into GARMIN_AUTH on Railway, then a re-run with key mode:
        # the old value is not shown in any letter case.
        junk = "Pk" + secrets.token_urlsafe(12) + "Qz"
        vars_text = self.existing_key.replace("GARMIN_AUTH=key", "GARMIN_AUTH=" + junk)
        rc, out, log = run(vars_text, {"GARMIN_AUTH": "key", "FAKE_HC": "1"})
        self.check(rc == 0 and junk.lower() not in out.lower() and "GARMIN_AUTH changes to key" in out
                   and "not shown" in out and "/data/tokens" not in out
                   and "variable set --skip-deploys GARMIN_AUTH=key" in log, out, log)

    def test_22c_switch_full_to_key_names_both_modes(self):
        rc, out, log = run(self.existing_full + "GARMIN_PERSONAL_API_KEY=" + self.old_key + "\n",
                           {"GARMIN_AUTH": "key", "FAKE_HC": "1"})
        self.check(rc == 0 and "GARMIN_AUTH changes from full to key." in out
                   and "railway ssh -- rm -rf /data/tokens" in out, out, log)

    def test_22d_all_space_key_variable_counts_as_unset_in_full_mode(self):
        rc, out, log = run(self.existing_full + "GARMIN_PERSONAL_API_KEY=   \n", {"FAKE_HC": "1"})
        self.check(rc == 0 and "up --ci" in log and "does not start while it is set" not in out, out, log)

    def test_22e_all_space_key_variable_in_key_mode_without_terminal_stops(self):
        vars_text = self.existing_key.replace("GARMIN_PERSONAL_API_KEY=" + self.old_key,
                                              "GARMIN_PERSONAL_API_KEY=" + " \t ")
        rc, out, log = run(vars_text, {"FAKE_HC": "1"})
        self.check(rc == 1 and "GARMIN_AUTH=key needs GARMIN_PERSONAL_API_KEY" in out
                   and "variable set" not in log and "up --ci" not in log, out, log)

    def test_22f_all_space_report_tz_on_railway_counts_as_unset(self):
        vars_text = self.existing_full.replace("REPORT_TZ=Europe/London", "REPORT_TZ=   ")
        rc, out, log = run(vars_text, {"FAKE_HC": "1"})
        self.check(rc == 1 and "REPORT_TZ is required" in out and "variable set" not in log, out, log)

    # Key mode serves only config/enabled-tools-key.txt, and the server refuses to start
    # when GARMIN_ENABLED_TOOLS names another tool, so deploy.sh stops before it changes anything.
    FULL_ONLY = ("get_activity_splits", "get_cycling_ftp")

    def test_22g_key_mode_stops_on_tools_key_mode_lacks(self):
        enabled = " Get_Activity_Splits , get_stats,,get_cycling_ftp ,get_activity_splits"
        rc, out, log = run(self.existing_key + "GARMIN_ENABLED_TOOLS=" + enabled + "\n", {"FAKE_HC": "1"})
        self.check(rc == 1 and "names tools that GARMIN_AUTH=key does not have: get_activity_splits, get_cycling_ftp."
                   in out and "config/enabled-tools-key.txt" in out and "keep GARMIN_AUTH=full" in out
                   and "Nothing was deployed" in out and "get_stats" not in out
                   and "variable set" not in log and "up --ci" not in log and "environment edit" not in log, out, log)

    def test_22h_switch_full_to_key_stops_before_the_key_prompt(self):
        vars_text = self.existing_full + "GARMIN_ENABLED_TOOLS=get_stats,get_cycling_ftp\n"
        rc, out, log = run(vars_text, {"GARMIN_AUTH": "key"})
        self.check(rc == 1 and "does not have: get_cycling_ftp." in out
                   and "needs GARMIN_PERSONAL_API_KEY" not in out and "variable set" not in log
                   and "up --ci" not in log, out, log)

    def test_22i_an_entry_that_is_no_tool_name_is_never_shown(self):
        # Shaped like a pasted key, never like a tool name (the "-" sees to that).
        junk = "Pk-" + secrets.token_hex(12) + "Qz"
        rc, out, log = run(self.existing_key + "GARMIN_ENABLED_TOOLS=get_stats," + junk + "\n", {"FAKE_HC": "1"})
        self.check(rc == 1 and "an entry that is not a tool name (it is not shown)" in out
                   and junk.lower() not in out.lower() and "does not have:" not in out
                   and "variable set" not in log and "up --ci" not in log, out, log)

    def test_22i2_a_mixed_case_entry_without_a_dash_is_never_shown(self):
        # Lower-cased, this would have the shape of a tool name.
        junk = "kP" + secrets.token_hex(8) + "_tY" + secrets.token_hex(8)
        rc, out, log = run(self.existing_key + "GARMIN_ENABLED_TOOLS=get_stats," + junk + "\n", {"FAKE_HC": "1"})
        self.check(rc == 1 and "an entry that is not a tool name (it is not shown)" in out
                   and junk.lower() not in out.lower() and "variable set" not in log and "up --ci" not in log,
                   out, log)

    def test_22i3_a_list_of_only_commas_is_refused(self):
        # The server refuses it ("names no tools"), so deploy.sh stops first.
        for value in (",", " , ,"):
            with self.subTest(value=value):
                rc, out, log = run(self.existing_key + "GARMIN_ENABLED_TOOLS=" + value + "\n", {"FAKE_HC": "1"})
                self.check(rc == 1 and "names no tools" in out
                           and "variable set" not in log and "up --ci" not in log, out, log)

    def test_22j_key_mode_tools_only_deploys_and_names_the_key_list(self):
        rc, out, log = run(self.existing_key + "GARMIN_ENABLED_TOOLS=get_stats, GET_DAILY_STATUS\n", {"FAKE_HC": "1"})
        self.check(rc == 0 and "up --ci" in log
                   and "it narrows config/enabled-tools-key.txt (key mode; every name must be in that file)" in out,
                   out, log)

    def test_22k_full_mode_keeps_its_tools_and_names_the_full_list(self):
        enabled = ",".join(self.FULL_ONLY)
        rc, out, log = run(self.existing_full + "GARMIN_ENABLED_TOOLS=" + enabled + "\n", {"FAKE_HC": "1"})
        self.check(rc == 0 and "up --ci" in log and "does not have" not in out
                   and "it narrows config/enabled-tools.txt (every name must be in that file)" in out, out, log)


    # The linked project and service: printed first, checked before anything changes.
    def test_25_prints_the_linked_project_and_service(self):
        rc, out, log = run(self.existing_full, {"FAKE_HC": "1"})
        self.check(rc == 0 and "Linked project: test-project" in out
                   and "Linked service: coach-mcp-for-garmin" in out
                   and out.index("Linked service:") < out.index("Volume at /data"), out, log)

    def test_26_a_link_from_a_parent_folder_is_refused(self):
        rc, out, log = run(self.existing_full, {"FAKE_LINK_PARENT": "1"})
        self.check(rc == 1 and "has no Railway link of its own" in out and "Nothing was changed" in out
                   and "railway init --name coach-mcp-for-garmin" in out and log == "", out, log)

    def test_27_another_linked_service_is_refused(self):
        rc, out, log = run(self.existing_full, {"FAKE_LINKED_NAME": "my-coach"})
        self.check(rc == 1 and "linked to the service 'my-coach'" in out and "DEPLOY_SERVICE='my-coach'" in out
                   and "railway service link coach-mcp-for-garmin" in out and log == "", out, log)
        # Named, it deploys that service, and every call that changes it names it.
        rc, out, log = run(self.existing_full, {"FAKE_LINKED_NAME": "my-coach", "DEPLOY_SERVICE": "my-coach",
                                                "FAKE_HC": "1"})
        self.check(rc == 0 and "up --ci --service my-coach" in log, out, log)

    def test_27b_a_linked_service_missing_from_the_project_is_refused(self):
        rc, out, log = run(self.existing_full, {"FAKE_LINK_GONE": "1"})
        self.check(rc == 1 and "no longer in the project" in out and "Nothing was changed" in out
                   and log == "", out, log)

    def test_28_an_unlinked_existing_service_is_linked_not_added_again(self):
        rc, out, log = run(self.existing_full, {"FAKE_NO_SERVICE_LINK": "1", "FAKE_HC": "1"})
        self.check(rc == 0 and "service link coach-mcp-for-garmin" in log and "add " not in log
                   and "Linked service: none" in out and "up --ci" in log, out, log)

    def test_29_every_change_names_the_service(self):
        rc, out, log = run("", {"GARMIN_AUTH": "full", **TZ, "FAKE_NO_VOLUME": "1"})
        lines = [line for line in log.splitlines() if line.startswith(("variable set", "up", "domain", "volume"))]
        self.check(rc == 0 and lines, out, log)
        for line in lines:
            with self.subTest(line=line):
                self.assertTrue("--service coach-mcp-for-garmin" in line or "svc=coach-mcp-for-garmin" in line
                                or "--service s1 add --mount-path /data" in line, line)

    def test_30_another_services_volume_or_health_check_does_not_count(self):
        rc, out, log = run(self.existing_full, {"FAKE_OTHER_VOLUME": "1", "FAKE_HC_OTHER": "1"})
        self.check(rc == 0 and "volume --service s1 add --mount-path /data" in log
                   and "environment edit --service-config coach-mcp-for-garmin" in log, out, log)
        rc, out, log = run(self.existing_full, {"FAKE_HC": "1"})
        self.check(rc == 0 and "Already attached." in out and "Already /healthz." in out
                   and "volume" not in log and "environment edit" not in log, out, log)

    # A first deploy checks its inputs before it creates anything.
    def test_31_first_deploy_without_inputs_creates_nothing(self):
        for extra, needle in (({**TZ}, "GARMIN_AUTH is required"), ({"GARMIN_AUTH": "full"}, "REPORT_TZ is required"),
                              ({"GARMIN_AUTH": "key", **TZ}, "needs GARMIN_PERSONAL_API_KEY")):
            for start in ("FAKE_UNLINKED", "FAKE_NO_SERVICE"):
                with self.subTest(extra=extra, start=start):
                    rc, out, log = run("", {**extra, start: "1"})
                    self.check(rc == 1 and needle in out and "Nothing was changed" in out and log == "", out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_32_first_deploy_in_a_terminal_asks_for_the_mode_and_zone(self):
        rc, out, log = run_pty("", {"FAKE_UNLINKED": "1"}, answers=[
            (MODE_PROMPT, "fu" + "lll"), (MODE_PROMPT, " Full "), (TZ_PROMPT, "Nowhere/Atlantis"),
            (TZ_PROMPT, "Europe/London"), (LOGIN_PROMPT, "n")])
        self.check(rc == 0 and "Type full or key." in out and "not a time zone name" in out
                   and out.count("Nowhere/Atlantis") == 1 and out.count("fulll") == 1  # the terminal's echo only
                   and log.index("variable set --skip-deploys GARMIN_AUTH=full") > log.index("init")
                   and "REPORT_TZ=Europe/London" in log and "ssh" not in log
                   and "garmin-mcp-auth --token-path /data/tokens" in out, out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_33_full_first_deploy_offers_the_garmin_login(self):
        rc, out, log = run_pty("", {"GARMIN_AUTH": "full", **TZ}, answers=[(LOGIN_PROMPT, "")])
        self.check(rc == 0 and "ssh --service coach-mcp-for-garmin -- garmin-mcp-auth --token-path /data/tokens"
                   in log and log.index("up --ci") < log.index("ssh") and "Garmin login done." in out, out, log)
        rc, out, log = run_pty("", {"GARMIN_AUTH": "full", **TZ, "FAKE_SSH_FAIL": "1"}, answers=[(LOGIN_PROMPT, "y")])
        self.check(rc == 0 and "The Garmin login did not finish" in out and "railway ssh" in out, out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_33b_ready_wait_after_the_login_and_in_key_mode(self):
        """In a terminal, key mode and a finished full-mode login wait for /readyz before 'add the connector'."""
        rc, out, log = run_pty("", {"GARMIN_AUTH": "full", **TZ}, answers=[(LOGIN_PROMPT, "")])
        self.check(rc == 0 and "/readyz" in log and "The server is ready." in out
                   and "Garmin login done. Now add the connector" in out, out, log)
        rc, out, log = run_pty("", {"GARMIN_AUTH": "key", **TZ}, fake_key())
        self.check(rc == 0 and "/readyz" in log and "key mode needs no Garmin login. Now add the connector" in out,
                   out, log)
        rc, out, log = run_pty("", {"GARMIN_AUTH": "key", **TZ, "FAKE_READYZ": "fail", "DEPLOY_READY_WAIT_S": "4"},
                               fake_key())
        self.check(rc == 0 and "No 200 from /readyz yet" in out and "Wait ~30 s while the server loads" in out,
                   out, log)

    def test_33c_no_ready_wait_without_a_terminal_or_a_login(self):
        rc, out, log = run(self.existing_full, {"FAKE_HC": "1"})
        self.check(rc == 0 and "curl" not in log and "Waiting for" not in out, out, log)
        rc, out, log = run_pty("", {"GARMIN_AUTH": "full", **TZ, "FAKE_SSH_FAIL": "1"} if HAVE_PTY else {},
                               answers=[(LOGIN_PROMPT, "y")]) if HAVE_PTY else (0, "", "")
        self.check(rc == 0 and "curl" not in log, out, log)

    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_34_a_full_mode_redeploy_does_not_offer_the_login(self):
        rc, out, log = run_pty(self.existing_full, {"FAKE_HC": "1"})
        self.check(rc == 0 and LOGIN_PROMPT.decode() not in out and "ssh" not in log, out, log)

    def test_35_railway_link_overrides_are_refused(self):
        for name in ("RAILWAY_TOKEN", "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID"):
            with self.subTest(name=name):
                rc, out, log = run(self.existing_full, {name: "x"})
                self.check(rc == 1 and name + " is set" in out and log == "", out, log)


class DeployScriptStatic(unittest.TestCase):
    def test_23_no_here_string_on_secret_text(self):
        # bash 3.2 (macOS) writes here-string text to a temporary file.
        with open(DEPLOY, encoding="utf-8") as f:
            code = [line for line in f.read().splitlines() if not line.lstrip().startswith("#")]
        bad = [line for line in code
               if re.search(r'<<<\s*"?\$\{?(EXISTING|CLI_OUT|ENV_CONFIG|ENABLED_TOOLS|ENTRY|REST)\b', line)]
        self.assertEqual(bad, [])

    def test_24_variables_set_and_key_only_on_stdin(self):
        # The variable names the fake CLI may see set, and never a GARMIN_KEY on a command line.
        with open(DEPLOY, encoding="utf-8") as f:
            code = [line for line in f.read().splitlines() if not line.lstrip().startswith("#")]
        names = set()
        for line in code:
            if re.match(r"\s*(warn|echo) ", line):  # advice text, not a call
                continue
            m = re.search(r"railway variable set\b.*?\b([A-Z_]+)(=| --stdin)", line)
            if m:
                names.add(m.group(1))
        self.assertEqual(names, {"GARMIN_AUTH", "REPORT_TZ", "GARMIN_PERSONAL_API_KEY", "OWNER_SECRET",
                                 "MCP_BEARER_TOKEN"})
        self.assertFalse([line for line in code if "railway" in line and "$GARMIN_KEY" in line
                          and "printf '%s' \"$GARMIN_KEY\" |" not in line])


if __name__ == "__main__":
    unittest.main()
