"""Training plans in the eval server: the per-case `plan` fixture (evals/cases/plan.toml).

A case with plan = "memory" runs the server as with PLAN_TOOLS=on, but over plan_store.MemoryPlanStore instead
of Postgres: plan_assess and plan_preview read the fake Garmin like every other tool, and plan_preview stores
its drafts in a store that lives as long as the server. No database, no network, no thread started.

How the tools join the server, for the life of the server only: launch._plan_tools_wanted() answers True,
launch.allowed_tools() adds launch.PLAN_READ_TOOLS (both gates then let them through, as PLAN_TOOLS=on does),
and plan_tools.store_from_config() returns the memory store, so the registration itself is launch's own
(_register_plan_tools and its adder). The clock is the server's frozen one (status_common.today_local).
The store's history.meta records the world's Garmin account (as the history index would), so plan_get
finds the drafts plan_preview stored. Standard library only.

A case with plan = "tracked" runs as "memory" with PLAN_TRACKING=on too: launch's features read plan_tracking
on, so launch serves log_feedback (hinted additive, idempotent, local-only) and plan_tools.register points
plan_tracking.configured_store() at the same memory store, which get_daily_status and get_weekly_status then
read. The store holds TRACKED_PLAN, active since 2026-11-16: yesterday's long run (2026-11-22, which the world's
14 km run completes, so its feedback is due) and today's easy run (2026-11-23, not run yet).

A case with plan = "reviewed" runs as "tracked" over REVIEWED_PLAN instead (three weeks from 2026-11-09, active
since then, its inputs a real plan_engine echo so plan_preview(accept_changes) can rebuild it) with the athlete's
feedback seeded: easy runs at RPE 6 in both past weeks and pain 5 (calf) after the long run of 2026-11-22. Stage 4's
plan_review_week is served (PLAN_TRACKING=on serves it); reviewing the week of 2026-11-16 proposes A4 (easy RPE)
and A6 (pain) changes for the week of 2026-11-23, and the week of 2026-11-09 none of those. Nothing is applied
unless the model calls plan_preview with accept_changes; plan_apply_changes needs writes, which stay off here.

The per-case `writes` fixture (needs plan = "memory") serves Stage 2's real plan_commit and plan_cancel as
PLAN_WRITES=on does: launch's features read plan_writes on, so launch itself registers the write tools (through
plan_tools' writes=True registration), hints them, selects server_instructions.INSTRUCTIONS_PLAN and admits the
plan's write paths. What stands in for claude.ai:
- the caller's token carries the plan:write scope (plan_confirm.caller_may_write answers True);
- "on": the harness's MCP client declares no elicitation, so plan_confirm answers "unsupported" and a commit is
  confirmed by its token (confirmed_by "token");
- "declined": the client declares form elicitation (on a legacy-handshake session, which can carry the server's
  request) and declines every confirmation form (Server.elicitations keeps the messages);
- "committed": as "on", and seed() commits SEED_PREVIEW's next 7 days through the real tools before the first
  turn (its Garmin calls are then dropped from the log).
The executor's 1 s spacing between writes is wall-clock pacing only and is 0 here (plan_writes.SPACING_S).
"""

from __future__ import annotations

import json
from unittest import mock

FIXTURES = ("memory", "tracked", "reviewed", "off")
# The tool plan = "tracked" adds (launch.PLAN_TRACKING_TOOLS; test_cases checks it).
TRACKING_TOOLS = ("log_feedback",)
# The tool PLAN_TRACKING=on adds as a read tool (launch.PLAN_REVIEW_TOOLS), with plan = "tracked" or "reviewed".
REVIEW_TOOLS = ("plan_review_week",)
# plan = "tracked": the stored plan's weeks (plan_tracking.plan_sessions reads key, date, role, km, minutes).
TRACKED_PLAN = {"content_hash": "eval-tracked", "weeks": [
    {"week": 1, "start": "2026-11-16", "end": "2026-11-22", "sessions": [
        {"key": "w01-e1", "date": "2026-11-17", "role": "easy", "km": 8.0, "minutes": 45, "structure": "8 km easy"},
        {"key": "w01-q1", "date": "2026-11-19", "role": "threshold", "km": 10.0, "minutes": 55,
         "structure": "10 km with 3 x 8 min threshold"},
        {"key": "w01-long", "date": "2026-11-22", "role": "long", "km": 14.0, "minutes": 80,
         "structure": "14 km long"}]},
    {"week": 2, "start": "2026-11-23", "end": "2026-11-29", "sessions": [
        {"key": "w02-e1", "date": "2026-11-23", "role": "easy", "km": 8.0, "minutes": 45, "structure": "8 km easy"},
        {"key": "w02-q1", "date": "2026-11-25", "role": "threshold", "km": 10.0, "minutes": 55,
         "structure": "10 km with 3 x 8 min threshold"},
        {"key": "w02-long", "date": "2026-11-29", "role": "long", "km": 16.0, "minutes": 90,
         "structure": "16 km long"}]}]}
