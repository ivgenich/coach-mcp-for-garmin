#!/usr/bin/env python3
"""Run the unit tests and fail on any skip the leg does not allow.

    python -B scripts/ci-unittest.py                      # stdlib leg
    REQUIRE_UPSTREAM=1 python -B scripts/ci-unittest.py   # full leg

Standard library only; Python 3.10 or newer. Two suites, each in a child
process of its own, run the way their owners run them:

    ext            cd ext && python -m unittest
    scripts/tests  python -m unittest discover -s scripts/tests -t scripts/tests
                   (from the repository root; a note, not a failure, while
                   the folder does not exist)

Then it sorts every skip by its reason.

stdlib leg (REQUIRE_UPSTREAM unset or not "1"): a Python without mcp and
upstream garmin_mcp. A skip passes only when its reason says what the test
needs (STDLIB_REASON). Any other skip fails the run.

full leg (REQUIRE_UPSTREAM=1): upstream garmin_mcp, the mcp version that
constraints.txt pins and, once constraints.txt pins it, Garmin's Personal API
client at its pin must be installed, and a caddy binary must be found
(CADDY or PATH). A skip passes only when its suite, test id and reason are
all in FULL_ALLOWED, or when it is an optional feature's skip (OPTIONAL) and
that feature is not required. Any other skip fails the run: nothing that
needs upstream, mcp 2.x or caddy may skip silently.

The optional features (tracing, history search) have packages the image's
default pins leave out (config/requirements-telemetry.txt,
config/requirements-history.txt) and Postgres tests that need a database.
Their skips pass in the full leg unless the feature is required:

    REQUIRE_TELEMETRY=1   the OpenTelemetry SDK must be installed; its skips fail
    REQUIRE_HISTORY=1     psycopg and LangChain must be installed; their skips fail
    REQUIRE_DATABASE=1    COACH_TEST_DATABASE_URL must be set; its skips fail

Exit status: 0 when every test passed and every skip is allowed; 1 otherwise.
"""

from __future__ import annotations

import collections
import importlib.metadata
import importlib.util
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "ext")
CONSTRAINTS = os.path.join(ROOT, "constraints.txt")
# Garmin's Personal API client (key mode). It needs Python 3.12, so the stdlib
# leg never has it; the full leg must have it at its pin once one exists.
KEY_CLIENT = "garmin-connect-personal-api-python"

# name: (folder discovery starts in and imports from, working directory)
SUITES = {
    "ext": (EXT, EXT),
    "scripts/tests": (os.path.join(ROOT, "scripts", "tests"), ROOT),
}
REQUIRED = {"ext"}  # a missing folder fails the run; any other is a note

# The only skips the full leg allows: (suite, test id, reason), all exact.
FULL_ALLOWED = {
    # skipIf(HAVE_UPSTREAM): this test covers the path without upstream. The
    # stdlib leg runs it.
    ("ext", "test_daily_status.EndToEnd.test_without_upstream_workouts_are_unstructured_guesses",
     "covers the plain-python path"),
}

# An optional feature's skip, by its reason: (the variable that requires the feature, reason pattern).
# A reason naming COACH_TEST_DATABASE_URL is the database's, whatever else it names (checked first).
OPTIONAL = {
    "database": ("REQUIRE_DATABASE", re.compile(r"^needs .*\bCOACH_TEST_DATABASE_URL\b")),
    "telemetry": ("REQUIRE_TELEMETRY", re.compile(r"^needs (the )?(opentelemetry|telemetry)", re.I)),
    "history": ("REQUIRE_HISTORY", re.compile(
        r"^needs (the )?(history packages|psycopg|langchain|onnxruntime|numpy|tokenizers|pgvector)", re.I)),
}
# What a required feature needs: modules that must import, or a variable that must be set.
REQUIRED_MODULES = {
    "telemetry": ("opentelemetry.sdk", "opentelemetry.exporter.otlp.proto.http"),
    "history": ("psycopg", "langchain_core", "langchain_postgres"),
}
DATABASE_ENV = "COACH_TEST_DATABASE_URL"

# The skips the stdlib leg allows: a reason that names what the test needs.
STDLIB_REASON = re.compile(
    r"^(needs .+|garmin_mcp not installed|no caddy binary found|root ignores file modes)$"
)


def pinned(dist: str) -> str | None:
    """The version constraints.txt pins for dist (text after '#' ignored; names compared as pip does)."""
    want = re.sub(r"[-_.]+", "-", dist).lower()
    try:
        with open(CONSTRAINTS, encoding="utf-8") as f:
            for line in f:
                m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line.split("#", 1)[0].strip())
                if m and re.sub(r"[-_.]+", "-", m.group(1)).lower() == want:
                    return m.group(2)
    except OSError:
        return None
    return None


