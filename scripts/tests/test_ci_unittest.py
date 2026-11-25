"""Tests for scripts/ci-unittest.py: the isolation check (IsolationResult, the fixture check in run_suite)
and the skip rules each leg applies.

Standard library only; no network. The inner test runs use small throwaway TestCases and stop every
thread they start, so this suite leaves nothing behind for the outer check either.
"""

import importlib.util
import io
import logging
import os
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def load():
    spec = importlib.util.spec_from_file_location("ci_unittest", os.path.join(ROOT, "scripts", "ci-unittest.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cu = load()


class Stopper:
    """A thread that runs until stop(); the outer test stops it in its cleanups."""

    def __init__(self, test, name, daemon=True):
        self.event = threading.Event()
        self.thread = threading.Thread(target=self.event.wait, name=name, daemon=daemon)
        test.addCleanup(self.stop)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.event.set()
        self.thread.join(5)


def run_isolated(*tests, grace=0.2):
    """Run the given TestCase instances under IsolationResult; the result."""
    result = cu.IsolationResult(io.StringIO(), True, 0)
    result.grace = grace
    unittest.TestSuite(tests).run(result)
    return result


def messages(result):
    return [text for _, text in result.failures]


class Threads(unittest.TestCase):
    def test_a_raw_thread_is_skipped_not_joined(self):
        import _thread

        release, seen = threading.Event(), threading.Event()

        def body():
            threading.current_thread()  # registers a _DummyThread, which cannot be joined
            seen.set()
            release.wait(10)

        self.addCleanup(release.set)

        class Inner(unittest.TestCase):
            def test_leaks(self):
                _thread.start_new_thread(body, ())
                seen.wait(5)

        before = set(threading.enumerate())
        result = run_isolated(Inner("test_leaks"))
        # Joining it would raise and stop the run; it is skipped, and the run goes on.
        self.assertEqual((result.failures, result.errors), ([], []))
        self.assertTrue(any(isinstance(t, threading._DummyThread) for t in set(threading.enumerate()) - before))

    def test_a_thread_left_running_fails_the_test_and_names_thread_and_test(self):
        leak = Stopper(self, "leaky-worker")

        class Inner(unittest.TestCase):
            def test_leaks(self):
                leak.start()

        result = run_isolated(Inner("test_leaks"))
        self.assertEqual(len(result.failures), 1)
        text = messages(result)[0]
        self.assertIn("isolation:", text)
        self.assertIn("Inner.test_leaks", text)
        self.assertIn("'leaky-worker' (daemon", text)
        self.assertIn(leak.thread, result.reported_threads)

    def test_a_non_daemon_thread_is_named_as_one(self):
        leak = Stopper(self, "plain-worker", daemon=False)

        class Inner(unittest.TestCase):
            def test_leaks(self):
                leak.start()

        self.assertIn("'plain-worker' (non-daemon", messages(run_isolated(Inner("test_leaks")))[0])

    def test_a_thread_stopped_in_the_cleanups_passes(self):
        worker = Stopper(self, "tidy-worker")

        class Inner(unittest.TestCase):
            def test_tidy(self):
                worker.start()
                self.addCleanup(worker.stop)

        result = run_isolated(Inner("test_tidy"))
        self.assertTrue(result.wasSuccessful(), messages(result))

    def test_a_thread_that_ends_within_the_grace_join_passes(self):
        done = threading.Event()

        class Inner(unittest.TestCase):
            def test_short(self):
                threading.Thread(target=done.wait, args=(0.1,), name="short-lived", daemon=True).start()

        result = run_isolated(Inner("test_short"), grace=2.0)
        self.assertTrue(result.wasSuccessful(), messages(result))

    def test_a_thread_alive_before_the_test_is_not_counted(self):
        Stopper(self, "already-running").start()

        class Inner(unittest.TestCase):
            def test_nothing(self):
                pass

        result = run_isolated(Inner("test_nothing"))
        self.assertTrue(result.wasSuccessful(), messages(result))

    def test_a_leak_is_reported_once_not_again_by_the_next_test(self):
        leak = Stopper(self, "once-worker")

        class Inner(unittest.TestCase):
            def test_a_leaks(self):
                leak.start()

            def test_b_clean(self):
                pass

        result = run_isolated(Inner("test_a_leaks"), Inner("test_b_clean"))
        self.assertEqual([t.id().rsplit(".", 1)[1] for t, _ in result.failures], ["test_a_leaks"])

    def test_the_current_module_singleton_thread_is_allowed_a_replaced_one_is_not(self):
        current = Stopper(self, "mcp2-compat-tool-loop")
        old = Stopper(self, "mcp2-compat-tool-loop")
        fake = types.ModuleType("mcp2_compat")
        fake._tool_loop = types.SimpleNamespace(thread=current.thread)

        class Inner(unittest.TestCase):
            def test_starts_the_loop(self):
                current.start()

            def test_replaces_it_and_leaves_the_old_one(self):
                old.start()

        with mock.patch.dict(sys.modules, {"mcp2_compat": fake}):
            ok = run_isolated(Inner("test_starts_the_loop"))
            bad = run_isolated(Inner("test_replaces_it_and_leaves_the_old_one"))
        self.assertTrue(ok.wasSuccessful(), messages(ok))
        self.assertEqual(len(bad.failures), 1)

    def test_the_pg_engine_loop_and_its_executor_threads_are_allowed_others_are_not(self):
        loop_thread = Stopper(self, "Thread-1 (run_forever)")
        worker = Stopper(self, "asyncio_0")
        stranger = Stopper(self, "asyncio_1")
        engine = types.SimpleNamespace(
            _default_thread=loop_thread.thread,
            _default_loop=types.SimpleNamespace(_default_executor=types.SimpleNamespace(_threads={worker.thread})))
        fake = types.ModuleType("langchain_postgres.v2.engine")
        fake.PGEngine = engine

        class Inner(unittest.TestCase):
            def test_engine_threads(self):
                loop_thread.start()
                worker.start()

            def test_a_stranger(self):
                stranger.start()

        with mock.patch.dict(sys.modules, {"langchain_postgres.v2.engine": fake}):
            ok = run_isolated(Inner("test_engine_threads"))
            bad = run_isolated(Inner("test_a_stranger"))
        self.assertTrue(ok.wasSuccessful(), messages(ok))
        self.assertEqual(len(bad.failures), 1)

    def test_the_ignore_lists_stay_small(self):
        # A new entry needs a reason in the comment above it; this keeps the lists from growing quietly.
        self.assertLessEqual(len(cu.SINGLETON_THREADS), 3)
        self.assertLessEqual(len(cu.IGNORED_HANDLERS), 2)


class Handlers(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        saved = list(root.handlers)
        self.addCleanup(lambda: root.handlers.__setitem__(slice(None), saved))

    def test_a_handler_left_on_the_root_logger_fails_and_is_taken_off(self):
        handler = logging.NullHandler()

        class Inner(unittest.TestCase):
            def test_leaks(self):
                logging.getLogger().addHandler(handler)

        result = run_isolated(Inner("test_leaks"))
        self.assertEqual(len(result.failures), 1)
        self.assertIn("handler(s) on the root logger", messages(result)[0])
        self.assertNotIn(handler, logging.getLogger().handlers)

    def test_a_handler_removed_in_the_cleanups_passes(self):
        handler = logging.NullHandler()

        class Inner(unittest.TestCase):
            def test_tidy(self):
                logging.getLogger().addHandler(handler)
                self.addCleanup(logging.getLogger().removeHandler, handler)

        self.assertTrue(run_isolated(Inner("test_tidy")).wasSuccessful())

    def test_the_mcp_servers_rich_handler_is_ignored(self):
        rich = type("RichHandler", (logging.NullHandler,), {"__module__": "rich.logging"})()

        class Inner(unittest.TestCase):
            def test_builds_a_server(self):
                logging.getLogger().addHandler(rich)

        result = run_isolated(Inner("test_builds_a_server"))
        self.assertTrue(result.wasSuccessful(), messages(result))
        self.assertIn(rich, logging.getLogger().handlers)


FIXTURE_LEAK = '''
import threading
import unittest

STOP = threading.Event()


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        threading.Thread(target=STOP.wait, name="fixture-leak", daemon=True).start()

    def test_one(self):
        pass
'''


class RunSuite(unittest.TestCase):
    def run_suite(self, source, module):
        tmp = tempfile.mkdtemp(prefix="ci-unittest-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        with open(os.path.join(tmp, f"test_{module}.py"), "w", encoding="utf-8") as f:
            f.write(source)
        cwd, path = os.getcwd(), list(sys.path)
        self.addCleanup(os.chdir, cwd)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), path))
        self.addCleanup(sys.modules.pop, f"test_{module}", None)
        out = io.StringIO()
        with mock.patch.dict(cu.SUITES, {"tmp": (tmp, tmp)}), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cu.run_suite("tmp", False, "stdlib")
        return code, out.getvalue()

    def test_a_thread_a_class_fixture_leaves_fails_the_suite(self):
        try:
            code, out = self.run_suite(FIXTURE_LEAK, "ci_unittest_fixture_leak")
        finally:  # stop the fixture's thread (run_suite's cleanups drop the module later)
            mod = sys.modules.get("test_ci_unittest_fixture_leak")
            if mod is not None:
                mod.STOP.set()
            for t in threading.enumerate():
                if t.name == "fixture-leak":
                    t.join(5)
        self.assertEqual(code, 1)
        self.assertIn("LEAKED THREAD after the last test (a class or module fixture's): 'fixture-leak'", out)
        self.assertIn("threads leaked by fixtures", out)

    def test_a_clean_suite_passes(self):
        code, out = self.run_suite("import unittest\n\n\nclass Ok(unittest.TestCase):\n"
                                   "    def test_ok(self):\n        pass\n", "ci_unittest_clean")
        self.assertEqual(code, 0, out)
        self.assertIn(": PASS", out)


class Skips(unittest.TestCase):
    def test_needs_hypothesis_passes_in_the_stdlib_leg_only(self):
        self.assertTrue(cu.skip_allowed("ext", "test_x.T.test_p", "needs hypothesis", False, set()))
        self.assertFalse(cu.skip_allowed("ext", "test_x.T.test_p", "needs hypothesis", True, set()))
        self.assertFalse(cu.skip_allowed("ext", "test_x.T.test_p", "needs hypothesis",
                                         True, {"history", "telemetry", "database"}))

    def test_the_full_leg_needs_hypothesis_installed(self):
        real = cu._importable
        with mock.patch.object(cu, "_importable", lambda m: m != "hypothesis" and real(m)):
            problems = cu.preflight(True)
        self.assertIn("REQUIRE_UPSTREAM=1 but hypothesis is not installed (config/requirements-test.txt)", problems)


if __name__ == "__main__":
    unittest.main()
