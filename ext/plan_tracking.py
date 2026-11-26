"""Tracking the plan's own sessions against finished activities (PLAN_TRACKING=on, Stage 3).

The plan tracked is the account's active plan (plan_store.tracked_plan); a draft is never tracked, so
without an active plan nothing is stored and the readers answer None. Tracking works on the plan's own
sessions (date, sport, role) without any Garmin write. A Garmin workout id on a
session's row (Stage 2) is only an optional link, through which Garmin's own associatedActivityId wins.

Rules (fixed; docs/training-plan.md "Tracking"):
- Matching is status_common.match_completion_pairs with one day early or late (MAX_EARLY_DAYS,
  MAX_LATE_DAYS) and not_before = the plan's activation (its creation for a row without one), so an activity from before
  the plan never completes one of its sessions. Each activity completes at most one session (an id listed
  twice counts once); the most sessions are completed and, of those ways, the most on their own day.
- On one day the bigger run is offered first (by distance, then time, not by start time), so a warm-up jog
  before a long run does not turn the long run partial; of two sessions on one day the long or quality one
  is offered the run first, and a run too short for it goes to the easy one when it is a full run for that
  (the two swap runs when each is a full run for the other's session).
- A matched run under PARTIAL_SHARE (80%) of the planned distance or of the planned time is `partial`;
  otherwise `done` (exactly 80% is done: _under_share compares exactly); a day early or late adds
  `_early` / `_late`.
- A session left without a run is settled only when the data covers its whole window (its early day is in
  the data, its late day too and is over): then it is `replaced` when other sports that day add up to
  REPLACED_SHARE (80%) of its planned time, else `missed`. get_daily_status's two days never settle one;
  get_weekly_status's 14 days do, except from a failed activity search (activities None: nothing) or from
  the days a full page may lack (cut: only from the day after its oldest activity).
- A stored done, partial or replaced link stays as it is when its activity is not in the data being matched
  (a later refresh with fewer days never undoes it) or started before not_before.

The links are written to plan_workouts (plan_store.save_links: a session without a Garmin row gets a
`local` row) by refresh_links(), which get_daily_status calls with yesterday's and today's activities,
get_weekly_status with its 14 days and plan_review_week (Stage 4) with a week's: best effort, it never raises
and logs only a fixed text with the exception's type. It returns the plan state it read and wrote (a
PlanState, or None without a plan), which today_plan(), ours_by_garmin_id() and week_counts() take as
`state=` so one report reads the plan once; without it they read the stored links back themselves. None
raises. Nothing here logs a value from Garmin, the store or the caller.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import plan_store as ps
import status_common as sc

LOG = logging.getLogger("garmin_ext.plans")

MAX_EARLY_DAYS = 1
MAX_LATE_DAYS = 1
PARTIAL_SHARE = 0.80
REPLACED_SHARE = 0.80
# Sessions tracked: every run role. A B race (a triathlon) is not a run the matcher could pair.
UNTRACKED_ROLES = frozenset({"b_race"})
SPORT = "run"
DONE = ("done", "done_early", "done_late")
PARTIAL = ("partial", "partial_early", "partial_late")
SETTLED_POSITIVE = DONE + PARTIAL + ("replaced",)
MOVED = ("done_early", "done_late", "partial_early", "partial_late")
# Offered a same-day run before the other sessions of that day (plan_engine's long, race and quality roles).
FIRST_ROLES = frozenset({"long", "race", "threshold", "intervals", "mp"})

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {}


# --- configuration (plan_tools.register) ----------------------------------------------------------------------

def configure(store_factory: Optional[Callable[[], Any]]) -> None:
    """Turn tracking on (a function giving the PlanStore) or off (None). plan_tools.register calls it when
    PLAN_TRACKING=on; the status reports then add the plan through configured_store()."""
    with _LOCK:
        if store_factory is None:
            _STATE.pop("factory", None)
        else:
            _STATE["factory"] = store_factory


def configured_store() -> Any:
    """The PlanStore when tracking is on, else None. Never raises."""
    with _LOCK:
        factory = _STATE.get("factory")
    if factory is None:
        return None
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 - tracking is best effort
        LOG.warning("plans: the plan store is not available for tracking (%s)", type(exc).__name__)
        return None


def account_fp(store: Any, profile_id: Any) -> Optional[str]:
    """The account fingerprint for a Garmin profile id, or None (no id, or the database did not answer)."""
    if store is None or profile_id is None or isinstance(profile_id, bool):
        return None
    try:
        return str(store.account_fp(profile_id))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("plans: no account for tracking (%s)", type(exc).__name__)
        return None


# --- the plan's sessions ----------------------------------------------------------------------------------------

def plan_sessions(body: Any) -> List[Dict[str, Any]]:
    """The tracked sessions of a stored plan body: key, date, role, km, minutes, dto_hash; by date."""
    out = []
    weeks = body.get("weeks") if isinstance(body, dict) else None
    for week in weeks if isinstance(weeks, list) else []:
        for s in week.get("sessions") or [] if isinstance(week, dict) else []:
            if not isinstance(s, dict) or s.get("role") in UNTRACKED_ROLES:
                continue
            try:
                day = dt.date.fromisoformat(str(s.get("date")))
                key = ps.check_workout_key(s.get("key"))
            except ValueError:
                continue
            role = s.get("role")
            if not isinstance(role, str):
                continue
            km = sc.num(s.get("km"))
            minutes = sc.num(s.get("minutes"))
            text = json.dumps(s, sort_keys=True, separators=(",", ":"), default=str)
            out.append({"key": key, "date": day, "role": role, "km": km, "minutes": minutes,
                        "dto_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return sorted(out, key=lambda x: (x["date"], x["key"]))


def _not_before(plan: Mapping[str, Any], tz: dt.tzinfo) -> Optional[dt.datetime]:
    """The plan's activation (its creation for a row without one) as a naive local time."""
    moment = plan.get("activated_at") or plan.get("created_at")
    if not isinstance(moment, dt.datetime):
        return None
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(tz).replace(tzinfo=None)


