"""Training plans in Postgres (schema "history", migration 003) or in memory.

PlanStore is the interface; MemoryPlanStore backs the unit tests and the evals, PgPlanStore(dsn) is the real
one (psycopg 3, imported only when it is created). Like history_store: one short connection per call, or the
caller's `conn`; every psycopg error becomes StoreError with a fixed text, raised `from None`, so no row value
ever reaches an app log. Every value is checked in Python before any SQL runs (Postgres writes a failing
row's values to its own server log), and the statuses and enums are checked here, not by CHECK constraints.

Stage 1 stores previews: a plan (draft) and its versions (rules version, inputs, the engine's answer and its
content hash). A preview with the same content as the plan's current version stores nothing new. The tables
for Garmin writes, tokens, feedback and changes exist (migration 003). Stage 3 (PLAN_TRACKING=on, plan_tracking)
stores each tracked session's link in plan_workouts (a `local` row when Garmin has no workout for it; a later
Garmin write takes the row over through put_workout and keeps its link) and log_feedback's numbers in
plan_feedback. Stage 4 (plan_review_week) stores each week's proposed changes in plan_changes (save_changes:
proposed, approved by plan_preview(accept_changes) or plan_apply_changes, applied once on Garmin); the
machine form of a change (the plan edits it stands for) is kept in its to_value next to the value shown.

The account is history_store's fingerprint (HMAC of the Garmin profile id with the salt in history.meta):
plan_assess and plan_preview read the profile id from their own Garmin answers; plan_get reads only the plans
of the account the history index recorded in history.meta (history_account()), and none while that is unset
or flagged as a different account. Plan ids are 128 random bits. install_tag() is a per-install tag kept in history.meta, so two
installs that share one Garmin calendar never take each other's workouts for their own (Stage 2).

An active plan becomes `finished` once its A race's date (race_date) has passed: finish_past(today), run by
history_jobs.sweep and by move_plan(..., today=) before a plan_commit activates another plan, so a finished
plan never blocks the next one.

Retention (history_jobs.sweep): drafts untouched for 30 days, cancelled or finished plans 392 days after
they ended (with their versions, workouts, feedback and changes), tokens 7 days after they expired.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Protocol, Tuple

LOG = logging.getLogger("garmin_ext.plans")

SCHEMA = "history"
PLAN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
FP_RE = re.compile(r"^[0-9a-f]{64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
STATUSES = ("draft", "active", "cancelled", "finished")
OPEN_STATUSES = ("draft", "active")
WORKOUT_STATES = ("pending", "uploading", "uploaded", "scheduling", "scheduled", "conflict", "unscheduling",
                  "unscheduled", "deleting", "deleted", "unknown", "failed", "local")
# Our workouts that may still be on the Garmin calendar.
LIVE_STATES = ("uploading", "uploaded", "scheduling", "scheduled", "unscheduling", "unknown")
# The rows `launch.py --plan-cleanup` deletes (with a Garmin id, unlinked, from a day on): HISTORY_FORGET is
# refused while live_workouts(on_or_after=today) counts any, since the forget drops the rows that find them.
CLEANUP_STATES = ("uploaded", "scheduled", "unscheduling", "unscheduled", "deleting", "unknown")
META_SALT = "fp_salt"              # history_store.META_SALT: the same salt, so the fingerprints agree
META_ACCOUNT = "account_fp"        # history_store.META_ACCOUNT: the account the history index belongs to
META_MISMATCH = "account_mismatch"  # history_store.META_MISMATCH: "yes" while Garmin is another account
META_INSTALL_TAG = "plan_install_tag"
MAX_JSON_BYTES = 1_000_000
DRAFT_DAYS = 30
ENDED_DAYS = 392
TOKEN_DAYS = 7


class StoreError(RuntimeError):
    """A database failure, with a fixed text (never the SQL, the values or the URL)."""


class NotReady(StoreError):
    """The plan tables do not exist yet: the app's history startup thread has not migrated."""


class PlanNotFound(LookupError):
    """No such plan for this account (a fixed text)."""


class PlanStore(Protocol):
    def ready(self) -> bool: ...
    def account_fp(self, profile_id: Any) -> str: ...
    def install_tag(self) -> str: ...
    def save_preview(self, account_fp: str, plan_id: Optional[str], race_date: dt.date, rules_version: int,
                     inputs: Dict[str, Any], body: Dict[str, Any], content_hash: str) -> Tuple[str, int, bool]: ...
    def history_account(self) -> Optional[str]: ...
    def get_plan(self, account_fp: str, plan_id: Optional[str] = None) -> Optional[Dict[str, Any]]: ...
    def workouts(self, plan_id: str) -> List[Dict[str, Any]]: ...
    def live_workouts(self, on_or_after: Optional[dt.date] = None) -> int: ...
    def retention(self, now: Optional[dt.datetime] = None, *, conn: Any = None) -> Dict[str, int]: ...
    def finish_past(self, today: dt.date, account_fp: Optional[str] = None) -> int: ...
    def move_plan(self, plan_id: str, status: str, today: Optional[dt.date] = None) -> None: ...
    # Stage 3: tracking links and feedback.
    def tracked_plan(self, account_fp: str) -> Optional[Dict[str, Any]]: ...
    def save_links(self, plan_id: str, version: int, links: List[Dict[str, Any]],
                   checked_at: Optional[dt.datetime] = None) -> int: ...
    def save_feedback(self, plan_id: str, workout_key: str, rpe: int, pain: int, pain_location: str,
                      sharp: bool) -> bool: ...
    def feedback(self, plan_id: str) -> List[Dict[str, Any]]: ...
    def links(self, plan_id: str) -> List[Dict[str, Any]]: ...
    def save_changes(self, plan_id: str, week_start: dt.date, changes: List[Dict[str, Any]]
                     ) -> List[Dict[str, Any]]: ...
    def changes_of(self, plan_id: str, change_ids: Optional[List[str]] = None,
                   week_start: Optional[dt.date] = None) -> List[Dict[str, Any]]: ...
    def set_change_status(self, plan_id: str, change_ids: List[str], status: str) -> int: ...


# --- checks ---------------------------------------------------------------------------------------------------

def check_plan_id(value: Any) -> str:
    if not isinstance(value, str) or not PLAN_ID_RE.fullmatch(value):
        raise ValueError("plans: a plan id is 32 hex characters")
    return value


def _fp(value: Any) -> str:
    if not isinstance(value, str) or not FP_RE.fullmatch(value):
        raise ValueError("plans: account fingerprint must be 64 hex characters")
    return value


def _account_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and FP_RE.fullmatch(value) else None


def _json(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"plans: {name} must be an object")
    try:
        text = json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError(f"plans: {name} must be plain JSON") from None
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError(f"plans: {name} is too large")
    return dict(json.loads(text))


def _version_args(race_date: Any, rules_version: Any, content_hash: Any) -> Tuple[dt.date, int, str]:
    if not isinstance(race_date, dt.date) or isinstance(race_date, dt.datetime):
        raise ValueError("plans: race_date must be a date")
    if isinstance(rules_version, bool) or not isinstance(rules_version, int) or not 1 <= rules_version <= 999:
        raise ValueError("plans: rules_version must be a small whole number")
    if not isinstance(content_hash, str) or not HASH_RE.fullmatch(content_hash):
        raise ValueError("plans: content_hash must be 64 hex characters")
    return race_date, rules_version, content_hash


# --- Stage 3 checks: links and feedback -----------------------------------------------------------------------
# A session tracked in the app only (its link to a finished activity), never sent to Garmin: plan_tracking
# stores it so a plan works without Garmin writes. A later Garmin write of the same session takes the row over.
LOCAL_STATE = "local"
# done or partial (a day early or late: _early, _late), replaced (another sport that day), missed.
LINK_STATUSES = ("done", "done_early", "done_late", "partial", "partial_early", "partial_late", "replaced",
                 "missed")
# log_feedback's fixed pain locations: no free text is stored.
PAIN_LOCATIONS = ("none", "foot", "ankle", "achilles", "calf", "shin", "knee", "hamstring", "quad", "hip",
                  "glute", "lower_back", "other")


