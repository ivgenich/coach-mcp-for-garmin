"""Behaviour tests for scripts/setup.sh against a fake docker.

Stdlib only; no network, no Docker, no port. Each case copies setup.sh into a
fresh temporary folder (the project folder) and runs it with a fake `docker`
first on PATH. The fake logs every call with all its arguments, so a secret on
a command line would show in the log. Most cases use the non-interactive mode
(SETUP_NONINTERACTIVE=1 and the SETUP_* answers); one types the answers in a
pseudo-terminal, as a person would. The time zone checks use a small zoneinfo
folder of their own (SETUP_ZONEINFO_DIR), and a curl that never reaches the
network.

Every key, secret and token here is built at run time.
"""

import io
import os
import secrets
import select
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

try:
    import pty
except ImportError:  # not on Windows
    pty = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
SETUP = os.path.join(ROOT, "scripts", "setup.sh")
BASH = shutil.which("bash")
GIT = shutil.which("git")
HAVE_PTY = pty is not None and os.name == "posix" and BASH is not None

sys.path.insert(0, HERE)
import auth_config  # noqa: E402

# The fake docker, a python3 script. FAKE_COMPOSE_VERSION (default 2.29.7),
# FAKE_NO_COMPOSE=1 (no compose plugin), FAKE_DOCKER_DOWN=1 (no daemon),
# FAKE_UP_FAIL=1, FAKE_APP (the app's "state health", default "running healthy";
# "none": no container), FAKE_TUNNEL (default "running"), FAKE_NO_CONN=1 (the
# tunnel log shows no connection), FAKE_READYZ (default 200).
FAKE_DOCKER = r"""#!{python}
import io, os, sys, tarfile

LOG = os.environ["FAKE_LOG"]
env = os.environ.get
args = sys.argv[1:]


def log(line):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


log("docker " + " ".join(args))
if args == ["info"]:
    sys.exit(1 if env("FAKE_DOCKER_DOWN") else 0)
if args[:1] != ["compose"]:
    log("UNEXPECTED")
    sys.exit(3)
a = args[1:]
if a[:2] == ["version", "--short"]:
    if env("FAKE_NO_COMPOSE"):
        sys.exit("docker: 'compose' is not a docker command.")
    print(env("FAKE_COMPOSE_VERSION", "2.29.7"))
elif a[:1] == ["up"]:
    sys.exit(1 if env("FAKE_UP_FAIL") else 0)
elif a[:1] == ["ps"]:
    app = env("FAKE_APP", "running healthy")
    if app != "none":
        print("app " + app)
    print("tunnel " + env("FAKE_TUNNEL", "running"))
elif a[:1] == ["logs"]:
    if env("FAKE_TUNNEL", "running").startswith("running") and not env("FAKE_NO_CONN"):
        print("INF Registered tunnel connection connIndex=0")
elif a[:1] == ["exec"]:
    if "python" in a:
        print(env("FAKE_READYZ", "200"))
elif a[:1] == ["stop"]:
    pass
elif a[:1] == ["run"]:
    if "tar" in a:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"fake sign-in database"
            info = tarfile.TarInfo("./auth.sqlite")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        sys.stdout.buffer.write(buf.getvalue())
    elif "sh" in a:
        log("restore stdin len=%d" % len(sys.stdin.buffer.read()))
else:
    log("UNEXPECTED")
    sys.exit(3)
"""

ZONES = ("Europe/London", "Europe/Paris", "America/New_York", "Asia/Tokyo", "UTC")
HOST = "coach.example.com"
WANT_PREFILL = ("https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=Garmin%20coach"
                "&connectorUrl=https%3A%2F%2Fcoach.example.com%2Fmcp")


def fake_key():
    return "k" + secrets.token_urlsafe(18) + "Z"


