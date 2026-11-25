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
constraints.txt pins, hypothesis (config/requirements-test.txt) and, once
constraints.txt pins it, Garmin's Personal API client at its pin must be
installed, and a caddy binary must be found (CADDY or PATH). A skip passes only when its suite, test id and reason are
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

Every leg also checks test isolation (IsolationResult): a thread a test
started that is still alive after its cleanups (after a short grace join), or
a handler it left on the root logger, fails that test; such a thread used to
log into the next test's capture and flake it. A thread a class or module
fixture leaves behind fails the suite once all its tests have run.

Exit status: 0 when every test passed, every skip is allowed and nothing
leaked; 1 otherwise.
"""

from __future__ import annotations

import collections
import importlib.metadata
import importlib.util
import logging
import os
import re
import subprocess
import sys
import threading
import time
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

# The skips the stdlib leg allows: a reason that names what the test needs ("needs hypothesis" among
# them: the stdlib leg has no hypothesis). The full and history legs install hypothesis, so there that
# skip matches neither FULL_ALLOWED nor OPTIONAL and fails the run.
STDLIB_REASON = re.compile(
    r"^(needs .+|garmin_mcp not installed|no caddy binary found|root ignores file modes)$"
)

# Isolation: how long the threads a test left running get, in all, to finish before they count as leaked.
GRACE_SECONDS = 2.0
# What a test may leave behind. Keep both lists short and say why: a test stops and joins what it
# started in its cleanups (addCleanup runs before the check). Threads and handlers that were there before
# the test started are never counted (whoever started a pool owns it).
#
# Process-wide threads a module keeps for the life of the process, started by whichever test uses them
# first: (module, the attribute path to the one Thread allowed now). Only the current one is allowed; a
# replaced one still alive is a leak.
SINGLETON_THREADS = (
    # mcp2_compat's tool loop: one asyncio loop on a daemon thread for every async tool, by design.
    ("mcp2_compat", ("_tool_loop", "thread")),
    # langchain-postgres: PGEngine.from_connection_string starts one class-wide asyncio loop on a daemon
    # thread for the process (history_retrieval caches one engine per database), and that loop's default
    # executor keeps its worker threads ("asyncio_N") for reuse. Only those exact objects are ignored.
    ("langchain_postgres.v2.engine", ("PGEngine", "_default_thread")),
    ("langchain_postgres.v2.engine", ("PGEngine", "_default_loop", "_default_executor", "_threads")),
)
# Handlers a library puts on the root logger, by module and class name.
IGNORED_HANDLERS = (
    # mcp's MCPServer()/FastMCP() calls logging.basicConfig(handlers=[RichHandler(...)]) when the root
    # logger has none: the server's own stderr handler, added again whenever a test cleared the root.
    ("rich.logging", "RichHandler"),
)


def ignored_thread(thread) -> bool:
    for module, path in SINGLETON_THREADS:
        obj = sys.modules.get(module)
        for name in path:
            obj = getattr(obj, name, None)
        if obj is thread or (isinstance(obj, (set, frozenset, list, tuple)) and thread in obj):
            return True
    return False


def ignored_handler(handler) -> bool:
    return (type(handler).__module__, type(handler).__qualname__) in IGNORED_HANDLERS


def leaked_threads(before, grace: float = GRACE_SECONDS) -> list:
    """The threads alive now that were not in before (a set of Thread objects) and are not ignored,
    after joining them for up to grace seconds in all."""
    deadline = time.monotonic() + grace
    leaked = []
    for thread in threading.enumerate():
        if thread in before or thread is threading.current_thread() or ignored_thread(thread):
            continue
        if isinstance(thread, threading._DummyThread):  # noqa: SLF001 - started outside threading
            # A raw _thread thread that called current_thread(): threading can neither join it nor tell
            # when it ends (the dummy entry outlives it), so it is skipped instead of crashing the run.
            continue
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            leaked.append(thread)
    return leaked


def describe(thread) -> str:
    return f"{thread.name!r} ({'daemon' if thread.daemon else 'non-daemon'}, target {_target(thread)})"


def _target(thread) -> str:
    target = getattr(thread, "_target", None)
    if target is None:
        return type(thread).__name__ + ".run"
    return getattr(target, "__qualname__", None) or repr(target)


class IsolationResult(unittest.TextTestResult):
    """A TextTestResult that fails a test which leaves a thread running or a handler on the root logger.

    The check runs in stopTest, after tearDown and every cleanup, so a test that stops what it started in
    its cleanups passes. A leaked handler is also taken off the root logger so later tests do not
    inherit it; a leaked thread cannot be stopped from here and is reported once."""

    grace = GRACE_SECONDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reported_threads = set()  # every thread a test was failed for
        self._threads_before = set()
        self._handlers_before = []

    def startTest(self, test):
        self._threads_before = set(threading.enumerate())
        self._handlers_before = list(logging.getLogger().handlers)
        super().startTest(test)

    def stopTest(self, test):
        problems = []
        threads = leaked_threads(self._threads_before, self.grace)
        self.reported_threads.update(threads)
        if threads:
            problems.append(f"left {len(threads)} thread(s) running after its cleanups: "
                            + ", ".join(describe(t) for t in threads)
                            + "; stop and join them in the test's cleanups")
        root = logging.getLogger()
        handlers = [h for h in root.handlers if h not in self._handlers_before and not ignored_handler(h)]
        if handlers:
            for h in handlers:
                root.removeHandler(h)
            problems.append(f"left {len(handlers)} handler(s) on the root logger: "
                            + ", ".join(repr(h) for h in handlers) + "; remove them in the test's cleanups")
        if problems:
            msg = f"isolation: {test.id()} " + "; it also ".join(problems)
            self.addFailure(test, (AssertionError, AssertionError(msg), None))
        super().stopTest(test)


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
        if not _importable("hypothesis"):
            # The property tests skip without it ("needs hypothesis"): only the stdlib leg allows that.
            problems.append("REQUIRE_UPSTREAM=1 but hypothesis is not installed (config/requirements-test.txt)")
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


def skip_allowed(name: str, test_id: str, reason: str, full: bool, required: set[str]) -> bool:
    """Whether this leg allows the skip of test_id in suite name (see the module docstring)."""
    if full:
        feature = optional_feature(reason)
        return (name, test_id, reason) in FULL_ALLOWED or (feature is not None and feature not in required)
    return bool(STDLIB_REASON.match(reason))


def run_suite(name: str, full: bool, leg: str) -> int:
    """Run one suite in this process; the parent starts one process per suite."""
    start, cwd = SUITES[name]
    os.chdir(cwd)
    suite = unittest.TestLoader().discover(start_dir=start, top_level_dir=start)
    baseline = set(threading.enumerate())  # after discovery: a module-level pool belongs to its module
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1, resultclass=IsolationResult).run(suite)
    # What a class or module fixture left running (a test's own leaks were reported, and are not again).
    fixture_leaks = leaked_threads(baseline | result.reported_threads)
    for t in fixture_leaks:
        print(f"ci-unittest: {name}: LEAKED THREAD after the last test (a class or module fixture's): "
              f"{describe(t)}")

    allowed, unexpected = [], []
    required = required_features()
    for test, reason in result.skipped:
        ok = skip_allowed(name, test.id(), reason, full, required)
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
    if fixture_leaks:
        bad.append(f"{len(fixture_leaks)} threads leaked by fixtures")
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