def _int_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 < value < 2 ** 63:
        return value
    if isinstance(value, str) and value.isdigit() and len(value) < 19:
        return int(value) or None
    return None


def normalize_activities(activities: Any, tz: dt.tzinfo) -> List[Dict[str, Any]]:
    """Garmin activity-list items -> {id, date, start, sport, distance_m, duration_s}, by start time. An
    activity id listed twice (a list page repeated) counts once: its first item."""
    out = []
    seen: Set[int] = set()
    for a in activities if isinstance(activities, list) else []:
        if not isinstance(a, dict):
            continue
        aid = _int_id(a.get("activityId"))
        day, start = sc.activity_times(a, tz)
        if aid is None or day is None or aid in seen:
            continue
        seen.add(aid)
        distance = sc.num(a.get("distance"))
        duration = sc.num(a.get("duration")) or sc.num(a.get("movingDuration"))
        out.append({"id": aid, "date": day, "start": start,
                    "sport": sc.completion_sport(sc.dig(a, "activityType", "typeKey")),
                    "distance_m": distance if distance is not None and distance > 0 else None,
                    "duration_s": duration if duration is not None and duration > 0 else None})
    return sorted(out, key=lambda x: (x["start"] or dt.datetime(x["date"].year, x["date"].month, x["date"].day),
                                      x["id"]))


def garmin_links(scheduled: Any) -> Dict[int, int]:
    """Garmin workout id -> associatedActivityId from calendar entries (ds.schedule_entries)."""
    out = {}
    for e in scheduled if isinstance(scheduled, list) else []:
        if not isinstance(e, dict):
            continue
        wid, aid = _int_id(e.get("workoutId")), _int_id(e.get("associatedActivityId"))
        if wid is not None and aid is not None:
            out[wid] = aid
    return out


def _under_share(done: float, planned: float, share: float) -> bool:
    """done < share * planned, read exactly: both sides at millisecond (millimetre) precision and the share
    in whole percent, so exactly 80% of the planned value is not under it (0.8 * 48 * 60 in floating point
    is 2304.0000000000005, above 38:24)."""
    return round(done * 1000) * 100 < round(share * 100) * round(planned * 1000)


def _partial(session: Mapping[str, Any], act: Mapping[str, Any]) -> bool:
    km, minutes = session.get("km"), session.get("minutes")
    if km and act.get("distance_m") is not None and _under_share(act["distance_m"], km * 1000, PARTIAL_SHARE):
        return True
    return bool(minutes and act.get("duration_s") is not None
                and _under_share(act["duration_s"], minutes * 60, PARTIAL_SHARE))


def _shifted(base: str, gap: int) -> str:
    return base + ("_early" if gap < 0 else "_late" if gap > 0 else "")


