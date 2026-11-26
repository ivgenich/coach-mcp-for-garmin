"""plan_review_week (PLAN_TRACKING=on, Stage 4): the weekly review of the tracked plan.

It reads Garmin for the reviewed week (the activities from two weeks before it to today, the calendar for
Garmin's own links, HRV of four weeks, the week's nights, Garmin's training status at the week's end, and,
only when the plan has a quality session today, today's daily status for its level), refreshes the plan's
links over those days (plan_tracking.refresh_plan), reads the stored feedback, and hands it all to
plan_review.review_week's fixed rules. The proposed changes are stored in plan_changes (proposed); nothing
in the plan changes until the user accepts them: plan_preview(accept_changes=...) patches the stored
version with them (plan_review.apply_changes), and with PLAN_WRITES=on the answer's apply token lets plan_apply_changes write that version's
workouts to Garmin.

Annotation: read-only (launch.OWN_READ_TOOLS). Storing the proposals, the refreshed links and (PLAN_WRITES=on)
the apply token's row is the server's own bookkeeping in its database, the same as get_daily_status refreshing the links: nothing the
user sees in Garmin or in the plan changes, and calling it again changes nothing but the proposals' numbers.

Errors: a bad argument answers "Invalid input: <fixed detail>" (never the value); a Garmin failure of the
first call answers "Error: <source phrase>"; a database failure answers a fixed "No data:" text. Nothing
here logs a value from Garmin or from the caller.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import daily_status as ds
import plan_engine as pe
import plan_review as pr
import plan_store as ps
import plan_tools as pt
import plan_tracking
import status_common as sc
import weekly_status as ws

log = logging.getLogger("garmin_ext.plans")

TOOL_NAME = "plan_review_week"
ACTIVITY_LIMIT = 100
HISTORY_DAYS = 14      # activities read from this many days before the reviewed week
REFRESH_BACK_DAYS = 8  # links refreshed from this many days before the week (the previous week settles too)

NOTE = ("Every metric, change, reason and number was computed by the server's fixed review rules (rules_version). "
        "Explain each change with its reason as given and change no number. Nothing in the plan has changed: "
        "only after the user approves a change, call plan_preview with plan_id and accept_changes (its "
        "change_id) to build a new plan version. Training guidance, not medical advice.")
NOTE_APPLY = (" With the user's approval of the apply.change_ids listed, plan_apply_changes(plan_id, apply_token, "
              "change_ids) writes that version to the Garmin calendar; never call it without that approval.")
DESCRIPTION = (
    "Review one week of the tracked training plan and propose changes for the next week by fixed rules: "
    "completion of the planned km, HRV and short nights, Garmin's ACWR (never the only reason), a missed long "
    "run, easy-run RPE, quality sessions run faster than planned, pain, a missed week and a red day. Each "
    "change has a change_id, a reason sentence with its number, and evidence (metric, value, threshold). "
    "Changes only hold or reduce load. Nothing changes until the user approves: then call plan_preview with "
    "plan_id and accept_changes. Reads Garmin and the plan database; stores the proposals only.\n\n"
    "Arguments: plan_id (empty: the tracked plan); week_start, the Monday of the week to review as YYYY-MM-DD "
    "(empty: the last week that has ended, or this week on a Sunday).")
DESCRIPTION_APPLY = (
    " Garmin writes are on: the answer may carry apply {token, change_ids, ops}; plan_apply_changes writes the "
    "changed workouts with it, only after the user approved exactly those changes in this conversation.")
TEXT_NO_PLAN = "No data: there is no open plan to review. Call plan_preview to build one."


def default_week(today: dt.date) -> dt.date:
    """The last week that has ended (Monday to Sunday), or this week on its Sunday."""
    return pe.monday(today) if today.weekday() == 6 else pe.monday(today) - dt.timedelta(days=7)


def parse_week_start(value: Any, today: dt.date) -> dt.date:
    text = str(value or "").strip()
    if not text:
        return default_week(today)
    day = pt.parse_date(text, "week_start")
    if day.weekday() != 0:
        raise pt.BadInput("week_start must be a Monday")
    if day > pe.monday(today):
        raise pt.BadInput("week_start must be a week that has begun")
    return day


def review_calls(client: Any, week_start: dt.date, today: dt.date) -> Dict[str, sc.Call]:
    end = min(week_start + dt.timedelta(days=6), today)
    calls: Dict[str, sc.Call] = {"stats": sc.call(sc.client_method(client, "get_stats"), today.isoformat())}
    for back in ws.HRV_WEEKS:
        day = end - dt.timedelta(days=back)
        calls[f"hrv@{day.isoformat()}"] = sc.call(sc.client_method(client, "get_hrv_data"), day.isoformat())
    for offset in range((end - week_start).days + 1):
        day = week_start + dt.timedelta(days=offset)
        calls[f"sleep@{day.isoformat()}"] = sc.call(sc.client_method(client, "get_sleep_data"), day.isoformat())
    calls["activities"] = sc.call(sc.activities_call(client), params={
        "startDate": (week_start - dt.timedelta(days=HISTORY_DAYS)).isoformat(), "endDate": today.isoformat(),
        "start": "0", "limit": str(ACTIVITY_LIMIT)})
    calls["training_status"] = sc.call(sc.client_method(client, "get_training_status"), end.isoformat())
    calls["schedule"] = sc.call(sc.client_method(client, "query_garmin_graphql"), ds.schedule_query(
        (week_start - dt.timedelta(days=REFRESH_BACK_DAYS)).isoformat(), today.isoformat()))
    return calls


def health_of(data: Mapping[str, Any], week_start: dt.date, today: dt.date) -> pr.Health:
    """HRV, nights, Garmin's ACWR and weekly_status' flags for the reviewed week (no level, no volume)."""
    end = min(week_start + dt.timedelta(days=6), today)
    hrv = ws.hrv_section([ws.parse_hrv(data.get(f"hrv@{(end - dt.timedelta(days=b)).isoformat()}"))
                          for b in ws.HRV_WEEKS])
    nights = [ws.parse_sleep(data.get(f"sleep@{(week_start + dt.timedelta(days=i)).isoformat()}"),
                             week_start + dt.timedelta(days=i)) for i in range((end - week_start).days + 1)]
    sleep = ws.sleep_section(nights)
    ts = ws.parse_training_status(data.get("training_status"))
    flags = ws.evaluate_flags({"hrv": hrv, "sleep": sleep, "load": ws.load_section(ts, {})})
    return pr.Health(flags=tuple(f["id"] for f in flags), hrv_7d=hrv.get("avg_7d"), hrv_28d=hrv.get("avg_28d"),
                     short_nights=sleep.get("nights_under_6h"), acwr=ts.get("acwr"))


def activities_cut(raw: Any) -> bool:
    """True when the activity search filled its one page (ACTIVITY_LIMIT items): the oldest days may be cut."""
    return len(ws._activity_items(raw)) >= ACTIVITY_LIMIT


def runs_of(activities: Any) -> Dict[int, pr.Run]:
    """Activity id -> its km and average speed (Garmin's averageSpeed, else distance over duration)."""
    out: Dict[int, pr.Run] = {}
    for a in ws._activity_items(activities):
        aid = plan_tracking._int_id(a.get("activityId"))
        if aid is None:
            continue
        meters, secs, speed = sc.num(a.get("distance")), sc.num(a.get("duration")), sc.num(a.get("averageSpeed"))
        if (speed is None or speed <= 0) and meters and secs and secs > 0:
            speed = meters / secs
        out[aid] = pr.Run(pr.Km(round(meters / 1000.0, 2)) if meters and meters > 0 else None,
                          pe.MetersPerSecond(float(speed)) if speed and 0 < speed < 12 else None)
    return out


def change_rows(review: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """plan_store.save_changes items: the shown from/to with the rule, and the edits beside the shown to."""
    out = []
    for c in review["changes"]:
        to_value: Dict[str, Any] = {"value": c["to"], "edits": c["edits"]}
        if c.get("advice"):
            to_value["advice"] = c["advice"]
        out.append({"change_id": c["change_id"], "kind": c["kind"], "from_value": {"value": c["from"],
                                                                                   "rule": c["rule"]},
                    "to_value": to_value, "reason": c["reason"], "evidence": c["evidence"]})
    return out


def change_out(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A stored change as the tools show it (never its edits)."""
    frm, to = row.get("from_value") or {}, row.get("to_value") or {}
    out = {"change_id": row["change_id"], "rule": frm.get("rule"), "kind": row["kind"], "from": frm.get("value"),
           "to": to.get("value"), "reason": row["reason"], "evidence": row.get("evidence") or {},
           "status": row["status"]}
    if to.get("advice"):
        out["advice"] = to["advice"]
    return out


def change_for_apply(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A stored change as plan_review.apply_changes takes it."""
    return {"change_id": row["change_id"], "kind": row["kind"], "edits": (row.get("to_value") or {}).get("edits") or []}


def recheck(body: Mapping[str, Any], lt_speed: Optional[float]) -> Dict[str, Any]:
    """A (patched) version with plan_dto's workout checks run again on its sessions, in place of the results
    of those checks the stored version carries from its own preview."""
    import plan_dto

    ids = {str(c["id"]) for c in plan_dto.check_plan(dict(body), lt_speed)}
    answer = dict(body)
    inv = dict(answer.get("invariants") or {})
    inv["checked"] = [i for i in inv.get("checked") or [] if i not in ids]
    inv["failed"] = [f for f in inv.get("failed") or [] if str(f.get("id")) not in ids]
    inv["ok"] = not inv["failed"]
    answer["invariants"] = inv
    return pt.with_dto_checks(answer, lt_speed)


def patched(stored: Mapping[str, Any], changes: List[Dict[str, Any]], today: dt.date, done: Mapping[str, float],
            lt_speed: Optional[float]) -> Tuple[Dict[str, Any], List[str], List[Dict[str, Any]], List[str]]:
    """plan_review.apply_changes, then plan_dto's workout checks on the patched version. The changes are taken
    in order: one after which a check fails that the stored version passed is refused with that check and the
    rest go on without it. (answer, accepted, refused, applied); the answer carries the checks only when a
    change applied."""
    base = {str(f.get("id")) for f in recheck(stored, lt_speed)["invariants"]["failed"]}
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for change in changes:
        trial, _acc, _ref, applied = pr.apply_changes(stored, kept + [change], today, done)
        if change["change_id"] in applied:
            new = [f for f in recheck(trial, lt_speed)["invariants"]["failed"] if str(f.get("id")) not in base]
            if new:
                dropped.append({"change_id": change["change_id"], "rule": str(new[0].get("id")),
                                "value": new[0].get("detail"), "limit": "the plan's rules"})
                continue
        kept.append(change)
    built, accepted, refused, applied = pr.apply_changes(stored, kept, today, done)
    answer = recheck(built, lt_speed) if applied else built
    return answer, accepted, refused + dropped, applied


async def default_daily_level(client: Any, day: dt.date) -> Optional[str]:
    """Today's daily_status level, or None when it could not be built."""
    try:
        status = await ds.build_daily_status(client, day.isoformat())
    except Exception as exc:  # noqa: BLE001 - A8 then has no level ("today_level": null)
        log.info("plans: the review could not read today's daily status (%s)", type(exc).__name__)
        return None
    level = status.get("level")
    return str(level) if isinstance(level, str) else None


class ReviewService:
    """plan_review_week's work; the Garmin client, the store, the clock and the daily level are passed in."""

    # PLAN_WRITES=on (plan_writes.apply_section): the "apply" object of the answer.
    apply_hook: Optional[Callable[..., Dict[str, Any]]] = None

    def __init__(self, get_client: Callable[[], Any], store: Callable[[], Any], today: Callable[[], dt.date],
                 daily_level: Optional[Callable[[Any, dt.date], Any]] = None) -> None:
        self._client = get_client
        self._store = store
        self._today = today
        self._daily_level = daily_level or default_daily_level

    async def review(self, plan_id: Any = "", week_start: Any = "") -> str:
        today = self._today()
        try:
            pid = pt.parse_plan_id(plan_id)
            week = parse_week_start(week_start, today)
        except pt.BadInput as exc:
            return pt.bad_input_text(str(exc))
        client = self._client()
        results = await sc.fetch_all(review_calls(client, week, today), first="stats")
        reason = sc.systemic_failure(results, "stats")
        if reason:
            return f"Error: {reason}"
        failed = sorted(name for name, r in results.items() if not r.get("ok"))
        data = {name: sc.data_of(r) for name, r in results.items()}
        prof = sc.dig(data.get("stats"), "userProfileId")
        profile = str(prof).strip() if isinstance(prof, (int, str)) and not isinstance(prof, bool) \
            and str(prof).strip() else None
        if profile is None:
            return "No data: Garmin did not give the account id this time, so the plan cannot be found. Try again."
        try:
            loaded = await asyncio.to_thread(self._load, profile, pid, week, today, data,
                                             "activities" not in failed)
        except pt.BadInput as exc:
            return pt.bad_input_text(str(exc))
        except (ps.StoreError, ValueError) as exc:
            log.warning("plans: plan_review_week could not read the store (%s)", type(exc).__name__)
            return pt._store_error_text(self._store_or_none())
        if isinstance(loaded, str):
            return loaded
        plan, links, linked, feedback = loaded
        runs = runs_of(data.get("activities"))
        body = plan["version_row"]["body"]
        health = health_of(data, week, today)
        sessions = pr.sessions_of(body)
        if any(s.date == today and s.role in pe.QUALITY_ROLES for s in sessions):
            level = await self._daily_level(client, today)
            health = pr.Health(health.flags, health.hrv_7d, health.hrv_28d, health.short_nights, health.acwr, level)
        review = pr.review_week(plan["plan_id"], body, week, today, links, linked, runs, feedback, health)
        try:
            stored = await asyncio.to_thread(self._save, plan["plan_id"], week, review)
        except (ps.StoreError, ValueError, ps.PlanNotFound) as exc:
            log.warning("plans: plan_review_week could not store the proposals (%s)", type(exc).__name__)
            return pt._store_error_text(self._store_or_none())
        answer: Dict[str, Any] = {
            "kind": "plan_review", "rules_version": pr.REVIEW_RULES_VERSION, "plan_id": plan["plan_id"],
            "version": plan["current_version"], "week_start": week.isoformat(), "metrics": review["metrics"],
            "changes": [change_out(r) for r in stored], "rules": pr.REVIEW_RULES, "sources_failed": failed}
        note = NOTE
        if self.apply_hook is not None:
            proposed = [r for r in stored if r["status"] == "proposed"]
            answer["apply"] = await self._apply(client, plan, proposed, today, profile,
                                                pr.done_km(body, links, linked, runs))
            if answer["apply"].get("token"):
                note += NOTE_APPLY
        answer["note"] = note
        return sc.compact_json(answer)

    def _store_or_none(self) -> Any:
        try:
            return self._store()
        except Exception:  # noqa: BLE001
            return None

    def _load(self, profile: str, pid: Optional[str], week: dt.date, today: dt.date, data: Mapping[str, Any],
              activities_ok: bool = True) -> Any:
        """(plan, links, linked, feedback) after refreshing the links, or a fixed text. When the activity search
        failed nothing is settled from it (activities None); when it filled its one page the oldest days may be
        missing (cut)."""
        store = self._store()
        fp = store.account_fp(profile)
        plan = store.get_plan(fp, pid) if pid else store.tracked_plan(fp)
        if plan is None or not plan.get("version_row") or plan.get("status") not in ps.OPEN_STATUSES:
            return pt.TEXT_NOT_FOUND if pid else TEXT_NO_PLAN
        weeks = [w for w in plan["version_row"]["body"].get("weeks") or [] if isinstance(w, dict)]
        if not any(str(w.get("start")) <= week.isoformat() <= str(w.get("end")) for w in weeks):
            raise pt.BadInput("week_start is not a week of this plan")
        zone = plan_tracking._zone_or_utc()
        raw = data.get("activities")
        plan_tracking.refresh_plan(store, plan, ws._activity_items(raw) if activities_ok else None,
                                   ds.schedule_entries(data.get("schedule")),
                                   first=week - dt.timedelta(days=REFRESH_BACK_DAYS), last=today, tz=zone, today=today,
                                   cut=activities_ok and activities_cut(raw))
        rows = store.links(plan["plan_id"])
        links = {str(r["workout_key"]): r.get("link_status") for r in rows}
        linked = {str(r["workout_key"]): r.get("linked_activity_id") for r in rows}
        feedback = {str(r["workout_key"]): pr.Feedback(int(r["rpe"]), int(r["pain"]), bool(r["sharp"]))
                    for r in store.feedback(plan["plan_id"]) if r.get("rpe") is not None and r.get("pain") is not None}
        return plan, links, linked, feedback

    def _save(self, pid: str, week: dt.date, review: Mapping[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = self._store().save_changes(pid, week, change_rows(review))
        return rows

    async def _apply(self, client: Any, plan: Dict[str, Any], proposed: List[Dict[str, Any]], today: dt.date,
                     profile: str, done: Mapping[str, float]) -> Dict[str, Any]:
        """The apply object: a token for every proposed change that patches a session of the stored version
        (and the plan rules accept), or token null and why."""
        assert self.apply_hook is not None
        if not proposed:
            return {"token": None, "why": "no proposed change"}
        try:
            inputs = pr.inputs_from_echo(plan["version_row"]["inputs"])
        except (KeyError, ValueError, TypeError):
            return {"token": None, "why": "the stored plan's inputs are from an older release: call plan_preview"}
        try:
            snap, _pid, _failed = await pt.fetch_snapshot(client, today, inputs.race_date, inputs.course_id,
                                                          await asyncio.to_thread(pt.own_workout_ids, self._store))
        except sc.SystemicFailure:
            return {"token": None, "why": "Garmin did not answer the plan's numbers"}
        stored = {**plan["version_row"]["body"],
                  "inputs": plan["version_row"]["body"].get("inputs") or plan["version_row"]["inputs"]}
        answer, _accepted, refused, applied = await asyncio.to_thread(
            patched, stored, [change_for_apply(r) for r in proposed], today, done, snap.lt_speed_mps)
        if not applied:
            out0: Dict[str, Any] = {"token": None, "why": "no proposed change changes a session of the plan (advice "
                                                          "only, or the plan is already at or below them)"}
            if refused:
                out0["refused"] = refused
            return out0
        try:
            out: Dict[str, Any] = await asyncio.to_thread(self.apply_hook, self._store(), plan, answer, snap,
                                                          applied, proposed, today, profile)
        except Exception as exc:  # noqa: BLE001 - the review answers without a token
            log.warning("plans: no apply token (%s)", type(exc).__name__)
            out = {"token": None, "why": "the plan database did not answer"}
        if refused:
            out["refused"] = refused
        return out


def register(adder: Callable[..., Any], get_client: Callable[[], Any], store: Callable[[], Any],
             today: Callable[[], dt.date], *, writes: bool = False,
             daily_level: Optional[Callable[[Any, dt.date], Any]] = None) -> ReviewService:
    """Register plan_review_week through `adder` (plan_tools.register with PLAN_TRACKING=on); with `writes`
    (PLAN_WRITES=on) its answer carries the apply token (plan_writes.apply_section)."""
    import mcp2_compat

    svc = ReviewService(get_client, store, today, daily_level)
    description = DESCRIPTION
    if writes:
        import plan_writes

        svc.apply_hook = plan_writes.apply_section
        description += DESCRIPTION_APPLY

    @mcp2_compat.server_loop
    async def plan_review_week(plan_id: str = "", week_start: str = "") -> str:
        return await svc.review(plan_id=plan_id, week_start=week_start)

    adder(plan_review_week, name=TOOL_NAME, description=description)
    return svc

