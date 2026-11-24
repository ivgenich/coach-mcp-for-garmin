"""Test helpers for live-check and release-check (not a test file).

- write_fake_railway(): a fake `railway` executable for PATH that answers each
  argv with canned output from a JSON spec and records every argv it got.
- write_fake_gitleaks(): a fake `gitleaks` with a fixed exit code.
- StubServer: a loopback HTTP server on port 0 that records requests.
- GitRepo: a temp git repository with a repo-local GitHub noreply identity.

Fake data only: every token or secret is built at run time by the tests.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LIVE_CHECK = os.path.join(ROOT, "scripts", "live-check.py")
RELEASE_CHECK = os.path.join(ROOT, "scripts", "release-check.py")
NOREPLY_EMAIL = "1234567+tester@users.noreply.github.com"


def require_upstream():
    return os.environ.get("REQUIRE_UPSTREAM") == "1"


def temp_dir(test):
    """A temp folder removed when the test ends."""
    path = tempfile.mkdtemp(prefix="livefakes-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


_FAKE_RAILWAY = r'''
import json, os, sys, time
SPEC = {spec!r}
LOG = {log!r}
argv = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": argv, "cwd": os.getcwd()}}) + "\n")
with open(SPEC, encoding="utf-8") as f:
    spec = json.load(f)
for i, rule in enumerate(spec.get("rules", [])):
    args = rule["args"]
    if argv[:len(args)] != args or (rule.get("exact") and len(argv) != len(args)):
        continue
    if "seq" in rule:
        counter = SPEC + ".count%d" % i
        try:
            n = int(open(counter).read())
        except (OSError, ValueError):
            n = 0
        with open(counter, "w") as f:
            f.write(str(n + 1))
        step = rule["seq"][min(n, len(rule["seq"]) - 1)]
    else:
        step = rule
    out = step.get("stdout", "")
    probe = step.get("probe")
    if probe:
        try:
            paths = [p for p in open(probe["paths_file"]).read().splitlines() if p]
        except OSError:
            paths = []
        lines = []
        for p in paths:
            caddy = {{"level": "info", "logger": "http.log.access.log0", "msg": "handled request",
                     "request": {{"client_ip": probe["client_ip"], "remote_ip": "10.1.2.3", "uri": p,
                                 "method": "GET"}}, "status": 404}}
            lines.append(json.dumps({{"timestamp": "2026-11-17T00:00:00Z", "message": json.dumps(caddy)}}))
        out = out + "".join(x + "\n" for x in lines)
    if step.get("sleep"):
        time.sleep(step["sleep"])
    sys.stdout.write(out)
    sys.stderr.write(step.get("stderr", ""))
    sys.exit(step.get("exit", 0))
sys.stderr.write("fake railway: no rule for this command\n")
sys.exit(97)
'''


def write_executable(folder, name, body):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable} -B\n" + body)
    os.chmod(path, 0o755)
    return path


class FakeRailway:
    """A fake `railway` in its own bin folder. rules: [{"args": [...], "stdout": ..., "exit": ...}, ...]."""

    def __init__(self, folder, rules):
        self.bin = os.path.join(folder, "bin")
        os.makedirs(self.bin, exist_ok=True)
        self.spec = os.path.join(folder, "railway-spec.json")
        self.log = os.path.join(folder, "railway-argv.jsonl")
        self.set_rules(rules)
        write_executable(self.bin, "railway", _FAKE_RAILWAY.format(spec=self.spec, log=self.log))

    def set_rules(self, rules):
        with open(self.spec, "w", encoding="utf-8") as f:
            json.dump({"rules": rules}, f)

    def calls(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return [json.loads(x)["argv"] for x in f if x.strip()]
        except OSError:
            return []

    def path_env(self):
        return self.bin + os.pathsep + os.environ.get("PATH", "")


def write_fake_gitleaks(folder, exit_code=0, finding=None):
    """A fake gitleaks; with exit_code 1 it also writes `finding` ({"RuleID", "File", "Secret"}) to the report."""
    body = (
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['version']:\n"
        "    print('8.30.1'); sys.exit(0)\n"
        f"code = {int(exit_code)}\n"
        f"finding = {json.dumps(finding)!r}\n"
        "if '--report-path' in args:\n"
        "    path = args[args.index('--report-path') + 1]\n"
        "    with open(path, 'w') as f:\n"
        "        f.write('[' + finding + ']' if code and finding != 'null' else '[]')\n"
        "sys.exit(code)\n"
    )
    os.makedirs(folder, exist_ok=True)
    return write_executable(folder, "gitleaks", body)


class StubServer:
    """Loopback HTTP server on port 0 that records (path, headers) and answers 404."""

    def __init__(self, paths_file=None, status=404):
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's name
                stub.requests.append((self.path, dict(self.headers.items())))
                if paths_file:
                    with open(paths_file, "a", encoding="utf-8") as f:
                        f.write(self.path + "\n")
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def git_env(extra=None):
    """An environment for git that ignores the user's and the system's git config."""
    env = dict(os.environ)
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "TZ": "UTC"})
    for k in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    if extra:
        env.update(extra)
    return env


class GitRepo:
    """A temp git repository on branch main with a repo-local noreply identity."""

    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "--local", "user.email", NOREPLY_EMAIL)
        self.git("config", "--local", "user.name", "Test Tester")
        self.git("config", "--local", "commit.gpgsign", "false")
        self.git("config", "--local", "tag.gpgsign", "false")

    def git(self, *args, env=None):
        proc = subprocess.run(["git", "-C", self.path] + list(args), capture_output=True, text=True,
                              env=git_env(env), check=False)
        if proc.returncode != 0:
            raise AssertionError(f"git {args[0]} failed: {proc.stderr.strip()}")
        return proc.stdout

    def write(self, rel, text, mode=None):
        path = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(path, mode)

    def remove(self, rel):
        os.remove(os.path.join(self.path, rel))

    def commit(self, message, env=None):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, env=env)

    def sync_changelog(self, version="1.0.0"):
        """Make the release heading carry HEAD's UTC day (commits only when that changes the file)."""
        for _ in range(2):  # a second pass covers a commit that crossed midnight UTC
            self.write("CHANGELOG.md", f"## [Unreleased]\n\n## [{version}] - {self.head_utc_day()}\n")
            if self.git("status", "--porcelain"):
                self.commit("changelog")

    def head_utc_day(self):
        return self.git("log", "-1", "--format=%cd", "--date=format-local:%Y-%m-%d", env={"TZ": "UTC"}).strip()

    def state(self):
        return (self.git("status", "--porcelain"), self.git("for-each-ref"))