# plan = "reviewed": week 0 (2026-11-09) before TRACKED_PLAN's two weeks; the world's runs complete weeks 0 and 1.
REVIEWED_PLAN = {"content_hash": "eval-reviewed", "weeks": [
    {"week": 0, "start": "2026-11-09", "end": "2026-11-15", "sessions": [
        {"key": "w00-e1", "date": "2026-11-10", "role": "easy", "km": 8.0, "minutes": 45, "structure": "8 km easy"},
        {"key": "w00-q1", "date": "2026-11-12", "role": "threshold", "km": 10.0, "minutes": 55,
         "structure": "10 km with 3 x 8 min threshold"},
        {"key": "w00-long", "date": "2026-11-15", "role": "long", "km": 14.0, "minutes": 80,
         "structure": "14 km long"}]}] + TRACKED_PLAN["weeks"]}
REVIEWED_SINCE = "2026-11-09T06:00:00+00:00"
# (workout_key, rpe, pain, pain_location, sharp)
REVIEWED_FEEDBACK = (("w00-e1", 6, 0, "none", False), ("w01-e1", 6, 0, "none", False),
                     ("w01-long", 6, 5, "calf", False))
TRACKED_SINCE = "2026-11-16T08:00:00+00:00"   # created and activated (before the world's runs of that week)
TRACKED_NOW = "2026-11-23T09:00:00+00:00"     # the plan world's anchor (Europe/London is UTC in November)
# The `writes` fixture (evals/cases.py WRITES_FIXTURES is the same tuple; test_cases checks it).
WRITES_FIXTURES = ("off", "on", "declined", "committed")
WRITE_TOOLS = ("plan_commit", "plan_cancel")
PROFILE_ID = "71000001"   # get_stats userProfileId in the plan worlds (evals/harness/base_sets.py)
# What "committed" commits before the model's first turn: the plan cases' Tel Aviv plan, the next 7 days.
SEED_PREVIEW = {"race_date": "2027-02-26", "goal_time": "3:30:00", "b_races": "2027-01-29:tri_half",
                "run_days": "tue,wed,thu,sat,sun", "long_run_day": "sun", "commit_days": 7}


def install(stack, launch, writes="off", plan="memory") -> object:
    """Register the plan read tools over a fresh MemoryPlanStore in the next launch.main() (patches undone by
    `stack`), with `writes` other than "off" the real write tools too, and with plan "tracked" plan tracking and
    log_feedback over TRACKED_PLAN. `launch` is ext/launch.py, already imported. Returns the store."""
    import plan_store
    import plan_tools

    if writes not in WRITES_FIXTURES:
        raise ValueError(f"writes must be one of {WRITES_FIXTURES}, got {writes!r}")
    if plan not in FIXTURES or plan == "off":
        raise ValueError(f"plan must be 'memory', 'tracked' or 'reviewed', got {plan!r}")
    tracked = plan in ("tracked", "reviewed")
    store = plan_store.MemoryPlanStore(clock=_Clock()) if tracked else plan_store.MemoryPlanStore()
    store.meta[plan_store.META_ACCOUNT] = store.account_fp(PROFILE_ID)
    if plan == "tracked":
        _seed_tracked(store)
    elif plan == "reviewed":
        _seed_reviewed(store)
    real_allowed = launch.allowed_tools
    extra = (launch.PLAN_READ_TOOLS | (launch.PLAN_WRITE_TOOLS if writes != "off" else frozenset())
             | (launch.PLAN_TRACKING_TOOLS | launch.PLAN_REVIEW_TOOLS if tracked else frozenset()))

    def allowed_tools(environ):
        return frozenset(real_allowed(environ)) | extra

    stack.enter_context(mock.patch.object(launch, "allowed_tools", allowed_tools))
    stack.enter_context(mock.patch.object(launch, "_plan_tools_wanted", lambda features: True))
    stack.enter_context(mock.patch.object(plan_tools, "store_from_config", lambda cfg: store))
    if writes != "off" or tracked:
        _install_features(stack, launch, writes=writes != "off", tracking=tracked)
    return store