def _small_int(value: Any, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"plans: {name} must be a whole number from {low} to {high}")
    return value


def _activity_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 63:
        raise ValueError("plans: an activity id is a positive whole number")
    return value


def key_ok(value: Any, *, retired: bool = True) -> bool:
    """True for a session key (SESSION_KEY_RE, its eight digits a real date) or, with `retired`, a retired row's
    key (WORKOUT_KEY_RE: "<session key>~<hash8>" or "~<hash8>-<n>")."""
    if not isinstance(value, str) or not (WORKOUT_KEY_RE if retired else SESSION_KEY_RE).fullmatch(value):
        return False
    try:
        dt.datetime.strptime(value[:8], "%Y%m%d")
    except ValueError:
        return False
    return True


def check_workout_key(value: Any) -> str:
    if not key_ok(value):
        raise ValueError("plans: a workout key is a session key such as 20261206-run")
    return str(value)


def check_link(link: Any) -> Dict[str, Any]:
    """One save_links item, checked: workout_key, date_local, role, dto_hash, linked_activity_id, link_status."""
    if not isinstance(link, dict):
        raise ValueError("plans: a link must be an object")
    day = link.get("date_local")
    if not isinstance(day, dt.date) or isinstance(day, dt.datetime):
        raise ValueError("plans: a link's date_local must be a date")
    role = link.get("role")
    if not isinstance(role, str) or not ROLE_RE.fullmatch(role):
        raise ValueError("plans: a link's role must be a plan role")
    dto_hash = link.get("dto_hash")
    if not isinstance(dto_hash, str) or not HASH_RE.fullmatch(dto_hash):
        raise ValueError("plans: a link's dto_hash must be 64 hex characters")
    status = link.get("link_status")
    if status is not None and status not in LINK_STATUSES:
        raise ValueError("plans: unknown link status")
    return {"workout_key": check_workout_key(link.get("workout_key")), "date_local": day, "role": role,
            "dto_hash": dto_hash, "linked_activity_id": _activity_id(link.get("linked_activity_id")),
            "link_status": status}


def check_feedback(rpe: Any, pain: Any, pain_location: Any, sharp: Any) -> Tuple[int, int, str, bool]:
    """(rpe 1-10, pain 0-10, one of PAIN_LOCATIONS, sharp) or ValueError with a fixed text."""
    rpe = _small_int(rpe, 1, 10, "rpe")
    pain = _small_int(pain, 0, 10, "pain")
    if not isinstance(pain_location, str) or pain_location not in PAIN_LOCATIONS:
        raise ValueError("plans: unknown pain location")
    if not isinstance(sharp, bool):
        raise ValueError("plans: sharp must be true or false")
    return rpe, pain, pain_location, sharp


# --- Stage 4 checks: weekly changes ----------------------------------------------------------------------------
# plan_review's proposals: a change id (c + 16 hex, derived from the plan, the week and the rule), a kind from
# CHANGE_KINDS, from/to values and evidence as small JSON objects of numbers, dates and enums, and a reason the
# server built (printable ASCII, no caller text). Status only moves forward: proposed -> approved -> applied.
CHANGE_ID_RE = re.compile(r"c[0-9a-f]{16}", re.ASCII)
CHANGE_STATUSES = ("proposed", "approved", "applied")
CHANGE_KINDS = ("repeat_volume", "early_deload", "long_run_km", "easy_band", "cut_week", "threshold_retest",
                "hold_progression", "drop_quality", "cap_after_missed_week", "easy_today", "pain_advice")
MAX_CHANGE_JSON_BYTES = 8_000
MAX_CHANGES = 40
_REASON_RE = re.compile(r"[ -~]{1,400}", re.ASCII)


def check_change_id(value: Any) -> str:
    if not isinstance(value, str) or not CHANGE_ID_RE.fullmatch(value):
        raise ValueError("plans: a change id is c and 16 hex characters")
    return value


def _small_json(value: Any, name: str) -> Dict[str, Any]:
    out = _json(value, name)
    if len(json.dumps(out, sort_keys=True).encode("utf-8")) > MAX_CHANGE_JSON_BYTES:
        raise ValueError(f"plans: {name} is too large")
    return out


def check_change(change: Any) -> Dict[str, Any]:
    """One save_changes item, checked: change_id, kind, from_value, to_value, reason, evidence."""
    if not isinstance(change, dict):
        raise ValueError("plans: a change must be an object")
    if change.get("kind") not in CHANGE_KINDS:
        raise ValueError("plans: unknown change kind")
    reason = change.get("reason")
    if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
        raise ValueError("plans: a change reason is printable text of at most 400 characters")
    return {"change_id": check_change_id(change.get("change_id")), "kind": str(change["kind"]),
            "from_value": _small_json(change.get("from_value") or {}, "from_value"),
            "to_value": _small_json(change.get("to_value") or {}, "to_value"), "reason": reason,
            "evidence": _small_json(change.get("evidence") or {}, "evidence")}


def _change_status(status: Any) -> str:
    if status not in CHANGE_STATUSES:
        raise ValueError("plans: unknown change status")
    return str(status)


def _date_arg(value: Any, name: str) -> dt.date:
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        raise ValueError(f"plans: {name} must be a date")
    return value


def _change_ids(values: Any) -> List[str]:
    ids = [check_change_id(v) for v in values or []]
    if len(ids) > MAX_CHANGES:
        raise ValueError("plans: too many change ids")
    return sorted(set(ids))


def _checked_changes(changes: Any) -> List[Dict[str, Any]]:
    items = [check_change(c) for c in changes or []]
    if len(items) > MAX_CHANGES:
        raise ValueError("plans: too many changes")
    return items


def fingerprint(salt_hex: str, profile_id: Any) -> str:
    """history_store.fingerprint: HMAC-SHA256(salt, profile id), 64 hex."""
    return hmac.new(bytes.fromhex(salt_hex), str(profile_id).strip().encode("utf-8"), hashlib.sha256).hexdigest()


