"""Deep write tests through the served app: the confirmation of plan_commit and plan_cancel over the real server
launch.main() builds (PLAN_WRITES=on, the launch patch path, call_tool's wrapper, the call guard, mcp2_compat and
the SDK's request-state boundary), called in process with the SDK Client.

    cd ext && python -m unittest test_deep_writes_served

- protocol 2026-07-28 (Client mode "auto"): the input-required round. The Client's elicitation_callback answers
  the form and the Client calls the tool again by itself; client.session.call_tool(allow_input_required=True)
  drives the rounds by hand (forged answers, a changed argument, a tampered state).
- protocol 2025-11-25 (mode "legacy"): the elicitation request during the call, with the same enum form.

The plan store is an in-memory one and Garmin is test_deep_writes_base's fake: nothing reaches Postgres or
Garmin. Needs upstream garmin_mcp on mcp 2.x.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import json
import os
import sys
import time
import types
import unittest
from unittest import mock

import garmin_guard
import garmin_paths
import plan_store as ps
import plan_writes as pw
import test_deep_writes_base as b
from test_launch_support import (fake_db_url, fake_modules, FULL, HAVE_UPSTREAM, load_launch, quiet,
                                 UPSTREAM_MODULES, UpstreamCase)


def token_key():
    import base64
    import secrets

    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def fake_guard_pg():
    module = types.ModuleType("garmin_guard_pg")
    module.PgSharedState = lambda dsn, tz, live=False: garmin_guard.MemoryState()
    return module


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class ServedConfirmation(UpstreamCase):
    def setUp(self):
        super().setUp()
        import plan_tracking

        quiet(self, "mcp", "garmin_mcp", "garminconnect", "garmin_ext")
        fake_modules(self, garmin_guard_pg=fake_guard_pg())
        self.addCleanup(garmin_paths.allow_plan_writes, ())
        self.addCleanup(plan_tracking.configure, None)
        self.mono = [0.0]
        self.garmin = b.DeepGarmin(self.mono)
        self.addCleanup(self._release)
        self.store = ps.MemoryPlanStore(clock=b.tpw.Clock())
        self.fp = self.store.account_fp(b.PROFILE)
        self.store.meta[ps.META_ACCOUNT] = self.fp
        self.asked = []
        self.server = self.serve()

    def _release(self):
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))

    def serve(self):
        import garmin_mcp
        import key_mode
        import plan_confirm
        import plan_tools
        from mcp.server.mcpserver import MCPServer

        self.launch = launch = load_launch(self)
        self.addCleanup(setattr, sys, "stdout", sys.stdout)
        for name in UPSTREAM_MODULES:
            module = getattr(garmin_mcp, name)
            self.addCleanup(setattr, module, "garmin_client", getattr(module, "garmin_client", None))
        env = {**FULL, "GARMIN_MCP_TRANSPORT": "stdio", "GARMIN_MCP_STATELESS_HTTP": "false", "REPORT_TZ": "UTC",
               "HISTORY_SEARCH": "on", "PLAN_TOOLS": "on", "PLAN_WRITES": "on", "DATABASE_URL": fake_db_url(),
               "HISTORY_TOKEN_KEY": token_key()}
        served = []
        start = garmin_mcp._PendingGarminClient.start
        # For the life of the test: the in-memory store, the day, and the claude.ai stand-ins (as the evals
        # harness): the caller's token carries plan:write; no spacing between writes.
        for target, attribute, value in ((plan_tools, "store_from_config", lambda cfg: self.store),
                                         (plan_tools, "_today", lambda: b.TODAY),
                                         (plan_confirm, "caller_may_write", lambda: True),
                                         (pw, "SPACING_S", 0)):
            patcher = mock.patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env))
            for name in ("GARMIN_ENABLED_TOOLS", "GARMIN_DISABLED_TOOLS", "GARMIN_PERSONAL_API_KEY",
                         "GARMIN_KEY_TOKEN_CACHE", "ROLE", "HISTORY_FORGET", "TRACING", "PLAN_TRACKING"):
                os.environ.pop(name, None)
            stack.enter_context(mock.patch.object(garmin_mcp._PendingGarminClient, "start",
                                                  lambda pending, login: start(pending, lambda: self.garmin)))
            stack.enter_context(mock.patch.object(MCPServer, "run", lambda s, *a, **k: served.append(s)))
            stack.enter_context(mock.patch.object(key_mode, "install", lambda *a, **k: None))
            stack.enter_context(mock.patch.object(garmin_guard, "install_refresh_lock", lambda *a, **k: True))
            stack.enter_context(mock.patch.object(garmin_guard, "install_atomic_token_dump", lambda *a, **k: True))
            for name in ("_start_token_publisher", "_start_catch_up", "_start_token_cleanup"):
                stack.enter_context(mock.patch.object(launch, name, lambda *a, **k: None))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            launch.main([])
            pending = garmin_mcp.activity_management.garmin_client._client
            deadline = time.monotonic() + 30
            while pending.login_state() == "pending" and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(pending.login_state(), "ready")
        (server,) = served
        return server

    # --- helpers ---------------------------------------------------------------------------------------------

    def preview(self, content="a"):
        answer = b.plan_answer(b.default_sessions(), content)
        pid, version, _ = self.store.save_preview(self.fp, None, dt.date(2027, 3, 5), 1, {"x": 1}, answer,
                                                  answer["content_hash"])
        snap = types.SimpleNamespace(fixed=(), lt_speed_mps=None)
        commit = pw.commit_section(self.store, answer, snap, {"plan_id": pid, "version": version}, b.TODAY, 14,
                                   b.PROFILE)
        return pid, commit

    def answering(self, choice=None, action="accept"):
        async def callback(context, params):
            import mcp_types

            self.asked.append((params.message, params.requested_schema))
            if action != "accept":
                return mcp_types.ElicitResult(action=action)
            enum = params.requested_schema["properties"]["confirm"]["enum"]
            return mcp_types.ElicitResult(action="accept", content={"confirm": enum[1] if choice is None else choice})

        return callback

    def client(self, mode="auto", callback=None):
        from mcp.client import Client

        if callback is None:
            return Client(self.server, mode=mode)
        return Client(self.server, mode=mode, elicitation_callback=callback)

    def call(self, name, args, mode="auto", callback=None):
        async def go():
            async with self.client(mode, callback) as client:
                return await client.call_tool(name, args)

        result = asyncio.run(go())
        self.assertFalse(result.is_error, result)
        return json.loads(result.content[0].text)

    def rounds(self, steps):
        """Drive tools/call by hand on one 2026 connection whose client declares form elicitation (the callback
        is never called: allow_input_required hands each InputRequiredResult back). steps is a list of
        fn(session, last) -> awaitable; returns the list of results (an exception counts as a result)."""
        async def go():
            out = []
            async with self.client("auto", self.answering()) as client:
                last = None
                for step in steps:
                    try:
                        last = await step(client.session, last)
                    except Exception as exc:  # noqa: BLE001 - the MCPError the boundary raises
                        last = exc
                    out.append(last)
            return out

        return asyncio.run(go())

    def writes(self):
        return [c for c in self.garmin.calls if c[0] != "READ"]

    def token_status(self, token):
        return self.store.tokens[ps.token_hash(token)]["status"]

    # --- 2026-07-28: the input-required round -----------------------------------------------------------------

    def test_accept_through_the_input_required_round(self):
        pid, commit = self.preview()
        out = self.call("plan_commit", {"plan_id": pid, "commit_token": commit["token"]},
                        callback=self.answering())
        self.assertEqual((out["status"], out["confirmed_by"]), ("done", "elicitation"), out)
        ((message, schema),) = self.asked
        self.assertTrue(message.startswith("Add 3 training-plan workouts to your Garmin Connect calendar?"), message)
        self.assertEqual(schema["properties"]["confirm"]["enum"], ["Don't add", "Add them"])
        self.assertEqual(len(self.garmin.uploads()), 3)
        self.assertEqual(self.token_status(commit["token"]), "done")

    def test_decline_and_cancel_write_nothing_and_keep_the_token(self):
        pid, commit = self.preview()
        for callback in (self.answering(action="decline"), self.answering(action="cancel"),
                         self.answering(choice="Don't add")):
            with self.subTest():
                out = self.call("plan_commit", {"plan_id": pid, "commit_token": commit["token"]}, callback=callback)
                self.assertEqual((out["status"], out["written"], out["confirmed_by"]), ("declined", [], None))
        self.assertEqual(self.garmin.calls, [])
        self.assertEqual(self.token_status(commit["token"]), "issued")
        out = self.call("plan_commit", {"plan_id": pid, "commit_token": commit["token"]}, callback=self.answering())
        self.assertEqual((out["status"], out["confirmed_by"]), ("done", "elicitation"), out)

    def test_round_one_by_hand_and_what_round_two_accepts(self):
        import mcp_types
        from mcp.shared.exceptions import MCPError

        pid, commit = self.preview()
        args = {"plan_id": pid, "commit_token": commit["token"]}
        yes = {"confirm": mcp_types.ElicitResult(action="accept", content={"confirm": "Add them"})}
        guard_outcomes = []
        real_after = self.launch._GUARD.after_outcome
        self.launch._GUARD.after_outcome = lambda caller, group: (guard_outcomes.append(group),
                                                                  real_after(caller, group))[1]
        self.addCleanup(setattr, self.launch._GUARD, "after_outcome", real_after)

        def call(arguments, **kwargs):
            return lambda session, last: session.call_tool("plan_commit", arguments, allow_input_required=True,
                                                           **kwargs)

        def again(arguments, responses, state=None):
            return lambda session, last: session.call_tool(
                "plan_commit", arguments, allow_input_required=True, input_responses=responses,
                request_state=last.request_state if state is None else state)

        other_pid, other = self.preview(content="b")
        results = self.rounds([
            call(args),                                         # round 1
            call(args, input_responses=yes),                    # a forged answer with no state: asked again
            again(args, yes, state="v1.forged"),                # a state the SDK did not seal: refused
            call(args),                                         # round 1 again
            again({"plan_id": other_pid, "commit_token": other["token"]}, yes),   # other arguments: refused
            call(args),
            again(args, {"confirm": mcp_types.ElicitResult(action="accept", content={"confirm": True})}),
            call(args),
            again(args, None),                                  # no answer at all
            call(args),
            again(args, yes),                                   # the real round 2
        ])
        first = results[0]
        self.assertIsInstance(first, mcp_types.InputRequiredResult)
        request = first.input_requests["confirm"]
        self.assertEqual(request.params.requested_schema["properties"]["confirm"]["enum"], ["Don't add", "Add them"])
        self.assertTrue(first.request_state)
        self.assertNotIn("pc1.", first.request_state, "the SDK seals the state")
        self.assertIsInstance(results[1], mcp_types.InputRequiredResult)
        self.assertIsInstance(results[2], MCPError)
        self.assertIsInstance(results[4], MCPError)
        self.assertEqual(results[4].error.message, "Invalid or expired requestState")
        for index in (6, 8):
            text = json.loads(results[index].content[0].text)
            self.assertEqual((text["status"], text["written"]), ("declined", []), index)
        # Until the real round 2 nothing reached Garmin and the token stayed unused.
        done = json.loads(results[10].content[0].text)
        self.assertEqual((done["status"], done["confirmed_by"]), ("done", "elicitation"), done)
        self.assertEqual(len(self.garmin.uploads()), 3)
        self.assertEqual(self.token_status(other["token"]), "issued")
        # The guard heard the two declined answers and the write, never a round 1 (no Garmin call there).
        self.assertEqual(guard_outcomes, [None, None, None])

    def test_round_one_alone_calls_no_garmin_and_claims_nothing(self):
        import mcp_types

        pid, commit = self.preview()
        (first,) = self.rounds([lambda session, last: session.call_tool(
            "plan_commit", {"plan_id": pid, "commit_token": commit["token"]}, allow_input_required=True)])
        self.assertIsInstance(first, mcp_types.InputRequiredResult)
        self.assertEqual(self.garmin.calls, [])
        self.assertEqual(self.token_status(commit["token"]), "issued")

    def test_cancel_with_a_token(self):
        pid, commit = self.preview()
        self.assertEqual(self.call("plan_commit", {"plan_id": pid, "commit_token": commit["token"]},
                                   callback=self.answering())["status"], "done")
        preview = self.call("plan_cancel", {"plan_id": pid})
        token = preview["cancel"]["token"]
        out = self.call("plan_cancel", {"plan_id": pid, "cancel_token": token},
                        callback=self.answering(choice="Don't remove"))
        self.assertEqual(out["status"], "declined")
        out = self.call("plan_cancel", {"plan_id": pid, "cancel_token": token}, callback=self.answering())
        self.assertEqual((out["status"], out["confirmed_by"]), ("done", "elicitation"), out)
        self.assertEqual(self.asked[-1][1]["properties"]["confirm"]["enum"], ["Don't remove", "Remove them"])

    def test_a_client_without_elicitation_keeps_the_token_path(self):
        pid, commit = self.preview()
        out = self.call("plan_commit", {"plan_id": pid, "commit_token": commit["token"]})
        self.assertEqual((out["status"], out["confirmed_by"]), ("done", "token"), out)

    # --- 2025-11-25: the elicitation request during the call ---------------------------------------------------

    def test_legacy_protocol_asks_with_the_same_enum(self):
        pid, commit = self.preview()
        out = self.call("plan_commit", {"plan_id": pid, "commit_token": commit["token"]}, mode="legacy",
                        callback=self.answering(choice="Don't add"))
        self.assertEqual(out["status"], "declined")
        out = self.call("plan_commit", {"plan_id": pid, "commit_token": commit["token"]}, mode="legacy",
                        callback=self.answering())
        self.assertEqual((out["status"], out["confirmed_by"]), ("done", "elicitation"), out)
        self.assertEqual([s["properties"]["confirm"]["enum"] for _m, s in self.asked],
                         [["Don't add", "Add them"]] * 2)

    def test_the_output_schema_is_that_of_a_text_answer(self):
        tools = {t.name: t for t in asyncio.run(self.server.list_tools())}
        for name in ("plan_commit", "plan_cancel"):
            self.assertEqual(tools[name].output_schema, tools["plan_get"].output_schema, name)


if __name__ == "__main__":
    unittest.main()
