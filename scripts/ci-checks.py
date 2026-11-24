#!/usr/bin/env python3
"""CI helper checks for coach-mcp-for-garmin. Standard library only; Python 3.10+.

    python -B scripts/ci-checks.py install-full [--dry-run]
        Install upstream garmin-mcp and the image's packages into this Python
        exactly as the Dockerfile does, every pip step in the Dockerfile's
        order: the hashed build backends (config/requirements-build.txt), the
        upstream tarball at GARMIN_MCP_REF with --no-deps
        --no-build-isolation, each hashed `pip install --require-hashes
        --no-deps -r <file>` (the file the Dockerfile COPYs there, e.g.
        config/requirements-core.txt), the `pip uninstall` of the build
        backends, and last the `pip install -c constraints.txt ...` list with
        PIP_NO_INDEX=1, which installs nothing. So every package comes in by
        hash, as in the image. All are read from the Dockerfile, so the full
        test leg can never drift from the image.
    python -B scripts/ci-checks.py verify-env [--freeze FILE --pip-check FILE]
        The installed packages are exactly constraints.txt (plus upstream at
        the Dockerfile's GARMIN_MCP_REF, and pip itself), and `pip check`
        reports only the deliberate conflicts in EXPECTED_CONFLICTS. Without
        options it checks this Python; with them, the output of
        `pip freeze --all` and `pip check` saved from the image.
    python -B scripts/ci-checks.py text-scan
        No hidden characters (bidirectional controls, zero-width characters,
        other control characters except tab and newline) in any file git
        tracks or would add. Grafana dashboards (dashboards/grafana/*.json)
        name no grafana.net address and hold no "uid" other than a ${DS_*}
        data source variable (an export from the UI carries the stack's).
    python -B scripts/ci-checks.py pin-drift
        The Caddy and Python versions CI pins equal the Dockerfile's. The
        hashed feature files (config/requirements-telemetry.txt and
        config/requirements-history.txt) pin every package with a hash, at
        the version constraints.txt pins when it pins it too (the telemetry
        file must be in constraints.txt whole), and every package of theirs
        has a reviewed licence in LICENSES. The build backends' file
        (config/requirements-build.txt) pins every package with a hash and
        shares none with constraints.txt (the Dockerfile uninstalls them).
    python -B scripts/ci-checks.py licenses
        The reviewed licences in LICENSES are allowed (ALLOWED_LICENSES, or
        an exception with a reason in LICENSE_EXCEPTIONS), and each feature
        package installed in this Python declares no other licence in its
        metadata. Packages not installed here are checked from the table.
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
        pip-audit (installed in this Python) on constraints.txt and the
        hashed feature files, with the
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
# The image's build backends: installed by hash before its two source builds, uninstalled after them.
BUILD_REQUIREMENTS = os.path.join(ROOT, "config", "requirements-build.txt")
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
# Hash-pinned files of the optional features, installed after constraints.txt's packages: (path, whether
# constraints.txt must pin every package of it). The telemetry pins go in every image, so the full test
# leg's verify-env needs them in constraints.txt; the history pins only in HISTORY=1 images.
FEATURE_REQUIREMENTS = (
    (os.path.join(ROOT, "config", "requirements-telemetry.txt"), True),
    (os.path.join(ROOT, "config", "requirements-history.txt"), False),
)
# Licence families allowed for the feature packages (SPDX ids, compared case-insensitively).
ALLOWED_LICENSES = {"mit", "bsd-2-clause", "bsd-3-clause", "0bsd", "apache-2.0", "psf-2.0", "isc", "mpl-2.0",
                    "zlib", "cc0-1.0"}
# Other licences, each reviewed: (SPDX id, reason).
LICENSE_EXCEPTIONS = {
    "lgpl-3.0-only": "psycopg is used unmodified as a separate, dynamically imported library; LGPL-3.0 then "
                     "puts no terms on this repo's own code",
}
# The reviewed licence of every package in FEATURE_REQUIREMENTS (SPDX expression, from the wheel's metadata
# when it has one; checked against the installed metadata by `licenses`). A new pin needs a row here.
LICENSES = {
    # telemetry (config/requirements-telemetry.txt)
    "googleapis-common-protos": "Apache-2.0",
    "opentelemetry-exporter-http-transport": "Apache-2.0",
    "opentelemetry-exporter-otlp-common": "Apache-2.0",
    "opentelemetry-exporter-otlp-proto-common": "Apache-2.0",
    "opentelemetry-exporter-otlp-proto-http": "Apache-2.0",
    "opentelemetry-proto": "Apache-2.0",
    "opentelemetry-sdk": "Apache-2.0",
    "opentelemetry-semantic-conventions": "Apache-2.0",
    "protobuf": "BSD-3-Clause",
    # history search (config/requirements-history.txt)
    "asyncpg": "Apache-2.0",
    "distro": "Apache-2.0",
    "flatbuffers": "Apache-2.0",
    "greenlet": "MIT AND PSF-2.0",
    "httpcore": "BSD-3-Clause",
    "httpx": "BSD-3-Clause",
    "jsonpatch": "BSD-3-Clause",
    "jsonpointer": "BSD-3-Clause",
    "langchain-classic": "MIT",
    "langchain-core": "MIT",
    "langchain-postgres": "MIT",
    "langchain-protocol": "MIT",
    "langchain-text-splitters": "MIT",
    "langsmith": "MIT",
    "numpy": "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
    "onnxruntime": "MIT",
    "orjson": "MPL-2.0 AND (Apache-2.0 OR MIT)",
    "packaging": "Apache-2.0 OR BSD-2-Clause",
    "pgvector": "MIT",
    "psycopg": "LGPL-3.0-only",
    "psycopg-binary": "LGPL-3.0-only",
    "psycopg-pool": "LGPL-3.0-only",
    "pyyaml": "MIT",
    "requests-toolbelt": "Apache-2.0",
    "sniffio": "MIT OR Apache-2.0",
    "sqlalchemy": "MIT",
    "tenacity": "Apache-2.0",
    "tokenizers": "Apache-2.0",
    "uuid-utils": "BSD-3-Clause",
    "websockets": "BSD-3-Clause",
    "xxhash": "BSD-2-Clause",
    "zstandard": "BSD-3-Clause",
}
# Grafana dashboards: exported JSON must not carry the stack's address or data source uids.
DASHBOARDS_GLOB = ("dashboards/grafana/", ".json")
DASHBOARD_UID_RE = re.compile(r"^\$\{DS_[A-Z0-9_]+\}$")
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


def dockerfile_installs(text: str) -> list[tuple[list[str], dict[str, str]]]:
    """Every pip step of the Dockerfile's RUN lines, in order, as (pip arguments after `pip`, extra
    environment): `pip install` and `pip uninstall`, files mapped back to the repository file the Dockerfile
    copies there, the upstream URL with GARMIN_MCP_REF filled in, and PIP_NO_INDEX=1 after `export
    PIP_NO_INDEX=1` in the same RUN. Lines with other shell words (the HISTORY=1 `if`) are not steps.
    Raises ValueError for a pip command CI does not know, or when the upstream install, the hashed
    installs and the offline `-c constraints.txt` re-check are missing or out of order."""
    ref = dockerfile_ref(text)
    copies = dockerfile_copies(text)
    joined = re.sub(r"\\\n", " ", text)
    want = f"{UPSTREAM} @ {UPSTREAM_URL.format(ref='${GARMIN_MCP_REF}')}"
    steps: list[tuple[list[str], dict[str, str]]] = []
    upstream_at = reqs_at = None
    hashed_at: list[int] = []

    def mapped(words: list[str]) -> list[str]:
        missing = [w for w in words if w.startswith("/") and w not in copies]
        if missing:
            raise ValueError(f"the Dockerfile installs {missing[0]}, which no one-file COPY puts there")
        return [copies.get(w, w) for w in words]

    for line in joined.splitlines():
        if not line.startswith("RUN "):
            continue
        env: dict[str, str] = {}
        for command in line[4:].split("&&"):
            words = shlex.split(command)
            if words == ["export", "PIP_NO_INDEX=1"]:
                env = {"PIP_NO_INDEX": "1"}
                continue
            if words[:1] != ["pip"]:
                continue
            if words[:3] == ["pip", "uninstall", "--yes"] and words[3:4] == ["-r"] and len(words) == 5:
                steps.append((["uninstall", "--yes", "-r"] + mapped(words[4:]), dict(env)))
            elif words[:2] != ["pip", "install"]:
                raise ValueError(f"the Dockerfile runs a pip command CI does not know: {command.strip()!r}")
            elif want in words:
                if words[2:] not in (["--no-deps", want], ["--no-deps", "--no-build-isolation", want]):
                    raise ValueError(f"the Dockerfile must run `pip install --no-deps [--no-build-isolation] "
                                     f"\"{want}\"`, nothing else in that step")
                upstream_at = len(steps)
                steps.append((words[1:-1] + [want.replace("${GARMIN_MCP_REF}", ref)], dict(env)))
            elif words[:4] == ["pip", "install", "-c", CONSTRAINTS_IN_IMAGE]:
                if env.get("PIP_NO_INDEX") != "1":
                    raise ValueError("the Dockerfile must run `export PIP_NO_INDEX=1` before "
                                     f"`pip install -c {CONSTRAINTS_IN_IMAGE} ...`")
                reqs_at = len(steps)
                steps.append((["install", "-c", CONSTRAINTS] + words[4:], dict(env)))
            elif "--require-hashes" in words and "-r" in words:
                hashed_at.append(len(steps))
                steps.append((["install"] + mapped(words[2:]), dict(env)))
            else:
                raise ValueError(f"the Dockerfile runs a pip install CI does not know: {command.strip()!r}")
    if upstream_at is None:
        raise ValueError(f"the Dockerfile must run `pip install --no-deps \"{want}\"`")
    if reqs_at is None or len(steps[reqs_at][0]) <= 3:
        raise ValueError(f"the Dockerfile must run `pip install -c {CONSTRAINTS_IN_IMAGE} <requirements>`")
    if not any(i < reqs_at for i in hashed_at):
        raise ValueError(f"the Dockerfile must install the hashed packages before `pip install -c "
                         f"{CONSTRAINTS_IN_IMAGE} ...`, so that step installs nothing from the index")
    return steps


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


def hashed_pins(path: str) -> tuple[dict[str, str], list[str]]:
    """({normalised name: version}, problems) of a `--require-hashes` file: every requirement is
    `name==version` followed by at least one --hash=sha256:<64 hex>."""
    rel = os.path.relpath(path, ROOT)
    pins, problems = {}, []
    joined = re.sub(r"\\\n", " ", read(path))
    for n, line in enumerate(joined.splitlines(), 1):
        line = "" if line.lstrip().startswith("#") else line.strip()
        if not line:
            continue
        words = line.split()
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!-]+)", words[0])
        hashes = [w for w in words[1:] if re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", w)]
        if not m or not hashes or len(hashes) != len(words) - 1:
            problems.append(f"{rel}: not a hash-pinned `name==version --hash=sha256:...` line: {words[0]!r}")
            continue
        name = norm(m.group(1))
        if name in pins:
            problems.append(f"{rel}: {name} is pinned twice")
        pins[name] = m.group(2)
    if not pins:
        problems.append(f"{rel}: no pins")
    return pins, problems


def feature_pin_problems(pins: dict[str, str]) -> list[str]:
    """pin-drift for FEATURE_REQUIREMENTS against constraints.txt's `pins`, and LICENSES coverage."""
    problems, seen = [], {}
    for path, whole in FEATURE_REQUIREMENTS:
        rel = os.path.relpath(path, ROOT)
        try:
            file_pins, bad = hashed_pins(path)
        except OSError as e:
            problems.append(f"{rel}: cannot read it ({e.strerror})")
            continue
        problems += bad
        for name, version in sorted(file_pins.items()):
            if name in pins and pins[name] != version:
                problems.append(f"{rel} pins {name}=={version} but constraints.txt pins {pins[name]}; "
                                "change both together")
            elif whole and name not in pins:
                problems.append(f"{rel} pins {name}=={version}, which constraints.txt does not pin")
            if name in seen and seen[name][1] != version:
                problems.append(f"{rel} pins {name}=={version} but {seen[name][0]} pins {seen[name][1]}")
            seen.setdefault(name, (rel, version))
            if name not in LICENSES:
                problems.append(f"{rel}: {name} has no reviewed licence in LICENSES (scripts/ci-checks.py)")
        say(f"pin-drift: {rel}: {len(file_pins)} hash-pinned packages")
    for name in sorted(set(LICENSES) - set(seen)):
        problems.append(f"LICENSES lists {name}, which no feature requirements file pins: remove the row")
    return problems


