#!/usr/bin/env python3
"""CI helper checks for coach-mcp-for-garmin. Standard library only; Python 3.10+.

    python -B scripts/ci-checks.py install-full [--dry-run]
        Install upstream garmin-mcp and the image's packages into this Python
        exactly as the Dockerfile does: the upstream tarball at
        GARMIN_MCP_REF with --no-deps, then the Dockerfile's
        `pip install -c constraints.txt ...` list, then any hashed
        `pip install --require-hashes --no-deps -r <file>` (the file the
        Dockerfile COPYs there, e.g. config/garmin-key-client.txt). All are
        read from the Dockerfile, so the full test leg can never drift from
        the image.
    python -B scripts/ci-checks.py verify-env [--freeze FILE --pip-check FILE]
        The installed packages are exactly constraints.txt (plus upstream at
        the Dockerfile's GARMIN_MCP_REF, and pip itself), and `pip check`
        reports only the deliberate conflicts in EXPECTED_CONFLICTS. Without
        options it checks this Python; with them, the output of
        `pip freeze --all` and `pip check` saved from the image.
    python -B scripts/ci-checks.py text-scan
        No hidden characters (bidirectional controls, zero-width characters,
        other control characters except tab and newline) in any file git
        tracks or would add.
    python -B scripts/ci-checks.py pin-drift
        The Caddy and Python versions CI pins equal the Dockerfile's.
    python -B scripts/ci-checks.py version [--print]
        ext/version.py holds one VERSION = "X.Y.Z"; on a tag push
        (GITHUB_REF_TYPE=tag) the tag must be vX.Y.Z. With --print: print
        X.Y.Z alone (for scripts/ci-smoke.sh).
    python -B scripts/ci-checks.py compose
        compose.yaml resolves with `docker compose config` (the CLI only, no
        daemon) and stub settings, and the resolved services keep its rules:
        no published port, PORT and EDGE_CLIENT_IP_HEADER fixed whatever .env
        says, tunnel.env only for the tunnel, .env only for the app, a volume
        at /data. Without compose.yaml there is nothing to check.
    python -B scripts/ci-checks.py audit
        pip-audit (installed in this Python) on constraints.txt, with the
        reviewed exceptions in AUDIT_IGNORES (each has a review-by date; one
        that matches no finding any more fails, so it gets removed).

Each command prints one line per problem and a last line that ends in PASS or
FAIL. Exit status 0 means PASS.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
CONSTRAINTS = os.path.join(ROOT, "constraints.txt")
CI_TOOLS = os.path.join(ROOT, "scripts", "ci-tools.sh")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")
VERSION_FILE = os.path.join(ROOT, "ext", "version.py")
COMPOSE = os.path.join(ROOT, "compose.yaml")
# Stub settings for the compose check: nothing secret. PORT and
# EDGE_CLIENT_IP_HEADER here must lose to the values compose.yaml fixes.
COMPOSE_STUB_ENV = {"GARMIN_AUTH": "full", "PORT": "9999", "EDGE_CLIENT_IP_HEADER": "X-Real-IP"}
COMPOSE_FIXED = {"PORT": "8080", "EDGE_CLIENT_IP_HEADER": "CF-Connecting-IP"}

UPSTREAM = "garmin-mcp"
UPSTREAM_URL = "https://github.com/Taxuspt/garmin_mcp/archive/{ref}.tar.gz"
CONSTRAINTS_IN_IMAGE = "/tmp/constraints.txt"  # where the Dockerfile copies constraints.txt
# Packages that come with Python or pip, at any version.
BOOTSTRAP = {"pip", "setuptools", "wheel"}
# `pip check` lines that are deliberate (constraints.txt explains each one).
EXPECTED_CONFLICTS = (
    # Upstream caps mcp<2; the image runs mcp 2.x through ext/mcp2_compat.py.
    re.compile(r"^garmin-mcp \S+ has requirement mcp(?=\S*<2)\S*, but you have mcp 2\.\d+\.\d+\.$"),
)
# Advisories pip-audit may skip: (id, review-by date, reason). After the date
# the audit fails until someone reviews the entry and moves the date.
AUDIT_IGNORES = (
    ("PYSEC-2026-3467", "2027-02-15",
     "garminconnect<=0.3.4 writes its token file with mode 0644 (GHSA-wjhr-76vg-2hvc). Upstream "
     "pins garminconnect==0.3.2; the image keeps the tokens in a 0700 directory in a one-user container."),
)
# Characters text-scan refuses besides the Cc/Cf/Zl/Zp/Co/Cs categories: none today.
ALLOWED_CONTROLS = {"\t", "\n"}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def say(msg: str) -> None:
    print(f"ci-checks: {msg}", flush=True)


def finish(cmd: str, problems: list[str]) -> int:
    for p in problems:
        say(f"{cmd}: {p}")
    say(f"{cmd}: {'FAIL' if problems else 'PASS'}")
    return 1 if problems else 0


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# --- the Dockerfile ----------------------------------------------------------------------

def dockerfile_ref(text: str) -> str:
    refs = re.findall(r"^ARG GARMIN_MCP_REF=([0-9a-f]{40})$", text, re.M)
    if len(refs) != 1:
        raise ValueError("the Dockerfile must have exactly one `ARG GARMIN_MCP_REF=<40-hex commit>` line")
    return refs[0]


def dockerfile_copies(text: str) -> dict[str, str]:
    """{path in the image: path in the repository} for each one-file `COPY src dest`."""
    copies = {}
    for m in re.finditer(r"^COPY (?!--)(\S+) (/\S+)$", text, re.M):
        copies[m.group(2)] = os.path.join(ROOT, m.group(1))
    return copies


def dockerfile_installs(text: str) -> tuple[str, list[str], list[list[str]]]:
    """(the upstream requirement, the list after `pip install -c /tmp/constraints.txt`,
    the arguments of each hashed `pip install --require-hashes ... -r FILE`, FILE
    mapped back to the repository file the Dockerfile copies there)."""
    ref = dockerfile_ref(text)
    copies = dockerfile_copies(text)
    joined = re.sub(r"\\\n", " ", text)
    upstream, reqs, hashed = None, None, []
    for line in joined.splitlines():
        if not line.startswith("RUN "):
            continue
        for command in line[4:].split("&&"):
            words = shlex.split(command)
            if words[:2] != ["pip", "install"]:
                continue
            if words[:3] == ["pip", "install", "--no-deps"]:
                upstream = [w for w in words[3:] if w.startswith(UPSTREAM + " @ ")]
            elif words[:4] == ["pip", "install", "-c", CONSTRAINTS_IN_IMAGE]:
                reqs = words[4:]
            elif "--require-hashes" in words and "-r" in words:
                args = [copies.get(w, w) for w in words[2:]]
                missing = [w for w in words[2:] if w.startswith("/") and w not in copies]
                if missing:
                    raise ValueError(f"the Dockerfile installs {missing[0]}, which no one-file COPY puts there")
                hashed.append(args)
            else:
                raise ValueError(f"the Dockerfile runs a pip install CI does not know: {command.strip()!r}")
    want = f"{UPSTREAM} @ {UPSTREAM_URL.format(ref='${GARMIN_MCP_REF}')}"
    if not upstream or upstream != [want]:
        raise ValueError(f"the Dockerfile must run `pip install --no-deps \"{want}\"`")
    if not reqs:
        raise ValueError(f"the Dockerfile must run `pip install -c {CONSTRAINTS_IN_IMAGE} <requirements>`")
    return want.replace("${GARMIN_MCP_REF}", ref), reqs, hashed


def constraints() -> dict[str, str]:
    pins = {}
    for line in read(CONSTRAINTS).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line)
            if not m:
                raise ValueError(f"constraints.txt: not an exact pin: {line!r}")
            pins[norm(m.group(1))] = m.group(2)
    return pins


# --- commands ----------------------------------------------------------------------------

def cmd_install_full(args) -> int:
    try:
        upstream, reqs, hashed = dockerfile_installs(read(DOCKERFILE))
    except (OSError, ValueError) as e:
        return finish("install-full", [str(e)])
    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    for cmd in [pip + ["--no-deps", upstream], pip + ["-c", CONSTRAINTS] + reqs] + [pip + h for h in hashed]:
        say("install-full: " + " ".join(shlex.quote(c) for c in cmd[1:]))
        if not args.dry_run and subprocess.run(cmd, check=False).returncode != 0:
            return finish("install-full", ["pip install failed"])
    return finish("install-full", [])


def pip_output(*args: str) -> list[str]:
    """stdout lines of `python -m pip <args>` in this Python (pip check exits 1 on a conflict)."""
    return subprocess.run([sys.executable, "-m", "pip", *args, "--disable-pip-version-check"],
                          capture_output=True, text=True, check=False).stdout.splitlines()


def cmd_verify_env(args) -> int:
    problems = []
    try:
        pins = constraints()
        ref = dockerfile_ref(read(DOCKERFILE))
    except (OSError, ValueError) as e:
        return finish("verify-env", [str(e)])
    if bool(args.freeze) != bool(args.pip_check):
        return finish("verify-env", ["pass --freeze and --pip-check together"])
    if args.freeze:
        freeze = read(args.freeze).splitlines()
        check = read(args.pip_check).splitlines()
    else:
        freeze = pip_output("freeze", "--all")
        check = pip_output("check")

    seen, upstream_url = {}, None
    for line in (x.strip() for x in freeze):
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:==(\S+)| @ (\S+))", line)
        if not m:
            problems.append(f"freeze line not understood: {line!r}")
            continue
        name = norm(m.group(1))
        if name == UPSTREAM:
            upstream_url = (m.group(3) or "").split("#", 1)[0]
        elif m.group(3):
            problems.append(f"{name} is installed from a URL, not pinned by constraints.txt")
        else:
            seen[name] = m.group(2)
    want_url = UPSTREAM_URL.format(ref=ref)
    if upstream_url != want_url:
        problems.append(f"upstream {UPSTREAM} is {upstream_url or 'not installed'}; want {want_url}")
    for name, version in sorted(seen.items()):
        if name in BOOTSTRAP:
            continue
        if name not in pins:
            problems.append(f"{name}=={version} is installed but not pinned by constraints.txt")
        elif pins[name] != version:
            problems.append(f"{name}=={version} is installed; constraints.txt pins {pins[name]}")
    for name in sorted(set(pins) - set(seen)):
        problems.append(f"constraints.txt pins {name}=={pins[name]}, which is not installed (a stale pin?)")

    lines = [x.strip() for x in check if x.strip() and not x.startswith("No broken requirements")]
    for line in lines:
        if not any(p.match(line) for p in EXPECTED_CONFLICTS):
            problems.append(f"pip check: unexpected: {line}")
    for p in EXPECTED_CONFLICTS:
        if not any(p.match(line) for line in lines):
            say(f"verify-env: NOTE: the deliberate conflict {p.pattern!r} is gone; update constraints.txt's comment "
                "and EXPECTED_CONFLICTS")
    say(f"verify-env: {len(seen)} packages, {len(pins)} pins, upstream at {ref[:12]}, "
        f"{len(lines)} pip check lines")
    return finish("verify-env", problems)


def git_files() -> list[str]:
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, check=True).stdout
    return sorted({p for p in out.decode("utf-8", "surrogateescape").split("\0") if p})


def cmd_text_scan(args) -> int:
    problems, binary, scanned = [], 0, 0
    try:
        files = git_files()
    except (OSError, subprocess.CalledProcessError) as e:
        return finish("text-scan", [f"git ls-files failed: {e}"])
    for rel in files:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            binary += 1
            continue
        if "\0" in text:
            binary += 1
            continue
        scanned += 1
        for n, line in enumerate(text.split("\n"), 1):
            for col, ch in enumerate(line, 1):
                if ch not in ALLOWED_CONTROLS and unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs"):
                    name = unicodedata.name(ch, "control character")
                    problems.append(f"{rel}:{n}:{col}: U+{ord(ch):04X} {name}; write it as an escape such as "
                                    f"\\u{ord(ch):04x}")
    say(f"text-scan: {scanned} text files scanned, {binary} binary files skipped")
    return finish("text-scan", problems[:50] + ([f"... and {len(problems) - 50} more"] if len(problems) > 50 else []))


def cmd_pin_drift(args) -> int:
    problems = []
    try:
        docker, tools, workflow = read(DOCKERFILE), read(CI_TOOLS), read(WORKFLOW)
    except OSError as e:
        return finish("pin-drift", [str(e)])
    pairs = (
        ("Caddy", re.findall(r"^FROM caddy:(\S+) AS caddy$", docker, re.M),
         re.findall(r"^caddy\s+(\S+)\s", tools, re.M), "scripts/ci-tools.sh"),
        ("Python", re.findall(r"^FROM python:(\d+\.\d+\.\d+)-slim$", docker, re.M),
         re.findall(r"^\s+FULL_PYTHON:\s+\"(\d+\.\d+\.\d+)\"(?:\s+#.*)?$", workflow, re.M),
         ".github/workflows/ci.yml"),
    )
    for what, image, ci, where in pairs:
        if len(set(image)) != 1 or len(set(ci)) != 1:
            problems.append(f"{what}: cannot read one version from the Dockerfile ({image}) and {where} ({ci})")
        elif image[0] != ci[0]:
            problems.append(f"{what}: the Dockerfile uses {image[0]} but {where} pins {ci[0]}; change both together")
        else:
            say(f"pin-drift: {what} {image[0]} in the Dockerfile and {where}")
    return finish("pin-drift", problems)


def cmd_version(args) -> int:
    """ext/version.py holds one VERSION literal X.Y.Z; on a tag push the tag is vX.Y.Z."""
    try:
        tree = ast.parse(read(VERSION_FILE))
        values = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                  and [getattr(t, "id", None) for t in node.targets] == ["VERSION"]]
    except (OSError, SyntaxError, ValueError) as e:
        return finish("version", [f"cannot read VERSION from ext/version.py: {e}"])
    if len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(r"\d+\.\d+\.\d+", values[0]):
        return finish("version", ["ext/version.py must hold exactly one VERSION = \"X.Y.Z\""])
    version = values[0]
    if args.print:
        print(version)
        return 0
    say(f"version: ext/version.py says {version}")
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME", "")
        if tag != f"v{version}":
            return finish("version", [f"the tag {tag} is not v{version}; move the tag or fix ext/version.py"])
    return finish("version", [])


def cmd_compose(args) -> int:
    """compose.yaml resolves without a daemon, and the resolved services keep the rules of DEPLOY's design."""
    if not os.path.isfile(COMPOSE):
        say("compose: no compose.yaml, nothing to check")
        return finish("compose", [])
    docker = shutil.which("docker")
    if not docker:
        return finish("compose", ["the docker CLI is not installed (it needs no daemon for this check)"])
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    env["DOCKER_HOST"] = "unix:///nonexistent/docker.sock"  # proves that no daemon is used
    with tempfile.TemporaryDirectory(prefix="ci-compose-") as tmp:
        shutil.copyfile(COMPOSE, os.path.join(tmp, "compose.yaml"))
        with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
            f.write("".join(f"{k}={v}\n" for k, v in COMPOSE_STUB_ENV.items()))
        with open(os.path.join(tmp, "tunnel.env"), "w", encoding="utf-8") as f:
            f.write("TUNNEL_TOKEN=ci-placeholder\n")
        r = subprocess.run([docker, "compose", "-f", os.path.join(tmp, "compose.yaml"), "config", "--format", "json"],
                           capture_output=True, text=True, env=env, check=False)
    if r.returncode != 0:
        return finish("compose", [f"docker compose config failed (exit status {r.returncode}): "
                                  + " ".join(r.stderr.split())[:300]])
    try:
        services = json.loads(r.stdout)["services"]
        app, tunnel = services["app"], services["tunnel"]
    except (ValueError, KeyError, TypeError):
        return finish("compose", ["the resolved config has no services app and tunnel"])
    problems = []
    for name, svc in sorted(services.items()):
        if svc.get("ports"):
            problems.append(f"{name}: publishes a port; the tunnel must be the only way in")
        if svc.get("network_mode"):
            problems.append(f"{name}: sets network_mode {svc['network_mode']!r}")
    app_env, tunnel_env = app.get("environment") or {}, tunnel.get("environment") or {}
    for key, want in COMPOSE_FIXED.items():
        if app_env.get(key) != want:
            problems.append(f"app: {key} resolves to {app_env.get(key)!r} when .env sets it to "
                            f"{COMPOSE_STUB_ENV[key]!r}; compose.yaml must fix it to {want!r}")
    if "TUNNEL_TOKEN" in app_env:
        problems.append("app: gets TUNNEL_TOKEN; tunnel.env must reach the tunnel only")
    if "TUNNEL_TOKEN" not in tunnel_env:
        problems.append("tunnel: gets no TUNNEL_TOKEN from tunnel.env")
    leaked = sorted(set(COMPOSE_STUB_ENV) & set(tunnel_env))
    if leaked:
        problems.append(f"tunnel: gets {', '.join(leaked)} from .env; .env must reach the app only")
    if not any(v.get("type") == "volume" and v.get("target") == "/data" for v in app.get("volumes") or []):
        problems.append("app: no named volume mounted at /data")
    say(f"compose: services {', '.join(sorted(services))}; checked with stub .env and tunnel.env, no daemon")
    return finish("compose", problems)


