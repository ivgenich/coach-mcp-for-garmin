"""Deep tests (release test phase): failing plan tool calls log no row values, and a caller without the
plan:write scope (here: no auth context at all) never reaches the store or Garmin through a write tool.

    cd ext && python -m unittest test_deep_config_logs

Standard library only: the real plan_tools / plan_writes registration with a fake store and a fake Garmin
client; the tool functions are called directly.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import unittest

import feature_config
import plan_store
import plan_tools
import plan_tracking

from test_launch_support import captured_records

MARK = "TEST-ONLY-ROW-9c1e"
HEX32 = "0123456789abcdef" * 2
TOKEN_BODY = "A" * 10 + "z" * 10 + "0123456789" + "-_"


class FailingStore:
    """Every store method raises StoreError carrying a row-like value, and is recorded."""

    def __init__(self, error=plan_store.StoreError):
        self.calls = []
        self.error = error

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append(name)
            if name == "ready":
                return True
            raise self.error(f"row value {MARK} plan_id={MARK}")

        return method


class GarminSpy:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        self.calls.append(name)
        raise AssertionError("Garmin was called")


def register(store, *, writes=True, tracking=True, may_write=None):
    tools = {}

    def adder(fn, name=None, description=None, **kwargs):
        tools[name or fn.__name__] = fn

    env = {"GARMIN_AUTH": "full", "HISTORY_SEARCH": "on", "PLAN_TOOLS": "on",
           "PLAN_WRITES": "on" if writes else "off", "PLAN_TRACKING": "on" if tracking else "off",
           "DATABASE_URL": "postgresql://u:TEST-ONLY@db.example.internal/x"}
    cfg = feature_config.load(env)
    garmin = GarminSpy()
    options = {} if may_write is None else {"may_write": may_write}
    plan_tools.register(adder, cfg, lambda: garmin, writes=writes, store=store, today=lambda: dt.date(2026, 11, 25),
                        write_options=options)
    return tools, garmin


def call(fn, **kwargs):
    return asyncio.run(fn(**kwargs))


class FailingCallsLogNoValues(unittest.TestCase):
    def setUp(self):
        self.addCleanup(plan_tracking.configure, None)

    def test_store_failures_log_the_class_name_only(self):
        for error in (plan_store.StoreError, ValueError):
            store = FailingStore(error)
            tools, garmin = register(store, may_write=lambda: True)
            calls = {
                "plan_get": {"plan_id": ""},
                "log_feedback": {"workout_key": "w01-long", "rpe": 5, "pain": 0, "pain_location": "none",
                                 "sharp": False, "plan_id": ""},
                "plan_commit": {"plan_id": HEX32, "commit_token": "ct1." + TOKEN_BODY},
                "plan_cancel": {"plan_id": HEX32, "cancel_token": "xt1." + TOKEN_BODY},
            }
            if error is ValueError:
                calls.pop("plan_get")   # plan_get catches StoreError only; others are the server's internal error
            for name, arguments in calls.items():
                with self.subTest(error=error.__name__, tool=name), captured_records("garmin_ext") as records:
                    try:
                        answer = call(tools[name], **arguments)
                    except Exception as exc:  # noqa: BLE001 - launch's wrapper would answer an internal error
                        self.fail(f"{name} raised {type(exc).__name__}")
                    self.assertNotIn(MARK, answer)
                    for record in records:
                        self.assertNotIn(MARK, record.getMessage())
                    self.assertTrue(any(error.__name__ in r.getMessage() for r in records), name)
            self.assertEqual(garmin.calls, [])


class NoAuthContextNeverWrites(unittest.TestCase):
    """The default may_write (plan_confirm.caller_may_write): with no auth context (stdio, tests) the write
    tools answer no_write_permission before the store or Garmin is touched."""

    def setUp(self):
        self.addCleanup(plan_tracking.configure, None)

    def test_commit_cancel_and_apply(self):
        store = FailingStore()
        tools, garmin = register(store)
        self.assertLessEqual({"plan_commit", "plan_cancel", "plan_apply_changes"}, set(tools))
        for name, arguments in (("plan_commit", {"plan_id": HEX32, "commit_token": "ct1." + TOKEN_BODY}),
                                ("plan_cancel", {"plan_id": HEX32, "cancel_token": "xt1." + TOKEN_BODY}),
                                ("plan_apply_changes", {"plan_id": HEX32, "apply_token": "at1." + TOKEN_BODY,
                                                        "change_ids": "c" + "0" * 16})):
            with self.subTest(tool=name):
                answer = json.loads(call(tools[name], **arguments))
                self.assertEqual((answer.get("status"), answer.get("reason")), ("refused", "no_write_permission"),
                                 answer)
        self.assertEqual([c for c in store.calls if c != "ready"], [])
        self.assertEqual(garmin.calls, [])

    def test_with_writes_off_no_write_tool_is_registered(self):
        tools, _ = register(FailingStore(), writes=False, tracking=True)
        self.assertFalse({"plan_commit", "plan_cancel", "plan_apply_changes"} & set(tools))
        self.assertIn("log_feedback", tools)
        tools, _ = register(FailingStore(), writes=True, tracking=False)
        self.assertNotIn("plan_apply_changes", tools)
        self.assertNotIn("log_feedback", tools)

    def test_a_caller_may_write_that_raises_counts_as_no(self):
        def broken():
            raise RuntimeError(MARK)

        store = FailingStore()
        tools, garmin = register(store, may_write=broken)
        with captured_records("garmin_ext") as records:
            try:
                answer = call(tools["plan_commit"], plan_id=HEX32, commit_token="ct1." + TOKEN_BODY)
            except RuntimeError:
                answer = None   # raised to launch's wrapper: an internal error, still no write
        if answer is not None:
            self.assertNotIn(MARK, answer)
        self.assertEqual([c for c in store.calls if c != "ready"], [])
        self.assertEqual(garmin.calls, [])
        self.assertFalse([r for r in records if MARK in r.getMessage()])


if __name__ == "__main__":
    unittest.main()
