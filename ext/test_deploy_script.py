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

# The fake CLI. FAKE_UNLINKED=1: no project is linked yet; after init the
# project has no service until add runs, and variable list then fails with the
# text of the real Railway CLI 5.70 ("Project has no services.", exit 1), or
# with FAKE_NOSERVICE_TEXT. FAKE_HC=1: the environment config already has the
# /healthz health check.
FAKE_RAILWAY = r"""#!/usr/bin/env bash
log() { printf '%s\n' "$*" >> "$FAKE_LOG"; }
sha1() { if command -v sha1sum >/dev/null 2>&1; then sha1sum; else shasum; fi; }
case "$1 ${2:-}" in
  "--version "*) echo "railway 5.70.0" ;;
  "whoami "*) echo "Logged in as test" ;;
  "status "*)
    if [[ -n ${FAKE_UNLINKED:-} && ! -e "$FAKE_LOG.init" ]]; then echo "No linked project found. Run railway link to connect to a project"; exit 1; fi
    echo "Project: test" ;;
  "init "*)
    : > "$FAKE_LOG.init"
    # One log field per argument, so a test can see how the arguments were split.
    line="init"; for a in "${@:2}"; do line+=" [$a]"; done; log "$line" ;;
  "add "*) : > "$FAKE_LOG.added"; log "$*" ;;
  "variable list")
    if [[ -n ${FAKE_UNLINKED:-} && ! -e "$FAKE_LOG.added" ]]; then echo "${FAKE_NOSERVICE_TEXT:-Project has no services.}"; exit 1; fi
    if [[ " $* " == *" --kv "* ]]; then cat "$FAKE_VARS"; else echo "vars"; fi ;;
  "variable set")
    if [[ " $* " == *" --stdin "* ]]; then
      v="$(cat)"; log "variable set $3 --stdin sha=$(printf '%s' "$v" | sha1 | cut -c1-12) len=${#v}"
    else log "$*"; fi ;;
  "volume list") echo "data /data" ;;
  "domain "*) echo "Service Domain created: https://test-app.up.railway.app" ;;
  "environment config")
    if [[ -n ${FAKE_HC:-} || -e "$FAKE_LOG.hc" ]]; then echo '{"services":{"x":{"deploy":{"healthcheckPath": "/healthz"}}}}'; else echo '{}'; fi ;;
  "environment edit")
    log "$*"
    # FAKE_EDIT_NOOP=1: Railway CLI 5.70 with no terminal (seen live on 17 Nov
    # 2026): nothing is applied and the exit code is still 0.
    if [[ -n ${FAKE_EDIT_NOOP:-} ]]; then echo '{"committed":false,"message":"No changes to apply","staged":false}'
    else : > "$FAKE_LOG.hc"; echo '{"committed":true,"staged":true}'; fi ;;
  "up --ci") log "up --ci" ;;
  *) log "UNEXPECTED $*"; exit 3 ;;
esac
"""

PROMPT = b"Garmin Personal API key"
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
        with open(os.path.join(self.root, "vars"), "w", encoding="utf-8") as f:
            f.write(vars_text)
        self.log_path = os.path.join(self.root, "log")
        self.env = {
            "PATH": bindir + os.pathsep + "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": self.root,
            "FAKE_LOG": self.log_path,
            "FAKE_VARS": os.path.join(self.root, "vars"),
            "TERM": "dumb",
            "LC_ALL": "C",
        }
        self.env.update(extra)

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


def run_pty(vars_text, extra, typed, args=()):
    """Run deploy.sh with a pseudo-terminal as stdin, stdout and stderr; type
    `typed` and Enter 0.3 s after the key prompt shows."""
    box = _Sandbox(vars_text, extra)
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([BASH, box.script, *args], env=box.env, stdin=slave, stdout=slave,
                                stderr=slave, close_fds=True, start_new_session=True)
        os.close(slave)
        slave = None
        out = b""
        sent = False
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
                if not sent and PROMPT in out:
                    time.sleep(0.3)
                    os.write(master, typed.encode() + b"\n")
                    sent = True
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
        """Live test B1: the paste did not land and a 1-character key was stored and deployed."""
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
        # Railway CLI 5.70 (seen live on 17 Nov 2026): a new project has no
        # service, and `railway variable list` says so and exits 1.
        rc, out, log = run("", {"GARMIN_AUTH": "full", **TZ, "FAKE_UNLINKED": "1"})
        self.check(rc == 0 and "add --service coach-mcp-for-garmin\n" in log and "up --ci" in log
                   and "Could not read" not in out, out, log)

    def test_20c_older_no_service_text_also_adds_the_service(self):
        rc, out, log = run("", {"GARMIN_AUTH": "full", **TZ, "FAKE_UNLINKED": "1",
                                "FAKE_NOSERVICE_TEXT": "No service linked"})
        self.check(rc == 0 and "add --service coach-mcp-for-garmin\n" in log and "up --ci" in log, out, log)

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

    # Review R4: key mode serves only config/enabled-tools-key.txt, and the server refuses to start
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
        # Lower-cased, this would have the shape of a tool name (review C2).
        junk = "kP" + secrets.token_hex(8) + "_tY" + secrets.token_hex(8)
        rc, out, log = run(self.existing_key + "GARMIN_ENABLED_TOOLS=get_stats," + junk + "\n", {"FAKE_HC": "1"})
        self.check(rc == 1 and "an entry that is not a tool name (it is not shown)" in out
                   and junk.lower() not in out.lower() and "variable set" not in log and "up --ci" not in log,
                   out, log)

    def test_22i3_a_list_of_only_commas_is_refused(self):
        # The server refuses it ("names no tools"), so deploy.sh stops first (review C3).
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