class _Clock:
    """The tracked store's clock: TRACKED_SINCE while seeding, then the world's anchor (TRACKED_NOW)."""

    def __init__(self):
        import datetime as dt

        self.now = dt.datetime.fromisoformat(TRACKED_SINCE)

    def __call__(self):
        return self.now


def _seed_tracked(store):
    """TRACKED_PLAN as the account's active plan, created and activated at TRACKED_SINCE."""
    import datetime as dt
    import hashlib

    fp = store.account_fp(PROFILE_ID)
    race = dt.date(2027, 2, 26)
    pid, _, _ = store.save_preview(fp, None, race, 1, {"race_date": race.isoformat()}, TRACKED_PLAN,
                                   hashlib.sha256(b"eval-tracked").hexdigest())
    store.move_plan(pid, "active")
    store._clock.now = dt.datetime.fromisoformat(TRACKED_NOW)


def _seed_reviewed(store):
    """REVIEWED_PLAN as the account's active plan since REVIEWED_SINCE, with REVIEWED_FEEDBACK stored."""
    import datetime as dt
    import hashlib

    import plan_engine as pe

    fp = store.account_fp(PROFILE_ID)
    race = dt.date(2027, 2, 26)
    store._clock.now = dt.datetime.fromisoformat(REVIEWED_SINCE)
    inputs = pe.PlanInputs(race_date=race, goal_time_s=3 * 3600 + 30 * 60,
                           b_races=(pe.BRace(dt.date(2027, 1, 29), "tri_half"),)).echo()
    pid, _, _ = store.save_preview(fp, None, race, pe.RULES_VERSION, inputs, REVIEWED_PLAN,
                                   hashlib.sha256(b"eval-reviewed").hexdigest())
    store.move_plan(pid, "active")
    for key, rpe, pain, where, sharp in REVIEWED_FEEDBACK:
        store.save_feedback(pid, key, rpe, pain, where, sharp)
    store._clock.now = dt.datetime.fromisoformat(TRACKED_NOW)


def _install_features(stack, launch, *, writes, tracking):
    """PLAN_WRITES=on (with the claude.ai stand-ins of the module docstring) and/or PLAN_TRACKING=on for the next
    launch.main()."""
    import dataclasses

    import garmin_paths
    import plan_tracking

    feature_config = launch.feature_config
    real_load = feature_config.load
    switches = {"plan_writes": True} if writes else {}
    if tracking:
        switches["plan_tracking"] = True

    def load(environ):
        return dataclasses.replace(real_load(environ), **switches)

    stack.callback(setattr, launch, "_FEATURES", launch._FEATURES)
    stack.enter_context(mock.patch.object(feature_config, "load", load))
    if tracking:
        stack.callback(plan_tracking.configure, None)
    if writes:
        import plan_confirm
        import plan_writes

        stack.callback(garmin_paths.allow_plan_writes, garmin_paths.allowed_write_kinds())
        stack.enter_context(mock.patch.object(plan_confirm, "caller_may_write", lambda: True))
        stack.enter_context(mock.patch.object(plan_writes, "SPACING_S", 0))


async def decline(context, params):
    """The elicitation callback of the "declined" fixture: the user declines every confirmation form."""
    from mcp import types

    return types.ElicitResult(action="decline")


def seed(call):
    """For "committed": preview SEED_PREVIEW and commit it through the real tools. `call(name, args)` is the server's
    tool call (a ToolOutcome). Raises RuntimeError when the seed does not end done."""
    outcome = call("plan_preview", dict(SEED_PREVIEW))
    answer = json.loads(outcome.text) if not outcome.is_error and outcome.text.startswith("{") else {}
    token = (answer.get("commit") or {}).get("token")
    if not token:
        raise RuntimeError(f"committed: the seed preview gave no commit token: {outcome.text[:200]}")
    for _ in range(4):   # 10 writes: the write budget (8 per call) needs a second call, retry_after_s 0
        outcome = call("plan_commit", {"plan_id": answer["stored"]["plan_id"], "commit_token": token})
        result = json.loads(outcome.text) if not outcome.is_error and outcome.text.startswith("{") else {}
        if result.get("status") != "partial" or result.get("retry_after_s") != 0:
            break
    if result.get("status") != "done":
        raise RuntimeError(f"committed: the seed commit failed: {outcome.text[:200]}")