def build_pin_problems(pins: dict[str, str]) -> list[str]:
    """pin-drift for BUILD_REQUIREMENTS: hash-pinned, and no package constraints.txt pins (`pins`): the
    Dockerfile uninstalls every package of it after the builds, which would remove an image package."""
    rel = os.path.relpath(BUILD_REQUIREMENTS, ROOT)
    try:
        file_pins, problems = hashed_pins(BUILD_REQUIREMENTS)
    except OSError as e:
        return [f"{rel}: cannot read it ({e.strerror})"]
    for name in sorted(set(file_pins) & set(pins)):
        problems.append(f"{rel} pins {name}, which constraints.txt pins too: the Dockerfile uninstalls the build "
                        "backends after the builds, so the image would lose it")
    say(f"pin-drift: {rel}: {len(file_pins)} hash-pinned packages")
    return problems


def license_ids(expression: str) -> set[str]:
    """The SPDX ids of an expression ("MPL-2.0 AND (Apache-2.0 OR MIT)"), lowercased."""
    return {w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]*", expression)
            if w.upper() not in ("AND", "OR", "WITH")}


# Families read from free-text metadata (License: and classifiers) when there is no License-Expression.
_FAMILY_WORDS = (("lgpl", "lgpl"), ("lesser general public", "lgpl"), ("agpl", "agpl"),
                 ("affero", "agpl"), ("gpl", "gpl"), ("general public license", "gpl"), ("apache", "apache"),
                 ("bsd", "bsd"), ("mit", "mit"), ("mozilla", "mpl"), ("mpl", "mpl"), ("python software", "psf"),
                 ("psf", "psf"), ("isc", "isc"), ("zlib", "zlib"), ("cc0", "cc0"), ("public domain", "cc0"))