def fake_token():
    return "eyJ" + secrets.token_urlsafe(60).replace("-", "a").replace("_", "b")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def env_values(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                name, value = line.rstrip("\n").split("=", 1)
                out[name] = value
    return out


class Sandbox:
    """A temporary project folder with setup.sh, a fake docker and curl, and a zoneinfo folder."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="setup-test-")
        self.project = os.path.join(self.root, "project")
        os.makedirs(os.path.join(self.project, "scripts"))
        shutil.copy(SETUP, os.path.join(self.project, "scripts", "setup.sh"))
        self.bindir = os.path.join(self.root, "bin")
        os.makedirs(self.bindir)
        self.write_exe("docker", FAKE_DOCKER.replace("{python}", sys.executable, 1))
        # A curl that never reaches the network: it fails unless FAKE_CURL_OK=1.
        self.write_exe("curl", '#!/bin/sh\necho "curl $*" >>"$FAKE_LOG"\n[ "$FAKE_CURL_OK" = 1 ]\n')
        # A timedatectl that knows nothing, so the host's own never answers.
        self.write_exe("timedatectl", "#!/bin/sh\nexit 1\n")
        self.zoneinfo = os.path.join(self.root, "zoneinfo")
        for zone in ZONES:
            path = os.path.join(self.zoneinfo, zone)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"TZif")
        self.log_path = os.path.join(self.root, "log")
        self.env = {
            "PATH": self.bindir + os.pathsep + "/usr/bin:/bin",
            "HOME": self.root,
            "FAKE_LOG": self.log_path,
            "TERM": "dumb",
            "LC_ALL": "C",
            "SETUP_ZONEINFO_DIR": self.zoneinfo,
            "SETUP_LOCALTIME": os.path.join(self.root, "no-localtime"),
            "SETUP_ETC_TIMEZONE": os.path.join(self.root, "no-timezone"),
            "SETUP_POLL_S": "0",
            "SETUP_WAIT_S": "5",
            "SETUP_TUNNEL_WAIT_S": "0",
        }

    def write_exe(self, name, text):
        path = os.path.join(self.bindir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def path(self, *parts):
        return os.path.join(self.project, *parts)

    def log(self):
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, encoding="utf-8") as f:
            return f.read()

    def clear_log(self):
        if os.path.exists(self.log_path):
            os.remove(self.log_path)

    def run(self, extra=None, args=(), script=None):
        env = dict(self.env)
        env.update(extra or {})
        p = subprocess.run([BASH, script or self.path("scripts", "setup.sh"), *args], env=env, cwd=self.root,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr, self.log()

    def run_pty(self, extra, answers):
        """Run setup.sh in a pseudo-terminal; for each (prompt, text) of answers, in order, type text and
        Enter 0.3 s after the prompt shows."""
        env = dict(self.env)
        env.update(extra)
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen([BASH, self.path("scripts", "setup.sh")], env=env, cwd=self.root, stdin=slave,
                                    stdout=slave, stderr=slave, close_fds=True, start_new_session=True)
            os.close(slave)
            slave = None
            out, todo, seen = b"", list(answers), 0
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
            return rc, out.decode(errors="replace"), self.log(), todo
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


@unittest.skipUnless(BASH, "needs bash on PATH")
class SetupScript(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.key = fake_key()
        self.token = fake_token()
        self.answers = {"SETUP_NONINTERACTIVE": "1", "SETUP_HOST": HOST, "SETUP_AUTH": "key",
                        "SETUP_TZ": "Europe/London", "SETUP_KEY": self.key, "SETUP_TUNNEL_TOKEN": self.token}

    def check(self, cond, out, log=""):
        self.assertTrue(cond, "output:\n" + out[-3000:] + "\nlog:\n" + log[-2000:])

    def install(self, **extra):
        return self.box.run(dict(self.answers, **extra))

    def secret(self):
        return env_values(self.box.path(".env"))["OWNER_SECRET"]

    def assert_no_values(self, text, *more):
        for value in (self.key, self.token, *more):
            self.assertNotIn(value, text)

    # --- install ----------------------------------------------------------------
    def test_fresh_install_key_mode(self):
        rc, out, log = self.install()
        self.check(rc == 0, out, log)
        env, tunnel = self.box.path(".env"), self.box.path("tunnel.env")
        self.assertEqual(mode(env), 0o600)
        self.assertEqual(mode(tunnel), 0o600)
        values = env_values(env)
        self.assertEqual(values["GARMIN_AUTH"], "key")
        self.assertEqual(values["PUBLIC_URL"], "https://" + HOST)
        self.assertEqual(values["REPORT_TZ"], "Europe/London")
        self.assertEqual(values["GARMIN_PERSONAL_API_KEY"], "'%s'" % self.key)
        self.assertEqual(env_values(tunnel), {"TUNNEL_TOKEN": self.token})
        secret = values["OWNER_SECRET"]
        # The server's own check accepts the secret and the origin.
        cfg, errors = auth_config.config_from_environ({"OWNER_SECRET": secret, "PUBLIC_URL": values["PUBLIC_URL"]})
        self.assertEqual(errors, [])
        self.assertIsNotNone(cfg)
        # Shown exactly once, on the terminal; never on a command line; the key and token never shown.
        self.assertEqual(out.count(secret), 1, out)
        self.assertIn("save it now", out)
        self.assertNotIn(secret, log)
        self.assert_no_values(out)
        self.assert_no_values(log)
        self.assertIn("docker compose up -d --build", log)
        self.assertLess(log.index("docker compose version --short"), log.index("docker compose up -d --build"))
        self.assertIn("Connector URL (exactly, no trailing slash): https://coach.example.com/mcp", out)
        self.assertIn(WANT_PREFILL, out)
        self.assertIn("Bot Fight Mode off", out)
        self.assertIn("no Access application", out)
        self.assertNotIn("garmin-mcp-auth --token-path", log)
        self.assertNotIn(".setup-tmp", "".join(os.listdir(self.box.project)))

    def test_secrets_are_different_each_install(self):
        self.check(self.install()[0] == 0, "")
        first = self.secret()
        other = Sandbox()
        self.addCleanup(other.close)
        rc, out, log = other.run(self.answers)
        self.check(rc == 0, out, log)
        self.assertNotEqual(first, env_values(other.path(".env"))["OWNER_SECRET"])
        self.assertRegex(first, r"^[2-9a-hjkmnp-z]{5}(-[2-9a-hjkmnp-z]{5}){7}$")

    def test_rerun_keeps_existing_settings_and_secrets(self):
        self.check(self.install()[0] == 0, "")
        with open(self.box.path(".env"), encoding="utf-8") as f:
            before_env = f.read()
        with open(self.box.path("tunnel.env"), encoding="utf-8") as f:
            before_tunnel = f.read()
        secret = self.secret()
        self.box.clear_log()
        new_key, new_token = fake_key(), fake_token()
        rc, out, log = self.install(SETUP_KEY=new_key, SETUP_TUNNEL_TOKEN=new_token, SETUP_HOST="other.example.com")
        self.check(rc == 0, out, log)
        with open(self.box.path(".env"), encoding="utf-8") as f:
            self.assertEqual(f.read(), before_env)
        with open(self.box.path("tunnel.env"), encoding="utf-8") as f:
            self.assertEqual(f.read(), before_tunnel)
        self.assertNotIn(secret, out)
        self.assertIn("The owner secret is unchanged", out)
        self.assertIn("Kept tunnel.env.", out)
        self.assert_no_values(out + log, secret, new_key, new_token)

    def test_rerun_tightens_file_modes(self):
        self.check(self.install()[0] == 0, "")
        os.chmod(self.box.path(".env"), 0o644)
        os.chmod(self.box.path("tunnel.env"), 0o644)
        rc, out, _ = self.install()
        self.check(rc == 0 and "Made .env private" in out and "Made tunnel.env private" in out, out)
        self.assertEqual(mode(self.box.path(".env")), 0o600)
        self.assertEqual(mode(self.box.path("tunnel.env")), 0o600)

    def test_noninteractive_never_changes_an_incomplete_env(self):
        text = "OWNER_SECRET=generated-at-deploy\nGARMIN_AUTH=\nREPORT_TZ=\n"
        with open(self.box.path(".env"), "w", encoding="utf-8") as f:
            f.write(text)
        rc, out, log = self.install()
        self.check(rc == 1 and "never changes an existing .env" in out and "OWNER_SECRET" in out, out, log)
        with open(self.box.path(".env"), encoding="utf-8") as f:
            self.assertEqual(f.read(), text)
        self.assertNotIn("compose up", log)
        self.assertFalse(os.path.exists(self.box.path("tunnel.env")))

    def test_full_mode_never_starts_the_login_without_a_terminal(self):
        answers = dict(self.answers, SETUP_AUTH="full")
        del answers["SETUP_KEY"]
        rc, out, log = self.box.run(dict(answers, FAKE_READYZ="503"))
        self.check(rc == 0, out, log)
        self.assertNotIn("GARMIN_PERSONAL_API_KEY", env_values(self.box.path(".env")))
        self.assertNotIn("garmin-mcp-auth", log)
        self.assertIn("docker compose exec app garmin-mcp-auth --token-path /data/tokens", out)

    def test_old_compose_refused(self):
        for version in ("2.19.1", "v2.4.0", "1.29.2"):
            with self.subTest(version=version):
                rc, out, log = self.install(FAKE_COMPOSE_VERSION=version)
                self.check(rc == 1 and "too old" in out and "2.20 or newer" in out, out, log)
                self.assertNotIn("compose up", log)
                self.assertFalse(os.path.exists(self.box.path(".env")))
        for version in ("2.20.0", "v2.29.7", "5.7.1"):
            with self.subTest(version=version):
                self.box.clear_log()
                rc, out, log = self.install(FAKE_COMPOSE_VERSION=version)
                self.check(rc == 0, out, log)

    def test_compose_v1_only_refused(self):
        self.box.write_exe("docker-compose", "#!/bin/sh\necho 'docker-compose version 1.29.2'\n")
        rc, out, log = self.install(FAKE_NO_COMPOSE="1")
        self.check(rc == 1 and "Compose v1" in out and "not supported" in out and "compose up" not in log, out, log)

    def test_compose_missing_and_docker_down(self):
        rc, out, _ = self.install(FAKE_NO_COMPOSE="1")
        self.check(rc == 1 and "docker compose is missing" in out, out)
        rc, out, log = self.install(FAKE_DOCKER_DOWN="1")
        self.check(rc == 1 and "not running" in out and "compose up" not in log, out, log)

    def test_bad_hostnames_refused(self):
        bad = ("http://coach.example.com", "coach.example.com:8443", "https://coach.example.com:443",
               "coach.example.com/mcp", "https://coach.example.com/mcp", "192.168.1.10", "[::1]", "localhost",
               "user@coach.example.com", "coach_example.com", "ftp://coach.example.com", "", "-coach.example.com")
        for host in bad:
            with self.subTest(host=host):
                rc, out, log = self.install(SETUP_HOST=host)
                self.check(rc == 1 and "SETUP_HOST" in out and "compose up" not in log, out, log)
                self.assertFalse(os.path.exists(self.box.path(".env")))

    def test_host_with_https_and_capitals_accepted(self):
        rc, out, log = self.install(SETUP_HOST="https://Coach.Example.com/")
        self.check(rc == 0, out, log)
        self.assertEqual(env_values(self.box.path(".env"))["PUBLIC_URL"], "https://coach.example.com")

    def test_deep_subdomain_warns(self):
        rc, out, _ = self.install(SETUP_HOST="a.coach.example.com")
        self.check(rc == 0 and "Advanced Certificate" in out, out)

    def test_bad_garmin_mode_and_key_refused(self):
        rc, out, _ = self.install(SETUP_AUTH="fulll")
        self.check(rc == 1 and "SETUP_AUTH must be key or full" in out, out)
        for key, why in (("abc12", "too short"), ("has space in it, long enough", "spaces"),
                         ("k" * 513, "too long"), ("quote'in-the-key-here", "'")):
            with self.subTest(why=why):
                rc, out, log = self.install(SETUP_KEY=key)
                self.check(rc == 1 and why in out and "compose up" not in log, out, log)
                self.assertNotIn(key, out)
                self.assertFalse(os.path.exists(self.box.path(".env")))

    def test_bad_tunnel_token_refused(self):
        rc, out, log = self.install(SETUP_TUNNEL_TOKEN="not a token")
        self.check(rc == 1 and "SETUP_TUNNEL_TOKEN" in out and "compose up" not in log, out, log)
        self.assertFalse(os.path.exists(self.box.path("tunnel.env")))
        # The new owner secret is not lost: the stop message says where it is, without showing it.
        self.assertIn("grep '^OWNER_SECRET=' .env", out)
        self.assertNotIn(self.secret(), out)

    def test_not_a_terminal_without_noninteractive_stops(self):
        rc, out, log = self.box.run({})
        self.check(rc == 1 and "Run this in a terminal" in out and "compose up" not in log, out, log)

    # --- time zone ----------------------------------------------------------------
    def tz_install(self, **extra):
        answers = dict(self.answers, **extra)
        answers.pop("SETUP_TZ")
        return self.box.run(answers)

    def test_time_zone_from_macos_link(self):
        link = os.path.join(self.box.root, "localtime")
        os.symlink("/var/db/timezone/zoneinfo/America/New_York", link)
        rc, out, log = self.tz_install(SETUP_LOCALTIME=link)
        self.check(rc == 0, out, log)
        self.assertEqual(env_values(self.box.path(".env"))["REPORT_TZ"], "America/New_York")

    def test_time_zone_from_linux_link(self):
        link = os.path.join(self.box.root, "localtime")
        os.symlink("/usr/share/zoneinfo/Asia/Tokyo", link)
        rc, out, log = self.tz_install(SETUP_LOCALTIME=link)
        self.check(rc == 0, out, log)
        self.assertEqual(env_values(self.box.path(".env"))["REPORT_TZ"], "Asia/Tokyo")

    def test_time_zone_from_timedatectl(self):
        self.box.write_exe("timedatectl", '#!/bin/sh\n[ "$*" = "show -p Timezone --value" ] && echo Europe/Paris\n')
        rc, out, log = self.tz_install()
        self.check(rc == 0, out, log)
        self.assertEqual(env_values(self.box.path(".env"))["REPORT_TZ"], "Europe/Paris")

    def test_time_zone_from_etc_timezone(self):
        path = os.path.join(self.box.root, "timezone")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Europe/London\n")
        rc, out, log = self.tz_install(SETUP_ETC_TIMEZONE=path)
        self.check(rc == 0, out, log)
        self.assertEqual(env_values(self.box.path(".env"))["REPORT_TZ"], "Europe/London")

    def test_unknown_detected_zone_and_no_answer_stops(self):
        link = os.path.join(self.box.root, "localtime")
        os.symlink("/usr/share/zoneinfo/Mars/Base", link)
        rc, out, log = self.tz_install(SETUP_LOCALTIME=link)
        self.check(rc == 1 and "SETUP_TZ" in out and "compose up" not in log, out, log)

    def test_time_zone_is_case_exact(self):
        for zone in ("europe/london", "EUROPE/LONDON", "Europe/Atlantis", "../etc/passwd", "Europe//London"):
            with self.subTest(zone=zone):
                rc, out, log = self.install(SETUP_TZ=zone)
                self.check(rc == 1 and "SETUP_TZ" in out and "compose up" not in log, out, log)

    # --- app and tunnel state -----------------------------------------------------------
    def test_unhealthy_app_stops(self):
        rc, out, log = self.install(FAKE_APP="exited ")
        self.check(rc == 1 and "app is not healthy" in out and "docker compose logs app" in out, out, log)

    def test_up_failure_stops(self):
        rc, out, log = self.install(FAKE_UP_FAIL="1")
        self.check(rc == 1 and "docker compose up failed" in out, out, log)

    def test_tunnel_down_is_reported_and_the_rest_still_shown(self):
        rc, out, log = self.install(FAKE_TUNNEL="restarting ")
        self.check(rc == 1 and "tunnel: not running" in out and "docker compose logs tunnel" in out, out, log)
        self.assertEqual(out.count(self.secret()), 1)
        self.assertIn(WANT_PREFILL, out)

    def test_key_refused_is_reported(self):
        rc, out, log = self.install(FAKE_READYZ="503")
        self.check(rc == 1 and "Garmin refused the key" in out, out, log)

    # --- check --------------------------------------------------------------------
    def test_check_shows_names_not_values(self):
        self.check(self.install()[0] == 0, "")
        secret = self.secret()
        self.box.clear_log()
        rc, out, log = self.box.run({}, args=("--check",))
        for name in ("OWNER_SECRET: set", "GARMIN_AUTH: set", "GARMIN_PERSONAL_API_KEY: set", "REPORT_TZ: set",
                     "PUBLIC_URL: set", "TUNNEL_TOKEN: set", "app: running, healthy", "tunnel: running, connected",
                     "/readyz: 200"):
            self.assertIn(name, out)
        self.assert_no_values(out + log, secret)
        self.assertNotIn("Europe/London", out)
        # This fake curl cannot reach the public URL: one problem, with a next step.
        self.check(rc == 1 and "cannot reach the server's public URL" in out and "Next:" in out
                   and "check: 1 problem(s)" in out, out, log)
        # Read-only: nothing started, stopped, built or written.
        for verb in ("compose up", "compose stop", "compose run", "garmin-mcp-auth"):
            self.assertNotIn(verb, log)

    def test_check_runs_test_connector_when_reachable(self):
        self.check(self.install()[0] == 0, "")
        connector = self.box.path("scripts", "test-connector.sh")
        with open(connector, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho "test-connector $*" >>"$FAKE_LOG"\necho "RESULT: PASS"\n')
        os.chmod(connector, 0o755)
        rc, out, log = self.box.run({"FAKE_CURL_OK": "1"}, args=("--check",))
        self.check(rc == 0 and "check: OK" in out and "test-connector https://coach.example.com\n" in log, out, log)

    def test_check_full_mode_before_login(self):
        answers = dict(self.answers, SETUP_AUTH="full")
        del answers["SETUP_KEY"]
        self.check(self.box.run(dict(answers, FAKE_READYZ="503"))[0] == 0, "")
        rc, out, log = self.box.run({"FAKE_READYZ": "503"}, args=("--check",))
        self.check(rc == 1 and "no Garmin login yet" in out
                   and "docker compose exec app garmin-mcp-auth --token-path /data/tokens" in out, out, log)

    def test_check_flags_missing_files_and_open_modes(self):
        rc, out, _ = self.box.run({}, args=("--check",))
        self.check(rc == 1 and ".env is missing" in out and "tunnel.env is missing" in out, out)
        self.check(self.install()[0] == 0, "")
        os.chmod(self.box.path(".env"), 0o644)
        rc, out, _ = self.box.run({}, args=("--check",))
        self.check(rc == 1 and "readable by others (mode 644)" in out and "chmod 600 .env" in out, out)

    # --- backup and restore -------------------------------------------------------
    def test_backup_stops_copies_starts_in_order(self):
        self.check(self.install()[0] == 0, "")
        self.box.clear_log()
        rc, out, log = self.box.run({}, args=("--backup",))
        self.check(rc == 0, out, log)
        calls = [line for line in log.splitlines() if line.startswith("docker compose ") and " ps " not in line
                 and "version" not in line]
        self.assertEqual(calls, ["docker compose stop app",
                                 "docker compose run --rm --no-deps -T --entrypoint tar app -czf - -C /data .",
                                 "docker compose up -d"])
        files = os.listdir(self.box.path("backups"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^coach-data-\d{8}-\d{6}(-\d+)?\.tar\.gz$")
        backup = self.box.path("backups", files[0])
        self.assertEqual(mode(backup), 0o600)
        with tarfile.open(backup) as tar:
            self.assertEqual(tar.getnames(), ["./auth.sqlite"])
        self.assertIn("scripts/setup.sh --restore backups/" + files[0], out)
        self.assert_no_values(out + log, self.secret())

    def test_backup_to_a_folder_of_your_own_and_app_stopped(self):
        self.check(self.install()[0] == 0, "")
        self.box.clear_log()
        target = os.path.join(self.box.root, "safe")
        rc, out, log = self.box.run({"FAKE_APP": "exited "}, args=("--backup", target))
        self.check(rc == 0 and len(os.listdir(target)) == 1, out, log)
        # The app was not running: the backup neither stops nor starts it.
        self.assertNotIn("compose stop", log)
        self.assertNotIn("compose up", log)

    def make_backup(self, names=("./auth.sqlite",)):
        path = os.path.join(self.box.root, "backup.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            for name in names:
                data = b"x"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def test_restore_asks_then_stops_restores_starts(self):
        self.check(self.install()[0] == 0, "")
        backup = self.make_backup()
        self.box.clear_log()
        rc, out, log = self.box.run({"SETUP_NONINTERACTIVE": "1"}, args=("--restore", backup))
        self.check(rc == 1 and "SETUP_YES=1" in out and "compose stop" not in log and "compose run" not in log,
                   out, log)
        rc, out, log = self.box.run({"SETUP_NONINTERACTIVE": "1", "SETUP_YES": "1"}, args=("--restore", backup))
        self.check(rc == 0, out, log)
        calls = [line for line in log.splitlines() if line.startswith(("docker compose stop", "docker compose run",
                                                                       "docker compose up", "restore"))]
        self.assertEqual(calls, ["docker compose stop app",
                                 "docker compose run --rm --no-deps -T --entrypoint sh app -c "
                                 "find /data -mindepth 1 -delete && tar -xzf - -C /data",
                                 "restore stdin len=%d" % os.path.getsize(backup),
                                 "docker compose up -d"])

    def test_restore_refuses_paths_outside_the_volume(self):
        self.check(self.install()[0] == 0, "")
        self.box.clear_log()
        for names in (("../evil",), ("./ok", "./a/../../evil"), ("/etc/evil",)):
            with self.subTest(names=names):
                rc, out, log = self.box.run({"SETUP_NONINTERACTIVE": "1", "SETUP_YES": "1"},
                                            args=("--restore", self.make_backup(names)))
                self.check(rc == 1 and "outside the volume" in out and "compose run" not in log, out, log)

    def test_restore_refuses_a_file_that_is_not_a_backup(self):
        self.check(self.install()[0] == 0, "")
        path = os.path.join(self.box.root, "junk.tar.gz")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not a tar")
        rc, out, _ = self.box.run({"SETUP_NONINTERACTIVE": "1", "SETUP_YES": "1"}, args=("--restore", path))
        self.check(rc == 1 and "is not a .tar.gz backup" in out, out)

    def test_usage(self):
        for args in (("--restore",), ("--upgrade",), ("--check", "x"), ("--bogus",)):
            with self.subTest(args=args):
                rc, out, log = self.box.run({}, args=args)
                self.check(rc == 2 and "scripts/setup.sh --check" in out and log == "", out, log)

    # --- interactive ----------------------------------------------------------------
    @unittest.skipUnless(HAVE_PTY, "needs a pty (Linux or macOS)")
    def test_interactive_hidden_answers(self):
        link = os.path.join(self.box.root, "localtime")
        os.symlink("/var/db/timezone/zoneinfo/Europe/Paris", link)
        answers = [(b"Public host name", "coach.example.com:8443"), (b"Public host name", HOST),
                   (b"Garmin mode, key or full", ""), (b"Time zone, an IANA name such as Europe/London [Europe/Paris]", ""),
                   (b"Garmin Personal API key (not shown)", self.key), (b"Cloudflare tunnel token (not shown)", self.token)]
        rc, out, log, left = self.box.run_pty({"SETUP_LOCALTIME": link}, answers)
        self.check(rc == 0 and not left and "Not usable: no port" in out, out, log)
        values = env_values(self.box.path(".env"))
        self.assertEqual((values["GARMIN_AUTH"], values["REPORT_TZ"]), ("key", "Europe/Paris"))
        self.assertEqual(values["GARMIN_PERSONAL_API_KEY"], "'%s'" % self.key)
        self.assertEqual(env_values(self.box.path("tunnel.env")), {"TUNNEL_TOKEN": self.token})
        # Typed at read -s prompts: the terminal never shows the key or the token.
        self.assert_no_values(out + log)
        self.assertEqual(out.count(values["OWNER_SECRET"]), 1)

    # --- upgrade --------------------------------------------------------------------
    def git(self, cwd, *args):
        env = dict(self.box.env, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        subprocess.run([GIT, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                        *args], cwd=cwd, env=env, check=True, capture_output=True, timeout=30)

    def make_clone(self, changelog_new):
        """An origin repository with tags v1.0.0 and v1.1.0, and the project folder cloned from it at v1.0.0."""
        origin = os.path.join(self.box.root, "origin")
        os.makedirs(os.path.join(origin, "scripts"))
        shutil.copy(SETUP, os.path.join(origin, "scripts", "setup.sh"))
        with open(os.path.join(origin, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(".env\ntunnel.env\nbackups/\n")
        with open(os.path.join(origin, "CHANGELOG.md"), "w", encoding="utf-8") as f:
            f.write("# Changelog\n\n## [1.0.0] - 2026-01-01\n\nFirst.\n")
        self.git(origin, "init", "-q")
        self.git(origin, "add", "-A")
        self.git(origin, "commit", "-q", "-m", "one")
        self.git(origin, "tag", "v1.0.0")
        with open(os.path.join(origin, "CHANGELOG.md"), "w", encoding="utf-8") as f:
            f.write("# Changelog\n\n## [1.1.0] - 2026-02-01\n\n" + changelog_new + "\n\n## [1.0.0] - 2026-01-01\n\nFirst.\n")
        self.git(origin, "commit", "-q", "-am", "two")
        self.git(origin, "tag", "v1.1.0")
        shutil.rmtree(self.box.project)
        self.git(self.box.root, "clone", "-q", origin, self.box.project)
        self.git(self.box.project, "checkout", "-q", "v1.0.0")
        return origin

    @unittest.skipUnless(GIT, "needs git on PATH")
    def test_upgrade_backs_up_then_checks_out_and_rebuilds(self):
        self.make_clone("- The sign-in database gets a new table.")
        self.check(self.install()[0] == 0, "")
        self.box.clear_log()
        rc, out, log = self.box.run({}, args=("--upgrade", "v1.1.0"))
        self.check(rc == 0, out, log)
        calls = [line for line in log.splitlines() if line.startswith(("docker compose stop", "docker compose run",
                                                                       "docker compose up"))]
        self.assertEqual(calls, ["docker compose stop app",
                                 "docker compose run --rm --no-deps -T --entrypoint tar app -czf - -C /data .",
                                 "docker compose up -d", "docker compose up -d --build"])
        head = subprocess.run([GIT, "describe", "--tags", "--exact-match"], cwd=self.box.project,
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(head, "v1.1.0")
        backup = os.listdir(self.box.path("backups"))[0]
        self.assertIn("scripts/setup.sh --upgrade v1.0.0", out)
        self.assertIn("The CHANGELOG since v1.0.0 says the sign-in database changed", out)
        self.assertIn("scripts/setup.sh --restore backups/" + backup, out)
        self.assert_no_values(out + log, self.secret())

    @unittest.skipUnless(GIT, "needs git on PATH")
    def test_upgrade_without_database_change_and_rollback(self):
        self.make_clone("- A new tool.")
        self.check(self.install()[0] == 0, "")
        rc, out, log = self.box.run({}, args=("--upgrade", "v1.1.0"))
        self.check(rc == 0 and "The CHANGELOG since" not in out
                   and "If the CHANGELOG says the sign-in database changed" in out, out, log)
        rc, out, log = self.box.run({}, args=("--upgrade", "v1.0.0"))
        self.check(rc == 0 and "scripts/setup.sh --upgrade v1.1.0" in out, out, log)

    @unittest.skipUnless(GIT, "needs git on PATH")
    def test_upgrade_refuses_local_changes_and_unknown_tags(self):
        self.make_clone("- A new tool.")
        self.check(self.install()[0] == 0, "")
        with open(self.box.path("CHANGELOG.md"), "a", encoding="utf-8") as f:
            f.write("local edit\n")
        self.box.clear_log()
        rc, out, log = self.box.run({}, args=("--upgrade", "v1.1.0"))
        self.check(rc == 1 and "local changes" in out and "compose" not in log, out, log)
        self.git(self.box.project, "checkout", "-q", "--", "CHANGELOG.md")
        rc, out, log = self.box.run({}, args=("--upgrade", "v9.9.9"))
        self.check(rc == 1 and "There is no tag v9.9.9" in out and "compose up -d --build" not in log, out, log)
        rc, out, log = self.box.run({}, args=("--upgrade", "-x"))
        self.check(rc == 1 and "Not a tag name" in out, out, log)

    def test_upgrade_needs_a_git_clone(self):
        self.check(self.install()[0] == 0, "")
        self.box.env["GIT_CEILING_DIRECTORIES"] = self.box.root
        rc, out, log = self.box.run({}, args=("--upgrade", "v1.1.0"))
        self.check(rc == 1 and "not a git clone" in out and "compose run" not in log, out, log)


class SetupScriptText(unittest.TestCase):
    """Static rules for the script's text."""

    def setUp(self):
        with open(SETUP, encoding="utf-8") as f:
            self.text = f.read()

    def test_no_xtrace_and_secrets_read_hidden(self):
        self.assertIn("set +x", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*set -[a-z]*x")
        self.assertIn("IFS= read -rs", self.text)

    def test_no_registry_image_but_the_project(self):
        # The backup uses the app's own image; no pulled helper image (alpine, busybox, ...).
        for name in ("alpine", "busybox", "docker run", "docker pull", "ghcr.io", "docker push"):
            self.assertNotIn(name, self.text)

    def test_executable(self):
        self.assertTrue(os.access(SETUP, os.X_OK))

    def test_backups_stay_out_of_git_and_the_image(self):
        # --backup writes the Garmin login and the sign-ins to backups/ by default.
        for name in (".gitignore", ".dockerignore"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                lines = [line.strip() for line in f]
            with self.subTest(file=name):
                self.assertIn("backups/", lines)

    def test_whole_script_is_read_before_it_runs(self):
        # --upgrade checks out another version of this file while it runs.
        self.assertTrue(self.text.endswith('\nmain "$@"; exit $?\n'))


if __name__ == "__main__":
    unittest.main()