def good_repo(path, version="1.0.0", changelog_day=None):
    """A repository that passes release-check (with a fake gitleaks that finds nothing)."""
    repo = GitRepo(path)
    repo.write("ext/version.py", f'VERSION = "{version}"\n')
    repo.write(".gitignore", "__pycache__/\n*.pyc\n.env\n")
    repo.write("LICENSE", "MIT License\n")
    repo.write("README.md", "# test project\n")
    repo.write("SECURITY.md", "# Security\n")
    repo.write(".env.example", "GARMIN_AUTH=\n")
    repo.write("scripts/deploy.sh", "#!/bin/sh\necho deploy\n", 0o755)
    repo.write("scripts/tool.py", "#!/usr/bin/env python3\nprint('x')\n", 0o755)
    repo.write("scripts/helper.py", "X = 1\n")
    repo.write("CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n\n## [0.0.0] - 2000-01-01\n")
    repo.commit("first")
    day = changelog_day or repo.head_utc_day()
    repo.write("CHANGELOG.md", f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - {day}\n\n### Added\n- all\n")
    repo.commit("release")
    if changelog_day is None and repo.head_utc_day() != day:  # crossed midnight UTC between the commits
        repo.write("CHANGELOG.md", f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - {repo.head_utc_day()}\n")
        repo.git("commit", "-q", "-a", "--amend", "--no-edit")
    return repo


def run_script(script, args, env=None, timeout=60, cwd=None):
    """(exit code, stdout + stderr) of `python -B script args`."""
    proc = subprocess.run([sys.executable, "-B", script] + list(args), capture_output=True, text=True,
                          env=env, timeout=timeout, cwd=cwd or ROOT, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout + proc.stderr