def installed(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def required_features() -> set[str]:
    """The optional features this run requires (REQUIRE_TELEMETRY / REQUIRE_HISTORY / REQUIRE_DATABASE = 1)."""
    return {name for name, (var, _) in OPTIONAL.items() if os.environ.get(var) == "1"}


def optional_feature(reason: str) -> str | None:
    """The optional feature a skip reason belongs to, or None. A reason naming COACH_TEST_DATABASE_URL is
    the database's ("needs psycopg and COACH_TEST_DATABASE_URL": REQUIRE_HISTORY=1 already makes psycopg
    importable, so only the database can be missing)."""
    found = [name for name, (_, rx) in OPTIONAL.items() if rx.search(reason)]
    return found[0] if found else None


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def feature_problems(required: set[str]) -> list[str]:
    """Problems with the environment for the required features; [] when it fits."""
    problems = []
    for name in sorted(required):
        if name == "database":
            if not os.environ.get(DATABASE_ENV):
                problems.append(f"REQUIRE_DATABASE=1 but {DATABASE_ENV} is not set")
            continue
        missing = [m for m in REQUIRED_MODULES[name] if not _importable(m)]
        if missing:
            problems.append(f"{OPTIONAL[name][0]}=1 but {', '.join(missing)} cannot be imported")
    return problems


def preflight(full: bool) -> list[str]:
    """Problems with the environment for this leg; [] when it fits."""
    problems = feature_problems(required_features())
    has_upstream = importlib.util.find_spec("garmin_mcp") is not None
    mcp = installed("mcp")
    client = installed(KEY_CLIENT)
    if full:
        want = pinned("mcp")
        if not has_upstream:
            problems.append("REQUIRE_UPSTREAM=1 but upstream garmin_mcp is not installed")
        if want is None:
            problems.append("cannot read the mcp pin from constraints.txt")
        elif mcp != want:
            problems.append(f"REQUIRE_UPSTREAM=1 but mcp is {mcp or 'not installed'} (constraints.txt pins {want})")
        want = pinned(KEY_CLIENT)
        if want is not None and client != want:
            problems.append(f"REQUIRE_UPSTREAM=1 but {KEY_CLIENT} is {client or 'not installed'} "
                            f"(constraints.txt pins {want})")
    elif has_upstream or mcp is not None or client is not None:
        problems.append(f"the stdlib leg needs a Python without mcp, upstream garmin_mcp and {KEY_CLIENT} "
                        "(set REQUIRE_UPSTREAM=1 for the full leg)")
    return problems


def run_suite(name: str, full: bool, leg: str) -> int:
    """Run one suite in this process; the parent starts one process per suite."""
    start, cwd = SUITES[name]
    os.chdir(cwd)
    suite = unittest.TestLoader().discover(start_dir=start, top_level_dir=start)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)

    allowed, unexpected = [], []
    required = required_features()
    for test, reason in result.skipped:
        if full:
            feature = optional_feature(reason)
            ok = (name, test.id(), reason) in FULL_ALLOWED or (feature is not None and feature not in required)
        else:
            ok = bool(STDLIB_REASON.match(reason))
        (allowed if ok else unexpected).append((test.id(), reason))

    by_reason = collections.Counter(reason for _, reason in allowed)
    for reason, n in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"ci-unittest: {name}: allowed skip x{n}: {reason}")
    for test_id, reason in unexpected:
        print(f"ci-unittest: {name}: UNEXPECTED SKIP {test_id}: {reason}")

    bad = []
    if result.testsRun == 0:
        bad.append("no tests ran")
    if result.failures or result.errors or result.unexpectedSuccesses:
        bad.append("tests failed")
    if unexpected:
        bad.append(f"{len(unexpected)} unexpected skips")
    print(f"ci-unittest: {leg} leg, {name}, python {sys.version.split()[0]}: ran {result.testsRun}, "
          f"failures {len(result.failures)}, errors {len(result.errors)}, "
          f"skipped {len(result.skipped)} (allowed {len(allowed)}, unexpected {len(unexpected)}): "
          + ("FAIL (" + "; ".join(bad) + ")" if bad else "PASS"), flush=True)
    return 1 if bad else 0


def main() -> int:
    full = os.environ.get("REQUIRE_UPSTREAM") == "1"
    leg = "full" if full else "stdlib"
    if len(sys.argv) == 3 and sys.argv[1] == "--suite" and sys.argv[2] in SUITES:
        return run_suite(sys.argv[2], full, leg)
    if len(sys.argv) != 1:
        print("usage: [REQUIRE_UPSTREAM=1] [REQUIRE_TELEMETRY=1] [REQUIRE_HISTORY=1] [REQUIRE_DATABASE=1] "
              "python -B scripts/ci-unittest.py", file=sys.stderr)
        return 2

    problems = preflight(full)
    for p in problems:
        print(f"ci-unittest: {leg} leg: {p}", file=sys.stderr)
    if problems:
        return 1

    failed = []
    for name, (start, _) in SUITES.items():
        if not os.path.isdir(start):
            if name in REQUIRED:
                print(f"ci-unittest: {name}: the folder is missing")
                failed.append(name)
            else:
                print(f"ci-unittest: {name}: not present, nothing to run")
            continue
        sys.stdout.flush()
        child = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), "--suite", name], check=False)
        if child.returncode != 0:
            failed.append(name)
    print(f"ci-unittest: {leg} leg: " + (f"FAIL ({', '.join(failed)})" if failed else "PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