def _start_of(act: Mapping[str, Any]) -> dt.datetime:
    day = act["date"]
    return act.get("start") or dt.datetime(day.year, day.month, day.day)


def _size_order(act: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Activities by day, then the biggest run first (distance, then time), then start and id."""
    return (act["date"], -(act.get("distance_m") or 0.0), -(act.get("duration_s") or 0.0), _start_of(act), act["id"])


def _session_order(s: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Sessions by day, the long and quality ones first, then the bigger one, then the key."""
    return (s["date"], 0 if s.get("role") in FIRST_ROLES else 1, -(s.get("km") or 0.0),
            -(s.get("minutes") or 0.0), s["key"])


def _settles(session: Mapping[str, Any], first: dt.date, last: dt.date, today: Optional[dt.date]) -> bool:
    """The data first..last covers the session's whole window and its late day is over (first: the first
    day the data covers whole)."""
    late = session["date"] + dt.timedelta(days=MAX_LATE_DAYS)
    return (session["date"] - dt.timedelta(days=MAX_EARLY_DAYS) >= first and late <= last
            and (today is None or late < today))


def compute_links(sessions: Sequence[Mapping[str, Any]], rows: Iterable[Mapping[str, Any]],
                  activities: Sequence[Mapping[str, Any]], garmin: Mapping[int, int], *, first: dt.date,
                  last: dt.date, not_before: Optional[dt.datetime],
                  today: Optional[dt.date] = None, whole_from: Optional[dt.date] = None) -> List[Dict[str, Any]]:
    """The links to store (plan_store.check_link items), pure. activities are normalize_activities() items
    covering the days first..last; rows the stored links (plan_store.links); garmin from garmin_links();
    today the local date (a session is settled missed or replaced only once its late day is over; None:
    no such check); whole_from the first day the activities surely cover whole when that is after first
    (a list that may be cut; dt.date.max: none, a failed search), so no session is settled from days the
    data may lack."""
    settle_first = max(first, whole_from) if whole_from is not None else first
    stored = {r["workout_key"]: r for r in rows}
    by_id = {a["id"]: a for a in activities}
    acts = sorted((a for a in activities if first <= a["date"] <= last), key=_size_order)
    in_data = {a["id"] for a in acts}
    lo, hi = first - dt.timedelta(days=MAX_EARLY_DAYS), last + dt.timedelta(days=MAX_LATE_DAYS)

    def keeps(row: Optional[Mapping[str, Any]]) -> bool:
        """A stored positive link this data cannot redo: its activity is not in it, or predates not_before."""
        if not row or row.get("link_status") not in SETTLED_POSITIVE:
            return False
        aid = row.get("linked_activity_id")
        if aid not in in_data:
            return True
        return not_before is not None and _start_of(by_id[aid]) < not_before

    candidates = sorted((s for s in sessions if lo <= s["date"] <= hi and not keeps(stored.get(s["key"]))),
                        key=_session_order)
    keys = {s["key"] for s in candidates}
    taken = {r.get("linked_activity_id") for k, r in stored.items()
             if k not in keys and r.get("link_status") in SETTLED_POSITIVE}
    acts = [a for a in acts if a["id"] not in taken]
    sched = []
    for s in candidates:
        wid = (stored.get(s["key"]) or {}).get("garmin_workout_id")
        sched.append({"date": s["date"].isoformat(), "sport": SPORT,
                      "activity_id": garmin.get(wid) if wid is not None else None})
    pairs = sc.match_completion_pairs(
        sched, [{"date": a["date"].isoformat(), "start": a["start"], "sport": a["sport"], "id": a["id"]}
                for a in acts],
        max_early_days=MAX_EARLY_DAYS, max_late_days=MAX_LATE_DAYS, not_before=not_before)
    _hand_to_the_fitting_session(candidates, sched, pairs, acts)
    used = {acts[p[0]]["id"] for p in pairs if p is not None and p[0] is not None}
    out = []
    for s, pair, item in zip(candidates, pairs, sched):
        status: Optional[str] = None
        linked: Optional[int] = None
        if pair is not None and pair[0] is None:
            status, linked = "done", item["activity_id"]
        elif pair is not None and pair[0] is not None:
            act = acts[pair[0]]
            base = "partial" if _partial(s, act) else "done"
            # Garmin's own link names the session: no early or late label, whatever the dates say.
            status = base if item["activity_id"] is not None else _shifted(base, pair[1])
            linked = act["id"]
        elif _settles(s, settle_first, last, today):
            others = [a for a in acts if a["date"] == s["date"] and a["sport"] != SPORT and a["id"] not in used
                      and a["duration_s"] and (not_before is None or _start_of(a) >= not_before)]
            total = sum(a["duration_s"] for a in others)
            if s.get("minutes") and others and not _under_share(total, s["minutes"] * 60, REPLACED_SHARE):
                best = max(others, key=lambda a: (a["duration_s"], -a["id"]))
                status, linked = "replaced", best["id"]
                used.add(best["id"])
            else:
                status = "missed"
        row = stored.get(s["key"])
        if status is None and row is not None and row.get("link_status") not in DONE + PARTIAL:
            # Not settled by this data: a stored missed or replaced stays (only a matched run changes it).
            status, linked = row.get("link_status"), row.get("linked_activity_id")
        if status is None and row is None:
            continue
        out.append({"workout_key": s["key"], "date_local": s["date"], "role": s["role"], "dto_hash": s["dto_hash"],
                    "linked_activity_id": linked, "link_status": status})
    return sorted(out, key=lambda x: (x["date_local"], x["workout_key"]))


def _hand_to_the_fitting_session(candidates: Sequence[Mapping[str, Any]], sched: Sequence[Mapping[str, Any]],
                                 pairs: List[Optional[Tuple[Optional[int], int]]],
                                 acts: Sequence[Mapping[str, Any]]) -> None:
    """In place: a run matched (not Garmin-linked) to a session it is too short for moves to a session of
    the same day, at the same day gap, for which it is a full run: an unmatched one (two sessions, one run),
    or one whose own run is a full run for the first session (the two swap). Same day, so every gap and the
    matcher's count stay as they are."""
    for n, pair in enumerate(pairs):
        if pair is None or pair[0] is None or sched[n]["activity_id"] is not None:
            continue
        act = acts[pair[0]]
        if not _partial(candidates[n], act):
            continue
        for m, other in enumerate(candidates):
            if (m == n or sched[m]["activity_id"] is not None or other["date"] != candidates[n]["date"]
                    or _partial(other, act)):
                continue
            mine = pairs[m]
            if mine is None:
                pairs[m], pairs[n] = pair, None
                break
            if mine[0] is not None and not _partial(candidates[n], acts[mine[0]]):
                pairs[m], pairs[n] = pair, mine
                break


# --- store access (best effort) ---------------------------------------------------------------------------------

class PlanState:
    """One read of the tracked plan: the plan row, its sessions and its stored links by key."""

    __slots__ = ("plan", "sessions", "rows")

    def __init__(self, plan: Dict[str, Any], sessions: List[Dict[str, Any]], rows: Dict[str, Dict[str, Any]]):
        self.plan, self.sessions, self.rows = plan, sessions, rows


# Readers' default: read the plan state from the store (no state handed over).
_READ = object()


def _today(zone: dt.tzinfo) -> dt.date:
    return dt.datetime.now(dt.timezone.utc).astimezone(zone).date()


def whole_from(activities: Sequence[Mapping[str, Any]], cut: bool) -> Optional[dt.date]:
    """The first day a newest-first activity page surely holds whole: None when the page is complete, else
    the day after its oldest item (that day may have lost activities; no item: dt.date.max, none)."""
    if not cut:
        return None
    oldest = min((a["date"] for a in activities), default=None)
    return oldest + dt.timedelta(days=1) if oldest is not None and oldest < dt.date.max else dt.date.max


def refresh_plan(store: Any, plan: Mapping[str, Any], activities: Any, scheduled: Any, *, first: dt.date,
                 last: dt.date, tz: Optional[dt.tzinfo] = None, today: Optional[dt.date] = None,
                 rows: Optional[List[Dict[str, Any]]] = None, cut: bool = False) -> List[Dict[str, Any]]:
    """Match one stored plan (tracked_plan's row) against Garmin activities covering first..last and store
    the links; returns them. activities None: the activity search failed, so nothing is settled missed or
    replaced (Garmin's own links and the stored links still count). cut: the list is one full newest-first
    page that may lack the oldest days (nothing is settled from them). today: the local date (default: now
    in tz). rows: the plan's stored links when already read. Raises plan_store errors (refresh_links is the
    never-raising form)."""
    zone = tz or sc.zone()
    version_row = plan.get("version_row") or {}
    sessions = plan_sessions(version_row.get("body"))
    if not sessions:
        return []
    acts = normalize_activities(activities, zone)
    whole = dt.date.max if activities is None else whole_from(acts, cut)
    links = compute_links(sessions, store.links(plan["plan_id"]) if rows is None else rows,
                          acts, garmin_links(scheduled), first=first, last=last,
                          not_before=_not_before(plan, zone), today=today or _today(zone), whole_from=whole)
    if links:
        store.save_links(plan["plan_id"], int(version_row.get("version") or plan["current_version"]), links)
    return links


def refresh_links(store: Any, account_fp: Optional[str], day: dt.date, activities: Any, scheduled: Any, *,
                  first: Optional[dt.date] = None, last: Optional[dt.date] = None,
                  tz: Optional[dt.tzinfo] = None, today: Optional[dt.date] = None,
                  cut: bool = False) -> Optional[PlanState]:
    """Refresh the tracked plan's links from Garmin activities covering first..last (default: the day
    before `day` and `day`, as get_daily_status reads them; None: the search failed, and cut: a full page,
    as refresh_plan) and calendar entries (ds.schedule_entries, may be None). Returns the plan state with the links as refreshed (for the readers' state=), the state as
    read when the refresh failed after reading it, or None (no plan, or it could not be read). Best effort:
    never raises."""
    if store is None or not account_fp:
        return None
    state = None
    try:
        state = _plan_state(store, account_fp)
        if state is None:
            return None
        links = refresh_plan(store, state.plan, activities, scheduled, first=first or day - dt.timedelta(days=1),
                             last=last or day, tz=tz, today=today, rows=list(state.rows.values()), cut=cut)
        for link in links:
            row = dict(state.rows.get(link["workout_key"]) or {"state": ps.LOCAL_STATE, "garmin_workout_id": None})
            row.update(link)
            state.rows[link["workout_key"]] = row
    except Exception as exc:  # noqa: BLE001 - never fails the report
        LOG.warning("plans: the plan links were not refreshed (%s)", type(exc).__name__)
    return state


def _plan_state(store: Any, account_fp: Optional[str]) -> Optional[PlanState]:
    if store is None or not account_fp:
        return None
    plan = store.tracked_plan(account_fp)
    if plan is None or not plan.get("version_row"):
        return None
    sessions = plan_sessions(plan["version_row"].get("body"))
    rows = {r["workout_key"]: r for r in store.links(plan["plan_id"])}
    return PlanState(plan, sessions, rows)


def _state(store: Any, account_fp: Optional[str], state: Any) -> Optional[PlanState]:
    """The state handed over (None: no plan), else read now."""
    if state is _READ:
        return _plan_state(store, account_fp)
    return state if isinstance(state, PlanState) else None


def _feedback_keys(store: Any, plan_id: str) -> Set[str]:
    return {r["workout_key"] for r in store.feedback(plan_id)}


def today_plan(store: Any, account_fp: Optional[str], day: dt.date, *, state: Any = _READ
               ) -> Optional[Dict[str, Any]]:
    """Today's session of the tracked plan: {workout_key, date, role, sport, planned_km, planned_min,
    link_status, feedback_due, feedback_for}. A day of the plan without a run gives role "rest" and None
    session fields. feedback_for is the key of yesterday's or today's done or partial session that has no
    feedback yet (feedback_due: there is one). None without a plan, outside its weeks, or on a failure.
    state: refresh_links()'s answer (None: no plan), else the plan is read here."""
    try:
        got = _state(store, account_fp, state)
        if got is None:
            return None
        plan, sessions, rows = got.plan, got.sessions, got.rows
        body = plan["version_row"].get("body") or {}
        weeks = [w for w in body.get("weeks") or [] if isinstance(w, dict)]
        starts = [str(w.get("start")) for w in weeks]
        ends = [str(w.get("end")) for w in weeks]
        if not weeks or not min(starts) <= day.isoformat() <= max(ends):
            return None
        given = _feedback_keys(store, plan["plan_id"])
        due = [s["key"] for s in sessions if day - dt.timedelta(days=1) <= s["date"] <= day
               and (rows.get(s["key"]) or {}).get("link_status") in DONE + PARTIAL and s["key"] not in given]
        today = next((s for s in sessions if s["date"] == day), None)
        out: Dict[str, Any] = {"workout_key": None, "date": day.isoformat(), "role": "rest", "sport": None,
                               "planned_km": None, "planned_min": None, "link_status": None}
        if today is not None:
            out.update(workout_key=today["key"], role=today["role"], sport=SPORT, planned_km=today["km"],
                       planned_min=int(today["minutes"]) if today["minutes"] is not None else None,
                       link_status=(rows.get(today["key"]) or {}).get("link_status"))
        out["feedback_due"] = bool(due)
        out["feedback_for"] = due[-1] if due else None
        return out
    except Exception as exc:  # noqa: BLE001
        LOG.warning("plans: today's plan session could not be read (%s)", type(exc).__name__)
        return None


def ours_by_garmin_id(store: Any, account_fp: Optional[str], workout_ids: Iterable[Any], *,
                      state: Any = _READ) -> Dict[int, Dict[str, Any]]:
    """Garmin workout id -> {workout_key, role} for the tracked plan's current workouts among workout_ids
    (a retired `key~hash` row, a replaced day's old workout, is not the plan's any more)."""
    try:
        wanted = {i for i in (_int_id(w) for w in workout_ids or []) if i is not None}
        got = _state(store, account_fp, state) if wanted else None
        if got is None:
            return {}
        return {int(r["garmin_workout_id"]): {"workout_key": r["workout_key"], "role": r["role"]}
                for r in got.rows.values()
                if r.get("garmin_workout_id") in wanted and "~" not in str(r.get("workout_key"))}
    except Exception as exc:  # noqa: BLE001
        LOG.warning("plans: the plan's Garmin workouts could not be read (%s)", type(exc).__name__)
        return {}


def week_counts(store: Any, account_fp: Optional[str], week_start: dt.date, *, state: Any = _READ
                ) -> Optional[Dict[str, int]]:
    """The tracked plan's sessions in the 7 days from week_start by stored link: planned, done, partial,
    replaced, missed, open (not settled yet) and moved (done or partial a day early or late). None without
    a plan, without sessions that week, or on a failure. state: as for today_plan."""
    try:
        got = _state(store, account_fp, state)
        if got is None:
            return None
        sessions, rows = got.sessions, got.rows
        end = week_start + dt.timedelta(days=6)
        week = [s for s in sessions if week_start <= s["date"] <= end]
        if not week:
            return None
        statuses = [(rows.get(s["key"]) or {}).get("link_status") for s in week]
        return {"planned": len(week), "done": sum(st in DONE for st in statuses),
                "partial": sum(st in PARTIAL for st in statuses),
                "replaced": statuses.count("replaced"), "missed": statuses.count("missed"),
                "open": statuses.count(None), "moved": sum(st in MOVED for st in statuses)}
    except Exception as exc:  # noqa: BLE001
        LOG.warning("plans: the plan's week could not be read (%s)", type(exc).__name__)
        return None


def _zone_or_utc() -> dt.tzinfo:
    """REPORT_TZ (the server refuses to start without it); UTC where it is unset (a test)."""
    try:
        return sc.zone()
    except ValueError:
        return dt.timezone.utc


def _as_of(value: Any, tz: dt.tzinfo) -> Optional[str]:
    if not isinstance(value, dt.datetime):
        return None
    local = value.astimezone(tz) if value.tzinfo is not None else value
    return local.strftime("%Y-%m-%dT%H:%M")


def stored_links(store: Any, plan_id: str, tz: Optional[dt.tzinfo] = None) -> Dict[str, Any]:
    """plan_get's view: {links: [{key, date, status, as_of}], feedback: [{key, rpe, pain, pain_location,
    sharp}]}, each list only when it has rows. Raises plan_store errors."""
    out: Dict[str, Any] = {}
    rows = [r for r in store.links(plan_id) if r.get("link_checked_at") is not None]
    zone = (tz or _zone_or_utc()) if rows else dt.timezone.utc
    links = [{"key": r["workout_key"], "date": r["date_local"].isoformat() if isinstance(r["date_local"], dt.date)
              else str(r["date_local"]), "status": r["link_status"], "as_of": _as_of(r.get("link_checked_at"), zone)}
             for r in rows]
    if links:
        out["links"] = links
    feedback = [{"key": r["workout_key"], "rpe": r["rpe"], "pain": r["pain"], "pain_location": r["pain_location"],
                 "sharp": bool(r["sharp"])} for r in store.feedback(plan_id)]
    if feedback:
        out["feedback"] = feedback
    return out