def license_family(spdx_id: str) -> str:
    low = spdx_id.lower()
    for word, family in _FAMILY_WORDS:
        if word in low:
            return family
    return low


def metadata_families(meta) -> set[str]:
    """Licence families an installed distribution's metadata declares (empty when it declares none)."""
    expression = meta.get("License-Expression")
    if expression:
        return {license_family(i) for i in license_ids(expression)}
    texts = [(meta.get("License") or "").split("\n", 1)[0][:200]]
    texts += [c.split("::")[-1] for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
    families = set()
    for text in texts:
        low = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
        for word, family in _FAMILY_WORDS:
            if re.search(rf"\b{word}\b", low):
                families.add(family)
                break
    families.discard("")
    return families


# --- commands ----------------------------------------------------------------------------

def cmd_install_full(args) -> int:
    try:
        steps = dockerfile_installs(read(DOCKERFILE))
    except (OSError, ValueError) as e:
        return finish("install-full", [str(e)])
    for words, env in steps:
        cmd = [sys.executable, "-m", "pip", words[0], "--disable-pip-version-check"] + words[1:]
        shown = " ".join(f"{k}={v}" for k, v in env.items())
        say("install-full: " + (shown + " " if shown else "") + " ".join(shlex.quote(c) for c in cmd[1:]))
        if not args.dry_run and subprocess.run(cmd, check=False, env={**os.environ, **env}).returncode != 0:
            return finish("install-full", [f"pip {words[0]} failed"])
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


def dashboard_problems(rel: str, text: str) -> list[str]:
    """A Grafana dashboard export names no grafana.net address and no uid but a ${DS_*} variable."""
    problems = []
    for n, line in enumerate(text.split("\n"), 1):
        if "grafana.net" in line.lower():
            problems.append(f"{rel}:{n}: names a grafana.net address; export the dashboard for sharing "
                            "(no stack URL)")
    try:
        data = json.loads(text)
    except ValueError as e:
        return problems + [f"{rel}: not valid JSON ({e.msg} at line {e.lineno})"]
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "uid" and value is not None and not (isinstance(value, str)
                                                               and DASHBOARD_UID_RE.fullmatch(value)):
                    problems.append(f"{rel}: a uid that is not a ${{DS_*}} data source variable: {value!r}"[:200])
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return problems


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
        if rel.startswith(DASHBOARDS_GLOB[0]) and rel.endswith(DASHBOARDS_GLOB[1]) and "/" not in \
                rel[len(DASHBOARDS_GLOB[0]):]:
            problems += dashboard_problems(rel, text)
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
        ("Caddy", re.findall(r"^FROM caddy:([^@\s]+)(?:@sha256:[0-9a-f]{64})? AS caddy$", docker, re.M),
         re.findall(r"^caddy\s+(\S+)\s", tools, re.M), "scripts/ci-tools.sh"),
        ("Python", re.findall(r"^FROM python:(\d+\.\d+\.\d+)-slim(?:@sha256:[0-9a-f]{64})?$", docker, re.M),
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
    try:
        pins = constraints()
    except (OSError, ValueError) as e:
        return finish("pin-drift", problems + [str(e)])
    problems += feature_pin_problems(pins)
    problems += build_pin_problems(pins)
    return finish("pin-drift", problems)


def cmd_licenses(args) -> int:
    import importlib.metadata as md

    problems, checked = [], 0
    for name, expression in sorted(LICENSES.items()):
        for spdx in sorted(license_ids(expression)):
            if spdx not in ALLOWED_LICENSES and spdx not in LICENSE_EXCEPTIONS:
                problems.append(f"{name}: licence {spdx} is neither allowed nor a reviewed exception")
        try:
            meta = md.distribution(name).metadata
        except md.PackageNotFoundError:
            continue
        checked += 1
        declared = metadata_families(meta)
        reviewed = {license_family(i) for i in license_ids(expression)}
        if declared - reviewed:
            problems.append(f"{name}: the installed metadata declares {', '.join(sorted(declared - reviewed))}, "
                            f"not in the reviewed {expression!r}; review it and update LICENSES")
    for spdx, reason in sorted(LICENSE_EXCEPTIONS.items()):
        say(f"licenses: reviewed exception {spdx}: {reason}")
    say(f"licenses: {len(LICENSES)} reviewed packages, {checked} installed here and checked against their metadata")
    return finish("licenses", problems)


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
    """pip-audit on constraints.txt and FEATURE_REQUIREMENTS, one run per file: pip-audit refuses to mix a file
    without hashes (constraints.txt) with hash-pinned ones in one run. Any run that fails or writes no report
    fails the step. A finding fails unless AUDIT_IGNORES names it (by id or alias) and its review date has not
    passed; an exception that matches nothing any more fails too, so none lingers."""
    problems, reasons = [], {}
    for vuln, review_by, reason in AUDIT_IGNORES:
        reasons[vuln] = reason
        if dt.date.fromisoformat(review_by) < dt.date.today():
            problems.append(f"the exception for {vuln} passed its review date {review_by}: review it ({reason})")
    files = [CONSTRAINTS] + [path for path, _ in FEATURE_REQUIREMENTS]
    dependencies, failed_runs = [], []
    with tempfile.TemporaryDirectory(prefix="ci-audit-") as tmp:
        for i, path in enumerate(files):
            report = os.path.join(tmp, f"audit-{i}.json")
            cmd = [sys.executable, "-m", "pip_audit", "--requirement", path, "--no-deps", "--disable-pip",
                   "--strict", "--progress-spinner", "off", "--format", "json", "--output", report]
            say("audit: pip-audit " + " ".join(shlex.quote(c) for c in cmd[3:-2]))
            rc = subprocess.run(cmd, check=False).returncode
            try:
                data = json.loads(read(report))
            except (OSError, ValueError):
                problems.append(f"pip-audit wrote no report for {os.path.relpath(path, ROOT)} "
                                f"(exit status {rc}); see above")
                continue
            dependencies += data.get("dependencies", [])
            if rc != 0:
                failed_runs.append((os.path.relpath(path, ROOT), rc))
    used, findings, audited = set(), set(), 0
    for dep in dependencies:
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
    if failed_runs and not findings and not problems:
        problems += [f"pip-audit exited with status {rc} on {path} and no finding; see above"
                     for path, rc in failed_runs]
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
    sub.add_parser("licenses")
    args = p.parse_args()
    return {"install-full": cmd_install_full, "verify-env": cmd_verify_env, "text-scan": cmd_text_scan,
            "pin-drift": cmd_pin_drift, "version": cmd_version, "compose": cmd_compose,
            "audit": cmd_audit, "licenses": cmd_licenses}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