def _active_only(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """get_plan(account_fp)'s row when it is the active plan (get_plan lists the active plan first), else None."""
    return row if row is not None and row.get("status") == "active" else None


def new_plan_id() -> str:
    return secrets.token_hex(16)


def _profile(profile_id: Any) -> str:
    if isinstance(profile_id, bool) or not isinstance(profile_id, (int, str)) or not str(profile_id).strip():
        raise ValueError("plans: a Garmin profile id is required")
    return str(profile_id).strip()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --- Stage 2: tokens and workout rows -------------------------------------------------------------------------
# A token is "<ct1|at1|xt1>." + 24 random bytes in URL-safe base64 (32 characters); only its SHA-256 is stored,
# with the plan, the version, the action and the exact ordered ops it allows (and their hash). First use within
# TOKEN_FIRST_USE of issue; from the first use on, resumable for TOKEN_RESUME (expires_at moves then). A token
# that is done replays its stored result.

TOKEN_RE = re.compile(r"(ct|at|xt)1\.[A-Za-z0-9_-]{32}", re.ASCII)
TOKEN_PREFIXES = {"commit": "ct1", "apply": "at1", "cancel": "xt1"}
TOKEN_STATUSES = ("issued", "running", "partial", "done", "expired")
TOKEN_FIRST_USE = dt.timedelta(minutes=30)
TOKEN_RESUME = dt.timedelta(hours=24)
# A session key is the day the plan placed the session and its slot (plan_engine.session_key): it does not move
# when the plan is rebuilt on a later day. A retired row (replaced by a new workout) keeps "~<hash8>[-<n>]" after it.
SESSION_KEY_RE = re.compile(r"[0-9]{8}-(run|race|b)", re.ASCII)
WORKOUT_KEY_RE = re.compile(r"[0-9]{8}-(run|race|b)(~[0-9a-f]{8}(-[1-9][0-9]{0,2})?)?", re.ASCII)
ROLE_RE = re.compile(r"[a-z_]{1,16}", re.ASCII)
MAX_GARMIN_ID = 10 ** 19 - 1

# --- old session keys (before the date keys): migration 004 and MemoryPlanStore.upgrade_keys ------------------
# Plans stored before session keys were dates have keys numbered by week ("w03-long", "w00-e1", "w05-race",
# "w04-b"; a retired row "w03-long~<hash8>[-<n>]"). Migration 004 (history_sql/004_date_session_keys.sql) turns
# each into the date key of its session or row (plan_engine.session_key(date, slot): the session's date in a
# version body, date_local for a row); MemoryPlanStore.upgrade_keys() is the same conversion over the dicts,
# step by step, and test_plan_store runs one contract on both. Steps:
#   1. rows, per plan, the linked first, then those that may be on Garmin (LIVE_STATES), then the newest: the
#      date key (a retired row keeps its "~<hash8>[-<n>]"); a key the plan already holds (a duplicate made by the
#      old key scheme) becomes a retired key "<date key>~<md5(old key)[:8]>[-<n>]", which the next commit deletes
#      from Garmin like any replaced workout. The feedback of an old session key moves to its row's date key.
#   2. versions: every old session key in the body (and the same string in the version's inputs) becomes its
#      session's date key; for the plan's current version, the feedback not moved in step 1 and the plan's
#      changes follow the same mapping. Feedback moved onto a key that already has feedback is dropped.
#   3. feedback still under an old key (no row, no session) is deleted; open tokens whose ops name an old key
#      expire (a new preview issues a new one).
OLD_KEY_RE = re.compile(r"w[0-9]{2}-([a-z0-9]{1,8})(~[0-9a-f]{8}(-[1-9][0-9]{0,2})?)?", re.ASCII)
OLD_SESSION_KEY_RE = re.compile(r"w[0-9]{2}-([a-z0-9]{1,8})", re.ASCII)
OLD_TOKEN_KEY_RE = re.compile(r'"w[0-9]{2}-[a-z0-9]{1,8}[~"]', re.ASCII)
_ISO_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _old_slot(old: str) -> str:
    m = OLD_KEY_RE.fullmatch(old)
    tail = m.group(1) if m else ""
    return tail if tail in ("race", "b") else "run"


def _old_suffix(old: str) -> str:
    m = OLD_KEY_RE.fullmatch(old)
    return (m.group(2) or "") if m else ""


def _fallback_key(old: str, base: str, taken: Any) -> str:
    h = hashlib.md5(old.encode("ascii"), usedforsecurity=False).hexdigest()[:8]
    candidate, n = f"{base}~{h}", 1
    while candidate in taken:
        n += 1
        candidate = f"{base}~{h}-{n}"
    return candidate


def old_key_map(body: Any) -> Dict[str, str]:
    """{old session key: its date key} for the sessions of a version body that still carry an old key."""
    out: Dict[str, str] = {}
    weeks = body.get("weeks") if isinstance(body, dict) else None
    for w in weeks if isinstance(weeks, list) else []:
        sessions = w.get("sessions") if isinstance(w, dict) else None
        for s in sessions if isinstance(sessions, list) else []:
            if not isinstance(s, dict):
                continue
            key, day = s.get("key"), s.get("date")
            if isinstance(key, str) and OLD_SESSION_KEY_RE.fullmatch(key) and isinstance(day, str) \
                    and _ISO_DAY_RE.fullmatch(day):
                out[key] = day.replace("-", "") + "-" + _old_slot(key)
    return out


def replace_keys(value: Any, mapping: Mapping[str, str]) -> Any:
    """`value` (JSON) with every string equal to an old key of `mapping` (also an object's key) replaced, as
    migration 004 replaces '"<old>"' in the jsonb text."""
    if value is None or not mapping:
        return value
    text = json.dumps(value)
    for old, new in mapping.items():
        text = text.replace(f'"{old}"', f'"{new}"')
    return json.loads(text)


class TokenRefused(LookupError):
    """A token that cannot be used now; .reason is one of unknown, expired, done_elsewhere (fixed words)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"plans: token refused ({reason})")
        self.reason = reason


class PlanConflict(StoreError):
    """Another plan of this account is already active (a fixed text)."""


def new_token(action: str) -> str:
    return f"{TOKEN_PREFIXES[action]}." + base64.urlsafe_b64encode(secrets.token_bytes(24)).decode("ascii")


def check_token(value: Any, action: Optional[str] = None) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise ValueError("plans: a token is ct1./at1./xt1. and 32 characters")
    if action is not None and not value.startswith(TOKEN_PREFIXES[action] + "."):
        raise ValueError("plans: this token is for another action")
    return value


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def ops_digest(ops: Any) -> str:
    return hashlib.sha256(json.dumps(ops, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _token_args(plan_id: Any, version: Any, action: Any, ops: Any) -> Tuple[str, int, str, Dict[str, Any]]:
    pid = check_plan_id(plan_id)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("plans: a token needs the plan version")
    if action not in TOKEN_PREFIXES:
        raise ValueError("plans: unknown token action")
    return pid, version, action, _json(ops, "ops")


def _token_status(status: Any) -> str:
    if status not in TOKEN_STATUSES:
        raise ValueError("plans: unknown token status")
    return str(status)


def _plan_status(status: Any) -> str:
    if status not in STATUSES:
        raise ValueError("plans: unknown status")
    return str(status)


def _token_checked(row: Optional[Dict[str, Any]], plan_id: str, action: str, now: dt.datetime) -> Dict[str, Any]:
    """The token row when it may be used now (status done: to replay its result), else TokenRefused."""
    if row is None or row["plan_id"] != plan_id or row["action"] != action:
        raise TokenRefused("unknown")
    if row["status"] == "done":
        return row
    if row["status"] == "expired" or now > row["expires_at"]:
        raise TokenRefused("expired")
    return row


def _garmin_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_GARMIN_ID:
        raise ValueError("plans: a Garmin id is a positive whole number")
    return value


def _workout_args(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The columns of one plan_workouts row, each checked (never the value in the error)."""
    key = row.get("workout_key")
    if not key_ok(key):
        raise ValueError("plans: a workout key looks like 20261206-run")
    version = row.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("plans: a workout needs its plan version")
    day = row.get("date_local")
    if not isinstance(day, dt.date) or isinstance(day, dt.datetime):
        raise ValueError("plans: date_local must be a date")
    role = row.get("role")
    if not isinstance(role, str) or not ROLE_RE.fullmatch(role):
        raise ValueError("plans: a role is a short lower-case word")
    digest = row.get("dto_hash")
    if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
        raise ValueError("plans: dto_hash must be 64 hex characters")
    state = row.get("state")
    if state not in WORKOUT_STATES:
        raise ValueError("plans: unknown workout state")
    return {"workout_key": key, "version": version, "date_local": day, "role": role, "dto_hash": digest,
            "state": state, "garmin_workout_id": _garmin_id(row.get("garmin_workout_id")),
            "garmin_schedule_id": _garmin_id(row.get("garmin_schedule_id"))}


# --- in memory ------------------------------------------------------------------------------------------------

class MemoryPlanStore:
    """The interface over dicts (tests, the evals). `clock` gives the time (UTC)."""

    def __init__(self, clock: Callable[[], dt.datetime] = _utcnow) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self.meta: Dict[str, str] = {}
        self.plans: Dict[str, Dict[str, Any]] = {}
        self.versions: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self.workout_rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.tokens: Dict[str, Dict[str, Any]] = {}
        self.feedback_rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.changes: Dict[str, Dict[str, Any]] = {}
        self.is_ready = True

    def ready(self) -> bool:
        return self.is_ready

    def account_fp(self, profile_id: Any) -> str:
        pid = _profile(profile_id)
        with self._lock:
            salt = self.meta.setdefault(META_SALT, secrets.token_hex(32))
        return fingerprint(salt, pid)

    def install_tag(self) -> str:
        with self._lock:
            return self.meta.setdefault(META_INSTALL_TAG, secrets.token_hex(4))

    def save_preview(self, account_fp, plan_id, race_date, rules_version, inputs, body, content_hash):
        fp = _fp(account_fp)
        race_date, rules_version, content_hash = _version_args(race_date, rules_version, content_hash)
        inputs, body = _json(inputs, "inputs"), _json(body, "body")
        now = self._clock()
        with self._lock:
            if plan_id is None:
                pid = new_plan_id()
                self.plans[pid] = {"plan_id": pid, "account_fp": fp, "status": "draft", "current_version": 0,
                                   "race_date": race_date, "created_at": now, "updated_at": now,
                                   "activated_at": None, "ended_at": None}
            else:
                pid = check_plan_id(plan_id)
                row = self.plans.get(pid)
                if row is None or row["account_fp"] != fp or row["status"] not in OPEN_STATUSES:
                    raise PlanNotFound("plans: no open plan with this id for this Garmin account")
                cur = self.versions.get((pid, row["current_version"]))
                if cur is not None and cur["content_hash"] == content_hash:
                    row["updated_at"] = now   # a draft in use is not dropped by the 30-day retention
                    if "basis" in body and "basis" not in cur["body"]:
                        cur["body"]["basis"] = body["basis"]   # a plan stored before plan_basis gets it once
                    return pid, row["current_version"], False
            row = self.plans[pid]
            version = row["current_version"] + 1
            self.versions[(pid, version)] = {"plan_id": pid, "version": version, "rules_version": rules_version,
                                             "inputs": inputs, "body": body, "content_hash": content_hash,
                                             "created_at": now}
            row.update(current_version=version, race_date=race_date, updated_at=now)
            return pid, version, True

    def history_account(self):
        with self._lock:
            if self.meta.get(META_MISMATCH) == "yes":
                return None
            return _account_or_none(self.meta.get(META_ACCOUNT))

    def get_plan(self, account_fp, plan_id=None):
        fp = _fp(account_fp)
        with self._lock:
            if plan_id is not None:
                row = self.plans.get(check_plan_id(plan_id))
                if row is not None and row["account_fp"] != fp:
                    row = None
            else:
                rows = [r for r in self.plans.values() if r["status"] in OPEN_STATUSES and r["account_fp"] == fp]
                rows.sort(key=lambda r: (r["status"] == "active", r["updated_at"], r["plan_id"]), reverse=True)
                row = rows[0] if rows else None
            if row is None:
                return None
            out = copy.deepcopy(row)
            out["version_row"] = copy.deepcopy(self.versions.get((row["plan_id"], row["current_version"])))
            return out

    def workouts(self, plan_id):
        pid = check_plan_id(plan_id)
        with self._lock:
            rows = [copy.deepcopy(r) for (p, _k), r in self.workout_rows.items() if p == pid]
        return sorted(rows, key=lambda r: (r["date_local"], r["workout_key"]))

    def live_workouts(self, on_or_after=None):
        """Rows in LIVE_STATES; with `on_or_after`, the rows --plan-cleanup deletes: CLEANUP_STATES with a
        Garmin id, not linked to an activity, dated that day or later."""
        with self._lock:
            if on_or_after is None:
                return sum(1 for r in self.workout_rows.values() if r["state"] in LIVE_STATES)
            day = _date_arg(on_or_after, "on_or_after")
            return sum(1 for r in self.workout_rows.values()
                       if r["state"] in CLEANUP_STATES and r.get("garmin_workout_id")
                       and not r.get("linked_activity_id") and r["date_local"] >= day)

    def retention(self, now=None, *, conn=None):
        now = now or self._clock()
        with self._lock:
            gone = [pid for pid, r in self.plans.items()
                    if (r["status"] == "draft" and r["updated_at"] < now - dt.timedelta(days=DRAFT_DAYS))
                    or (r["status"] in ("cancelled", "finished")
                        and (r["ended_at"] or r["updated_at"]) < now - dt.timedelta(days=ENDED_DAYS))]
            for pid in gone:
                self._delete_plan(pid)
            tokens = [h for h, t in self.tokens.items() if t["expires_at"] < now - dt.timedelta(days=TOKEN_DAYS)]
            for h in tokens:
                del self.tokens[h]
        return {"plans": len(gone), "tokens": len(tokens)}

    def _delete_plan(self, pid: str) -> None:
        self.plans.pop(pid, None)
        for table in (self.versions, self.workout_rows, self.feedback_rows):
            for key in [k for k in table if k[0] == pid]:
                del table[key]
        for table2 in (self.tokens, self.changes):
            for key2 in [k for k, v in table2.items() if v.get("plan_id") == pid]:
                del table2[key2]

    def set_status(self, plan_id: str, status: str) -> None:
        """Tests and later stages: move a plan to another status (validated)."""
        if status not in STATUSES:
            raise ValueError("plans: unknown status")
        now = self._clock()
        with self._lock:
            row = self.plans[check_plan_id(plan_id)]
            row.update(status=status, updated_at=now)
            if status in ("cancelled", "finished"):
                row["ended_at"] = now

    # --- Stage 3: tracking links and feedback ---

    def tracked_plan(self, account_fp):
        """The plan tracking, the status reports, log_feedback and plan_review_week follow: the account's active
        plan, or None while it has none (a draft is never tracked; get_plan(account_fp) still gives it)."""
        return _active_only(self.get_plan(account_fp))

    def save_links(self, plan_id, version, links, checked_at=None):
        """Upsert each session's link (check_link items). A session without a row gets a LOCAL_STATE row; a
        row Garmin writes made keeps its date, role and state, and only its link columns change."""
        pid = check_plan_id(plan_id)
        version = _small_int(version, 1, 1_000_000, "version")
        items = [check_link(link) for link in links]
        now = checked_at or self._clock()
        with self._lock:
            if pid not in self.plans:
                raise PlanNotFound("plans: no plan with this id")
            for item in items:
                row = self.workout_rows.get((pid, item["workout_key"]))
                if row is None:
                    row = {"plan_id": pid, "workout_key": item["workout_key"], "version": version,
                           "date_local": item["date_local"], "role": item["role"], "dto_hash": item["dto_hash"],
                           "state": LOCAL_STATE, "garmin_workout_id": None, "garmin_schedule_id": None,
                           "state_changed_at": now}
                    self.workout_rows[(pid, item["workout_key"])] = row
                elif row["state"] == LOCAL_STATE:
                    row.update(version=version, date_local=item["date_local"], role=item["role"],
                               dto_hash=item["dto_hash"])
                row.update(linked_activity_id=item["linked_activity_id"], link_status=item["link_status"],
                           link_checked_at=now)
        return len(items)

    def save_feedback(self, plan_id, workout_key, rpe, pain, pain_location, sharp):
        """Upsert one session's feedback; True when it is new."""
        pid, key = check_plan_id(plan_id), check_workout_key(workout_key)
        rpe, pain, location, sharp = check_feedback(rpe, pain, pain_location, sharp)
        now = self._clock()
        with self._lock:
            if pid not in self.plans:
                raise PlanNotFound("plans: no plan with this id")
            old = self.feedback_rows.get((pid, key))
            self.feedback_rows[(pid, key)] = {
                "plan_id": pid, "workout_key": key, "rpe": rpe, "pain": pain, "pain_location": location,
                "sharp": sharp, "created_at": old["created_at"] if old else now, "updated_at": now}
        return old is None

    def links(self, plan_id):
        """The plan's workout rows with their link columns and Garmin workout id."""
        return self.workouts(plan_id)

    def feedback(self, plan_id):
        pid = check_plan_id(plan_id)
        with self._lock:
            rows = [copy.deepcopy(r) for (p, _k), r in self.feedback_rows.items() if p == pid]
        return sorted(rows, key=lambda r: r["workout_key"])

    # --- Stage 4: weekly changes ---

    def save_changes(self, plan_id, week_start, changes):
        """Store one week's proposals: a new change is proposed, a proposed one is updated, an approved or
        applied one keeps what it had; a proposed change of that week that is not proposed again is removed.
        Returns the week's changes as stored."""
        pid, week = check_plan_id(plan_id), _date_arg(week_start, "week_start")
        items = _checked_changes(changes)
        now = self._clock()
        with self._lock:
            if pid not in self.plans:
                raise PlanNotFound("plans: no plan with this id")
            ids = {i["change_id"] for i in items}
            for cid in [k for k, r in self.changes.items() if r["plan_id"] == pid and r["week_start"] == week
                        and r["status"] == "proposed" and k not in ids]:
                del self.changes[cid]
            for item in items:
                old = self.changes.get(item["change_id"])
                if old is not None and (old["plan_id"] != pid or old["status"] != "proposed"):
                    continue
                self.changes[item["change_id"]] = {**item, "plan_id": pid, "week_start": week, "status": "proposed",
                                                   "created_at": old["created_at"] if old else now, "updated_at": now}
        return self.changes_of(pid, week_start=week)

    def changes_of(self, plan_id, change_ids=None, week_start=None):
        """The plan's changes (only those ids, only that week when given), by week and id."""
        pid = check_plan_id(plan_id)
        wanted = set(_change_ids(change_ids)) if change_ids is not None else None
        week = _date_arg(week_start, "week_start") if week_start is not None else None
        with self._lock:
            rows = [copy.deepcopy(r) for r in self.changes.values() if r["plan_id"] == pid
                    and (wanted is None or r["change_id"] in wanted) and (week is None or r["week_start"] == week)]
        return sorted(rows, key=lambda r: (r["week_start"], r["change_id"]))

    def set_change_status(self, plan_id, change_ids, status):
        """Move changes forward (proposed -> approved -> applied; never back). Returns how many moved."""
        pid, ids, status = check_plan_id(plan_id), _change_ids(change_ids), _change_status(status)
        rank = CHANGE_STATUSES.index(status)
        now, moved = self._clock(), 0
        with self._lock:
            for cid in ids:
                row = self.changes.get(cid)
                if row is not None and row["plan_id"] == pid and CHANGE_STATUSES.index(row["status"]) < rank:
                    row.update(status=status, updated_at=now)
                    moved += 1
        return moved

    # --- Stage 2: tokens and the plan's workouts on Garmin ---------------------------------------------------

    def now(self) -> dt.datetime:
        return self._clock()

    def issue_token(self, plan_id: str, version: int, action: str, ops: Dict[str, Any]) -> Tuple[str, dt.datetime]:
        pid, version, action, ops = _token_args(plan_id, version, action, ops)
        token = new_token(action)
        now = self._clock()
        with self._lock:
            if pid not in self.plans:
                raise PlanNotFound("plans: no plan with this id")
            self.tokens[token_hash(token)] = {
                "token_hash": token_hash(token), "plan_id": pid, "version": version, "action": action, "ops": ops,
                "ops_hash": ops_digest(ops), "status": "issued", "result": None, "created_at": now,
                "expires_at": now + TOKEN_FIRST_USE, "first_used_at": None, "done_at": None}
        return token, now + TOKEN_FIRST_USE

    def token_row(self, token: str, plan_id: str, action: str) -> Dict[str, Any]:
        h, pid = token_hash(check_token(token, action)), check_plan_id(plan_id)
        with self._lock:
            row = copy.deepcopy(self.tokens.get(h))
        return _token_checked(row, pid, action, self._clock())

    def claim_token(self, token: str) -> None:
        h, now = token_hash(check_token(token)), self._clock()
        with self._lock:
            row = self.tokens[h]
            first = row["first_used_at"] or now
            row.update(status="running", first_used_at=first, expires_at=first + TOKEN_RESUME)

    def finish_token(self, token: str, status: str, result: Dict[str, Any]) -> None:
        h, status, result, now = token_hash(check_token(token)), _token_status(status), _json(result, "result"), \
            self._clock()
        with self._lock:
            self.tokens[h].update(status=status, result=result, done_at=now if status == "done" else None)

    def write_rows(self, plan_id: str) -> List[Dict[str, Any]]:
        pid = check_plan_id(plan_id)
        with self._lock:
            rows = [copy.deepcopy(r) for (p, _k), r in self.workout_rows.items() if p == pid]
        return sorted(rows, key=lambda r: (r["date_local"], r["workout_key"]))

    def all_write_rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [copy.deepcopy(r) for r in self.workout_rows.values()]
        return sorted(rows, key=lambda r: (r["plan_id"], r["date_local"], r["workout_key"]))

    def put_workout(self, plan_id: str, row: Mapping[str, Any]) -> None:
        pid, clean = check_plan_id(plan_id), _workout_args(row)
        now = self._clock()
        with self._lock:
            if pid not in self.plans:
                raise PlanNotFound("plans: no plan with this id")
            old = self.workout_rows.get((pid, clean["workout_key"])) or {
                "linked_activity_id": None, "link_status": None, "link_checked_at": None}
            self.workout_rows[(pid, clean["workout_key"])] = {**old, **clean, "plan_id": pid, "state_changed_at": now}

    def finish_past(self, today, account_fp=None):
        """Active plans (of `account_fp`, or all) whose race date is before `today` become finished; their
        number."""
        day = _date_arg(today, "today")
        fp = _fp(account_fp) if account_fp is not None else None
        now = self._clock()
        with self._lock:
            done = [r for r in self.plans.values() if r["status"] == "active" and r["race_date"] < day
                    and (fp is None or r["account_fp"] == fp)]
            for r in done:
                r.update(status="finished", updated_at=now, ended_at=now)
        return len(done)

    def upgrade_keys(self) -> int:
        """Migration 004 over the dicts (see OLD_KEY_RE): old session keys become date keys. Returns how many
        rows and versions changed; a second call changes nothing."""
        changed = 0
        with self._lock:
            for pid in sorted(self.plans):
                changed += self._upgrade_plan(pid)
            for t in self.tokens.values():
                if t["status"] in ("issued", "running", "partial") and OLD_TOKEN_KEY_RE.search(json.dumps(t["ops"])):
                    t["status"] = "expired"
        return changed

    def _move_feedback(self, pid: str, old: str, new: str) -> None:
        row = self.feedback_rows.pop((pid, old), None)
        if row is not None and (pid, new) not in self.feedback_rows:
            self.feedback_rows[(pid, new)] = {**row, "workout_key": new}

    def _upgrade_plan(self, pid: str) -> int:
        changed = 0
        rows = [r for (p, k), r in self.workout_rows.items() if p == pid and OLD_KEY_RE.fullmatch(k)]
        rows.sort(key=lambda r: (r.get("linked_activity_id") is None, r["state"] not in LIVE_STATES,
                                 -r["state_changed_at"].timestamp(), r["workout_key"]))
        taken = {k for (p, k) in self.workout_rows if p == pid}
        for r in rows:
            old = r["workout_key"]
            base = r["date_local"].strftime("%Y%m%d") + "-" + _old_slot(old)
            candidate = base + _old_suffix(old)
            taken.discard(old)
            if candidate in taken:
                candidate = _fallback_key(old, base, taken)
            taken.add(candidate)
            del self.workout_rows[(pid, old)]
            self.workout_rows[(pid, candidate)] = {**r, "workout_key": candidate}
            if "~" not in old:
                self._move_feedback(pid, old, base)
            changed += 1
        current = self.plans[pid]["current_version"]
        for (p, version), v in sorted(self.versions.items()):
            if p != pid:
                continue
            mapping = old_key_map(v["body"])
            if not mapping:
                continue
            if version == current:
                for old, new in mapping.items():
                    self._move_feedback(pid, old, new)
                for c in self.changes.values():
                    if c["plan_id"] == pid:
                        for k in ("from_value", "to_value", "evidence"):
                            c[k] = replace_keys(c.get(k), mapping)
            v["body"], v["inputs"] = replace_keys(v["body"], mapping), replace_keys(v["inputs"], mapping)
            changed += 1
        for key in [k for (p, k) in self.feedback_rows if p == pid and OLD_KEY_RE.fullmatch(k)]:
            del self.feedback_rows[(pid, key)]
        return changed

    def move_plan(self, plan_id: str, status: str, today: Optional[dt.date] = None) -> None:
        pid, status = check_plan_id(plan_id), _plan_status(status)
        now = self._clock()
        with self._lock:
            row = self.plans.get(pid)
            if row is None:
                raise PlanNotFound("plans: no plan with this id")
            if status == "active" and row["status"] != "active":
                if today is not None:
                    self.finish_past(today, row["account_fp"])
                if any(r["status"] == "active" and r["account_fp"] == row["account_fp"] for r in self.plans.values()):
                    raise PlanConflict("plans: another plan of this account is active")
                row["activated_at"] = now
            row.update(status=status, updated_at=now)
            if status in ("cancelled", "finished"):
                row["ended_at"] = now


# --- Postgres ---------------------------------------------------------------------------------------------------

def _psycopg() -> Tuple[Any, Any]:
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError:  # pragma: no cover - only with HISTORY_SEARCH=on and no history deps
        raise StoreError("plans: psycopg is not installed (build the image with HISTORY=1)") from None
    return psycopg, Jsonb


class PgPlanStore:
    """PlanStore on Postgres (psycopg 3); the tables come from history_store's migrations (003)."""

    def __init__(self, dsn: str, *, connect_timeout: int = 10) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("plans: DATABASE_URL is empty")
        self._dsn = dsn
        self._timeout = connect_timeout
        self._psycopg, self._Jsonb = _psycopg()

    def __repr__(self) -> str:  # never the URL
        return "PgPlanStore()"

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Any]:
        import jobqueue

        try:
            conn = self._psycopg.connect(self._dsn, connect_timeout=self._timeout,
                                         options=jobqueue.pg_options(jobqueue.STATEMENT_TIMEOUT_MS))
        except self._psycopg.Error:
            raise StoreError("plans: cannot connect to the database") from None
        try:
            with conn:
                yield conn
        except self._psycopg.Error:
            raise StoreError("plans: database error") from None
        finally:
            conn.close()

    @contextlib.contextmanager
    def _use(self, conn: Any) -> Iterator[Any]:
        if conn is not None:
            yield conn
            return
        with self.transaction() as own:
            yield own

    def _rows(self, conn: Any, sql: str, params: Optional[tuple] = None) -> List[tuple]:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []
        except self._psycopg.Error:
            raise StoreError("plans: database error") from None

    def ready(self) -> bool:
        with self.transaction() as c:
            return self._rows(c, "SELECT to_regclass(%s)", (f"{SCHEMA}.plans",))[0][0] is not None

    def _meta_setdefault(self, c: Any, key: str, value: str) -> str:
        sql = (f"WITH ins AS (INSERT INTO {SCHEMA}.meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING "
               f"RETURNING value) SELECT value FROM ins UNION ALL SELECT value FROM {SCHEMA}.meta WHERE key = %s "
               "LIMIT 1")
        rows = self._rows(c, sql, (key, value, key)) or self._rows(
            c, f"SELECT value FROM {SCHEMA}.meta WHERE key = %s", (key,))
        return str(rows[0][0])

    def account_fp(self, profile_id: Any) -> str:
        pid = _profile(profile_id)
        with self.transaction() as c:
            salt = self._meta_setdefault(c, META_SALT, secrets.token_hex(32))
        return fingerprint(salt, pid)

    def install_tag(self) -> str:
        with self.transaction() as c:
            return self._meta_setdefault(c, META_INSTALL_TAG, secrets.token_hex(4))

    def save_preview(self, account_fp, plan_id, race_date, rules_version, inputs, body, content_hash):
        fp = _fp(account_fp)
        race_date, rules_version, content_hash = _version_args(race_date, rules_version, content_hash)
        inputs, body = _json(inputs, "inputs"), _json(body, "body")
        with self.transaction() as c:
            if plan_id is None:
                pid = new_plan_id()
                self._rows(c, f"INSERT INTO {SCHEMA}.plans (plan_id, account_fp, status, current_version, race_date) "
                              "VALUES (%s, %s, 'draft', 0, %s)", (pid, fp, race_date))
                current = 0
            else:
                pid = check_plan_id(plan_id)
                rows = self._rows(c, f"SELECT current_version FROM {SCHEMA}.plans WHERE plan_id = %s AND "
                                     "account_fp = %s AND status = ANY(%s) FOR UPDATE", (pid, fp, list(OPEN_STATUSES)))
                if not rows:
                    raise PlanNotFound("plans: no open plan with this id for this Garmin account")
                current = int(rows[0][0])
                same = self._rows(c, f"SELECT 1 FROM {SCHEMA}.plan_versions WHERE plan_id = %s AND version = %s "
                                     "AND content_hash = %s", (pid, current, content_hash))
                if same:   # a draft in use is not dropped by the 30-day retention
                    self._rows(c, f"UPDATE {SCHEMA}.plans SET updated_at = now() WHERE plan_id = %s", (pid,))
                    if "basis" in body:   # a plan stored before plan_basis gets it once (same version, same hash)
                        self._rows(c, f"UPDATE {SCHEMA}.plan_versions SET body = jsonb_set(body, '{{basis}}', %s) "
                                      "WHERE plan_id = %s AND version = %s AND body -> 'basis' IS NULL",
                                   (self._Jsonb(body["basis"]), pid, current))
                    return pid, current, False
            version = current + 1
            self._rows(c, f"INSERT INTO {SCHEMA}.plan_versions (plan_id, version, rules_version, inputs, body, "
                          "content_hash) VALUES (%s, %s, %s, %s, %s, %s)",
                       (pid, version, rules_version, self._Jsonb(inputs), self._Jsonb(body), content_hash))
            self._rows(c, f"UPDATE {SCHEMA}.plans SET current_version = %s, race_date = %s, updated_at = now() "
                          "WHERE plan_id = %s", (version, race_date, pid))
        return pid, version, True

    _PLAN_COLS = "plan_id, account_fp, status, current_version, race_date, created_at, updated_at, activated_at, ended_at"

    def history_account(self):
        with self.transaction() as c:
            rows = self._rows(c, f"SELECT key, value FROM {SCHEMA}.meta WHERE key = ANY(%s)",
                              ([META_ACCOUNT, META_MISMATCH],))
        meta = {str(k): str(v) for k, v in rows}
        if meta.get(META_MISMATCH) == "yes":
            return None
        return _account_or_none(meta.get(META_ACCOUNT))

    def get_plan(self, account_fp, plan_id=None):
        fp = _fp(account_fp)
        with self.transaction() as c:
            if plan_id is not None:
                rows = self._rows(c, f"SELECT {self._PLAN_COLS} FROM {SCHEMA}.plans WHERE plan_id = %s "
                                     "AND account_fp = %s", (check_plan_id(plan_id), fp))
            else:
                rows = self._rows(c, f"SELECT {self._PLAN_COLS} FROM {SCHEMA}.plans WHERE status = ANY(%s) "
                                     "AND account_fp = %s "
                                     "ORDER BY status = 'active' DESC, updated_at DESC, plan_id DESC LIMIT 1",
                                  (list(OPEN_STATUSES), fp))
            if not rows:
                return None
            out = dict(zip(self._PLAN_COLS.split(", "), rows[0]))
            v = self._rows(c, f"SELECT version, rules_version, inputs, body, content_hash, created_at FROM "
                              f"{SCHEMA}.plan_versions WHERE plan_id = %s AND version = %s",
                           (out["plan_id"], out["current_version"]))
        out["version_row"] = None
        if v:
            r = v[0]
            out["version_row"] = {"plan_id": out["plan_id"], "version": r[0], "rules_version": r[1],
                                  "inputs": _loads(r[2]), "body": _loads(r[3]), "content_hash": r[4],
                                  "created_at": r[5]}
        return out

    def workouts(self, plan_id):
        sql = (f"SELECT workout_key, version, date_local, role, dto_hash, state, link_status, link_checked_at "
               f"FROM {SCHEMA}.plan_workouts WHERE plan_id = %s ORDER BY date_local, workout_key")
        with self.transaction() as c:
            rows = self._rows(c, sql, (check_plan_id(plan_id),))
        keys = ("workout_key", "version", "date_local", "role", "dto_hash", "state", "link_status", "link_checked_at")
        return [dict(zip(keys, r)) for r in rows]

    def live_workouts(self, on_or_after=None):
        with self.transaction() as c:
            if on_or_after is None:
                return int(self._rows(c, f"SELECT count(*) FROM {SCHEMA}.plan_workouts WHERE state = ANY(%s)",
                                      (list(LIVE_STATES),))[0][0])
            day = _date_arg(on_or_after, "on_or_after")
            return int(self._rows(
                c, f"SELECT count(*) FROM {SCHEMA}.plan_workouts WHERE state = ANY(%s) AND garmin_workout_id IS "
                   "NOT NULL AND linked_activity_id IS NULL AND date_local >= %s", (list(CLEANUP_STATES), day))[0][0])

    def retention(self, now=None, *, conn=None):
        now = now or _utcnow()
        with self._use(conn) as c:
            plans = self._rows(
                c, f"DELETE FROM {SCHEMA}.plans WHERE (status = 'draft' AND updated_at < %s) OR "
                   "(status IN ('cancelled', 'finished') AND coalesce(ended_at, updated_at) < %s) RETURNING 1",
                (now - dt.timedelta(days=DRAFT_DAYS), now - dt.timedelta(days=ENDED_DAYS)))
            tokens = self._rows(c, f"DELETE FROM {SCHEMA}.plan_tokens WHERE expires_at < %s RETURNING 1",
                                (now - dt.timedelta(days=TOKEN_DAYS),))
        if plans or tokens:
            LOG.info("plans: retention removed %d plan(s) and %d token(s)", len(plans), len(tokens))
        return {"plans": len(plans), "tokens": len(tokens)}

    # --- Stage 2: tokens and the plan's workouts on Garmin ---------------------------------------------------
    # Times come from Python (now()), like MemoryPlanStore's clock, so a token's windows read the same on both.

    def now(self) -> dt.datetime:
        return _utcnow()

    _TOKEN_COLS = ("token_hash, plan_id, version, action, ops, ops_hash, status, result, created_at, expires_at, "
                   "first_used_at, done_at")

    def issue_token(self, plan_id: str, version: int, action: str, ops: Dict[str, Any]) -> Tuple[str, dt.datetime]:
        pid, version, action, ops = _token_args(plan_id, version, action, ops)
        token, now = new_token(action), _utcnow()
        with self.transaction() as c:
            if not self._rows(c, f"SELECT 1 FROM {SCHEMA}.plans WHERE plan_id = %s", (pid,)):
                raise PlanNotFound("plans: no plan with this id")
            self._rows(c, f"INSERT INTO {SCHEMA}.plan_tokens (token_hash, plan_id, version, action, ops, ops_hash, "
                          "status, created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, 'issued', %s, %s)",
                       (token_hash(token), pid, version, action, self._Jsonb(ops), ops_digest(ops), now,
                        now + TOKEN_FIRST_USE))
        return token, now + TOKEN_FIRST_USE

    def token_row(self, token: str, plan_id: str, action: str) -> Dict[str, Any]:
        h, pid = token_hash(check_token(token, action)), check_plan_id(plan_id)
        with self.transaction() as c:
            rows = self._rows(c, f"SELECT {self._TOKEN_COLS} FROM {SCHEMA}.plan_tokens WHERE token_hash = %s", (h,))
        row = dict(zip(self._TOKEN_COLS.split(", "), rows[0])) if rows else None
        if row is not None:
            row["ops"], row["result"] = _loads(row["ops"]), _loads(row["result"])
        return _token_checked(row, pid, action, _utcnow())

    def claim_token(self, token: str) -> None:
        h, now = token_hash(check_token(token)), _utcnow()
        with self.transaction() as c:
            self._rows(c, f"UPDATE {SCHEMA}.plan_tokens SET status = 'running', first_used_at = "
                          "coalesce(first_used_at, %s), expires_at = coalesce(first_used_at, %s) + %s "
                          "WHERE token_hash = %s", (now, now, TOKEN_RESUME, h))

    def finish_token(self, token: str, status: str, result: Dict[str, Any]) -> None:
        h, status, result = token_hash(check_token(token)), _token_status(status), _json(result, "result")
        with self.transaction() as c:
            self._rows(c, f"UPDATE {SCHEMA}.plan_tokens SET status = %s, result = %s, done_at = %s "
                          "WHERE token_hash = %s",
                       (status, self._Jsonb(result), _utcnow() if status == "done" else None, h))

    _WRITE_COLS = ("plan_id, workout_key, version, date_local, role, dto_hash, state, garmin_workout_id, "
                   "garmin_schedule_id, state_changed_at, linked_activity_id, link_status, link_checked_at")

    def _write_rows(self, where: str, params: tuple) -> List[Dict[str, Any]]:
        with self.transaction() as c:
            rows = self._rows(c, f"SELECT {self._WRITE_COLS} FROM {SCHEMA}.plan_workouts {where} "
                                 "ORDER BY plan_id, date_local, workout_key", params)
        return [dict(zip(self._WRITE_COLS.split(", "), r)) for r in rows]

    def write_rows(self, plan_id: str) -> List[Dict[str, Any]]:
        return self._write_rows("WHERE plan_id = %s", (check_plan_id(plan_id),))

    def all_write_rows(self) -> List[Dict[str, Any]]:
        return self._write_rows("", ())

    def put_workout(self, plan_id: str, row: Mapping[str, Any]) -> None:
        pid, r = check_plan_id(plan_id), _workout_args(row)
        with self.transaction() as c:
            if not self._rows(c, f"SELECT 1 FROM {SCHEMA}.plans WHERE plan_id = %s", (pid,)):
                raise PlanNotFound("plans: no plan with this id")
            self._rows(
                c, f"INSERT INTO {SCHEMA}.plan_workouts (plan_id, workout_key, version, date_local, role, dto_hash, "
                   "state, garmin_workout_id, garmin_schedule_id, state_changed_at) VALUES (%s, %s, %s, %s, %s, %s, "
                   "%s, %s, %s, %s) ON CONFLICT (plan_id, workout_key) DO UPDATE SET version = EXCLUDED.version, "
                   "date_local = EXCLUDED.date_local, role = EXCLUDED.role, dto_hash = EXCLUDED.dto_hash, "
                   "state = EXCLUDED.state, garmin_workout_id = EXCLUDED.garmin_workout_id, "
                   "garmin_schedule_id = EXCLUDED.garmin_schedule_id, state_changed_at = EXCLUDED.state_changed_at",
                (pid, r["workout_key"], r["version"], r["date_local"], r["role"], r["dto_hash"], r["state"],
                 r["garmin_workout_id"], r["garmin_schedule_id"], _utcnow()))

    _FINISH_SQL = (f"UPDATE {SCHEMA}.plans SET status = 'finished', updated_at = now(), ended_at = now() "
                   "WHERE status = 'active' AND race_date < %s")

    def finish_past(self, today, account_fp=None):
        day = _date_arg(today, "today")
        fp = _fp(account_fp) if account_fp is not None else None
        with self.transaction() as c:
            if fp is None:
                rows = self._rows(c, self._FINISH_SQL + " RETURNING 1", (day,))
            else:
                rows = self._rows(c, self._FINISH_SQL + " AND account_fp = %s RETURNING 1", (day, fp))
        if rows:
            LOG.info("plans: %d plan(s) finished (their race date passed)", len(rows))
        return len(rows)

    def move_plan(self, plan_id: str, status: str, today: Optional[dt.date] = None) -> None:
        pid, status = check_plan_id(plan_id), _plan_status(status)
        day = _date_arg(today, "today") if today is not None else None
        with self.transaction() as c:
            rows = self._rows(c, f"SELECT account_fp, status FROM {SCHEMA}.plans WHERE plan_id = %s FOR UPDATE",
                              (pid,))
            if not rows:
                raise PlanNotFound("plans: no plan with this id")
            fp, current = rows[0]
            if status == "active" and current != "active":
                if day is not None:
                    self._rows(c, self._FINISH_SQL + " AND account_fp = %s", (day, fp))
                if self._rows(c, f"SELECT 1 FROM {SCHEMA}.plans WHERE account_fp = %s AND status = 'active'", (fp,)):
                    raise PlanConflict("plans: another plan of this account is active")
                self._rows(c, f"UPDATE {SCHEMA}.plans SET activated_at = now() WHERE plan_id = %s", (pid,))
            ended = "now()" if status in ("cancelled", "finished") else "ended_at"
            self._rows(c, f"UPDATE {SCHEMA}.plans SET status = %s, updated_at = now(), ended_at = {ended} "
                          "WHERE plan_id = %s", (status, pid))


    # --- Stage 3: tracking links and feedback ---

    def tracked_plan(self, account_fp):
        """The plan tracking, the status reports, log_feedback and plan_review_week follow: the account's active
        plan, or None while it has none (a draft is never tracked; get_plan(account_fp) still gives it)."""
        return _active_only(self.get_plan(account_fp))

    _LINK_COLS = ("workout_key, version, date_local, role, dto_hash, state, garmin_workout_id, linked_activity_id, "
                  "link_status, link_checked_at")

    def links(self, plan_id):
        with self.transaction() as c:
            rows = self._rows(c, f"SELECT {self._LINK_COLS} FROM {SCHEMA}.plan_workouts WHERE plan_id = %s "
                                 "ORDER BY date_local, workout_key", (check_plan_id(plan_id),))
        return [dict(zip(self._LINK_COLS.split(", "), r)) for r in rows]

    def save_links(self, plan_id, version, links, checked_at=None):
        pid = check_plan_id(plan_id)
        version = _small_int(version, 1, 1_000_000, "version")
        items = [check_link(link) for link in links]
        local = f"{SCHEMA}.plan_workouts.state = '{LOCAL_STATE}'"
        sql = (f"INSERT INTO {SCHEMA}.plan_workouts (plan_id, workout_key, version, date_local, role, dto_hash, state, "
               "linked_activity_id, link_status, link_checked_at) "
               f"VALUES (%s, %s, %s, %s, %s, %s, '{LOCAL_STATE}', %s, %s, coalesce(%s::timestamptz, now())) "
               "ON CONFLICT (plan_id, workout_key) DO UPDATE SET linked_activity_id = EXCLUDED.linked_activity_id, "
               "link_status = EXCLUDED.link_status, link_checked_at = EXCLUDED.link_checked_at, "
               f"version = CASE WHEN {local} THEN EXCLUDED.version ELSE {SCHEMA}.plan_workouts.version END, "
               f"date_local = CASE WHEN {local} THEN EXCLUDED.date_local ELSE {SCHEMA}.plan_workouts.date_local END, "
               f"role = CASE WHEN {local} THEN EXCLUDED.role ELSE {SCHEMA}.plan_workouts.role END, "
               f"dto_hash = CASE WHEN {local} THEN EXCLUDED.dto_hash ELSE {SCHEMA}.plan_workouts.dto_hash END")
        with self.transaction() as c:
            if not self._rows(c, f"SELECT 1 FROM {SCHEMA}.plans WHERE plan_id = %s", (pid,)):
                raise PlanNotFound("plans: no plan with this id")
            for i in items:
                self._rows(c, sql, (pid, i["workout_key"], version, i["date_local"], i["role"], i["dto_hash"],
                                    i["linked_activity_id"], i["link_status"], checked_at))
        return len(items)

    def save_feedback(self, plan_id, workout_key, rpe, pain, pain_location, sharp):
        pid, key = check_plan_id(plan_id), check_workout_key(workout_key)
        rpe, pain, location, sharp = check_feedback(rpe, pain, pain_location, sharp)
        sql = (f"INSERT INTO {SCHEMA}.plan_feedback (plan_id, workout_key, rpe, pain, pain_location, sharp) "
               "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (plan_id, workout_key) DO UPDATE SET rpe = EXCLUDED.rpe, "
               "pain = EXCLUDED.pain, pain_location = EXCLUDED.pain_location, sharp = EXCLUDED.sharp, "
               "updated_at = now() RETURNING (xmax = 0)")
        with self.transaction() as c:
            if not self._rows(c, f"SELECT 1 FROM {SCHEMA}.plans WHERE plan_id = %s", (pid,)):
                raise PlanNotFound("plans: no plan with this id")
            rows = self._rows(c, sql, (pid, key, rpe, pain, location, sharp))
        return bool(rows[0][0])

    _FEEDBACK_COLS = "plan_id, workout_key, rpe, pain, pain_location, sharp, created_at, updated_at"

    def feedback(self, plan_id):
        with self.transaction() as c:
            rows = self._rows(c, f"SELECT {self._FEEDBACK_COLS} FROM {SCHEMA}.plan_feedback WHERE plan_id = %s "
                                 "ORDER BY workout_key", (check_plan_id(plan_id),))
        return [dict(zip(self._FEEDBACK_COLS.split(", "), r)) for r in rows]

    # --- Stage 4: weekly changes ---

    _CHANGE_COLS = ("change_id, plan_id, week_start, kind, from_value, to_value, reason, evidence, status, "
                    "created_at, updated_at")

    def save_changes(self, plan_id, week_start, changes):
        pid, week = check_plan_id(plan_id), _date_arg(week_start, "week_start")
        items = _checked_changes(changes)
        upsert = (f"INSERT INTO {SCHEMA}.plan_changes (change_id, plan_id, week_start, kind, from_value, to_value, "
                  "reason, evidence, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'proposed') "
                  "ON CONFLICT (change_id) DO UPDATE SET kind = EXCLUDED.kind, from_value = EXCLUDED.from_value, "
                  "to_value = EXCLUDED.to_value, reason = EXCLUDED.reason, evidence = EXCLUDED.evidence, "
                  f"updated_at = now() WHERE {SCHEMA}.plan_changes.status = 'proposed' "
                  f"AND {SCHEMA}.plan_changes.plan_id = EXCLUDED.plan_id")
        with self.transaction() as c:
            if not self._rows(c, f"SELECT 1 FROM {SCHEMA}.plans WHERE plan_id = %s FOR UPDATE", (pid,)):
                raise PlanNotFound("plans: no plan with this id")
            self._rows(c, f"DELETE FROM {SCHEMA}.plan_changes WHERE plan_id = %s AND week_start = %s "
                          "AND status = 'proposed' AND NOT (change_id = ANY(%s))",
                       (pid, week, [i["change_id"] for i in items]))
            for i in items:
                self._rows(c, upsert, (i["change_id"], pid, week, i["kind"], self._Jsonb(i["from_value"]),
                                       self._Jsonb(i["to_value"]), i["reason"], self._Jsonb(i["evidence"])))
        return self.changes_of(pid, week_start=week)

    def changes_of(self, plan_id, change_ids=None, week_start=None):
        pid = check_plan_id(plan_id)
        where, params = "plan_id = %s", [pid]
        if change_ids is not None:
            where += " AND change_id = ANY(%s)"
            params.append(_change_ids(change_ids))
        if week_start is not None:
            where += " AND week_start = %s"
            params.append(_date_arg(week_start, "week_start"))
        with self.transaction() as c:
            rows = self._rows(c, f"SELECT {self._CHANGE_COLS} FROM {SCHEMA}.plan_changes WHERE {where} "
                                 "ORDER BY week_start, change_id", tuple(params))
        out = []
        for r in rows:
            row = dict(zip(self._CHANGE_COLS.split(", "), r))
            for k in ("from_value", "to_value", "evidence"):
                row[k] = _loads(row[k]) or {}
            out.append(row)
        return out

    def set_change_status(self, plan_id, change_ids, status):
        pid, ids, status = check_plan_id(plan_id), _change_ids(change_ids), _change_status(status)
        lower = list(CHANGE_STATUSES[:CHANGE_STATUSES.index(status)])
        if not ids or not lower:
            return 0
        with self.transaction() as c:
            rows = self._rows(c, f"UPDATE {SCHEMA}.plan_changes SET status = %s, updated_at = now() "
                                 "WHERE plan_id = %s AND change_id = ANY(%s) AND status = ANY(%s) RETURNING 1",
                              (status, pid, ids, lower))
        return len(rows)



def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def for_history_store(store: Any) -> Optional[PlanStore]:
    """The plan store beside a history store: its `plan_store` attribute when it has one (the tests' memory
    stores), a PgPlanStore on the same database for a PgHistoryStore, else None."""
    attached = getattr(store, "plan_store", None)
    if attached is not None:
        return attached  # type: ignore[no-any-return]
    dsn = getattr(store, "_dsn", None)
    if isinstance(dsn, str) and dsn:
        return PgPlanStore(dsn)
    return None