def cmd_audit(args) -> int:
    """pip-audit on constraints.txt. A finding fails unless AUDIT_IGNORES names it (by id or alias) and its
    review date has not passed; an exception that matches nothing any more fails too, so none lingers."""
    problems, reasons = [], {}
    for vuln, review_by, reason in AUDIT_IGNORES:
        reasons[vuln] = reason
        if dt.date.fromisoformat(review_by) < dt.date.today():
            problems.append(f"the exception for {vuln} passed its review date {review_by}: review it ({reason})")
    with tempfile.TemporaryDirectory(prefix="ci-audit-") as tmp:
        report = os.path.join(tmp, "audit.json")
        cmd = [sys.executable, "-m", "pip_audit", "--requirement", CONSTRAINTS, "--no-deps", "--disable-pip",
               "--strict", "--progress-spinner", "off", "--format", "json", "--output", report]
        say("audit: pip-audit " + " ".join(shlex.quote(c) for c in cmd[3:-2]))
        rc = subprocess.run(cmd, check=False).returncode
        try:
            data = json.loads(read(report))
        except (OSError, ValueError):
            return finish("audit", problems + [f"pip-audit wrote no report (exit status {rc}); see above"])
    used, findings, audited = set(), set(), 0
    for dep in data.get("dependencies", []):
        if dep.get("skip_reason"):
            problems.append(f"{dep.get('name')}: not audited: {dep['skip_reason']}")
            continue
        audited += 1
        for v in dep.get("vulns", []):
            ids = {v.get("id"), *(v.get("aliases") or [])}
            line = (f"{dep.get('name')} {dep.get('version')}: {v.get('id')} "
                    f"({', '.join(sorted(v.get('aliases') or [])) or 'no alias'}); "
                    f"fixed in {', '.join(v.get('fix_versions') or []) or 'no release yet'}")
            if line in findings:
                continue  # pip-audit can list one finding twice
            findings.add(line)
            hit = sorted(ids & set(reasons))
            if hit:
                used.update(hit)
                say(f"audit: reviewed exception {hit[0]}: {line}")
            else:
                problems.append(f"known vulnerability: {line}")
    for vuln in sorted(set(reasons) - used):
        problems.append(f"the exception for {vuln} matches no finding any more: remove it from AUDIT_IGNORES")
    if rc != 0 and not findings and not problems:
        problems.append(f"pip-audit exited with status {rc} and no finding; see above")
    say(f"audit: pinned packages audited: {audited}; findings: {len(findings)}")
    return finish("audit", problems)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("install-full")
    s.add_argument("--dry-run", action="store_true", help="print the pip commands only")
    s = sub.add_parser("verify-env")
    s.add_argument("--freeze", help="a saved `pip freeze --all` output (from the image)")
    s.add_argument("--pip-check", help="a saved `pip check` output (from the image)")
    sub.add_parser("text-scan")
    sub.add_parser("pin-drift")
    s = sub.add_parser("version")
    s.add_argument("--print", action="store_true", help="print X.Y.Z alone")
    sub.add_parser("compose")
    sub.add_parser("audit")
    args = p.parse_args()
    return {"install-full": cmd_install_full, "verify-env": cmd_verify_env, "text-scan": cmd_text_scan,
            "pin-drift": cmd_pin_drift, "version": cmd_version, "compose": cmd_compose,
            "audit": cmd_audit}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
