"""Property-based tests (Hypothesis) for the daily answer's "missing" list:

    python3 -m unittest discover -s ext -p "test_properties_*.py" -v

missing_paths() is checked on generated answers, and daily_status_from() on
the synthetic fixtures with sources failed, emptied or with fields nulled.
Skipped ("needs hypothesis") where hypothesis is not installed. Runs are
derandomized and keep no example database, so CI sees the same examples
every time.
"""

import copy
import datetime as dt
import json
import os
import re
import unittest
from zoneinfo import ZoneInfo

import daily_status as ds

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st
    HAVE_HYPOTHESIS = True
except ImportError:  # the classes below are skipped; the shim only lets the module load
    HAVE_HYPOTHESIS = False

    class _Absent:
        """Stands in for hypothesis.strategies: every attribute, call and | is itself."""

        def __getattr__(self, name):
            return self

        def __call__(self, *args, **kwargs):
            return self

        def __or__(self, other):
            return self

    st = HealthCheck = _Absent()

    def given(*args, **kwargs):
        def keep(fn):
            return fn
        return keep

    settings = given

NEEDS_HYPOTHESIS = "needs hypothesis"
PROPS = dict(max_examples=200, deadline=None, derandomize=True, database=None,
             suppress_health_check=[HealthCheck.too_slow])
# Each example builds a whole daily answer: fewer of them.
BUILD_PROPS = dict(PROPS, max_examples=120)

TZ = ZoneInfo("Europe/Athens")
DAY = dt.date(2026, 3, 18)
NOW = dt.datetime(2026, 3, 18, 6, 30, tzinfo=TZ)
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "daily_status")
SOURCES = {  # payload name -> fixture file
    "stats": "stats", "sleep": "sleep_night", "sleep_prev": "sleep_prev_night", "readiness": "readiness",
    "hrv": "hrv_d0", "hrv_d1": "hrv_d1", "hrv_d2": "hrv_d2", "training_status": "training_status",
    "activities_prev": "activities_prev", "activities_today": "activities_today",
}
ATHLETE = {"lt_speed_mps": 3.7, "lt_hr": 172, "ftp_w": 240}
# Rule inputs with no field of their own in the answer (evaluate_rules' docstring).
VIRTUAL = re.compile(r"hrv_trend\.d-\d+")
STEP = re.compile(r"([^.\[\]]+)(?:\[(\d+)\])?")
_ABSENT = object()


def fixture(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def resolve(status, path):
    """The value at a "missing" path ("recovery.hrv", "today[0].structured"); _ABSENT when it is not there."""
    node = status
    for part in path.split("."):
        m = STEP.fullmatch(part)
        if m is None or not isinstance(node, dict) or m.group(1) not in node:
            return _ABSENT
        node = node[m.group(1)]
        if m.group(2) is not None:
            k = int(m.group(2))
            if not isinstance(node, list) or k >= len(node):
                return _ABSENT
            node = node[k]
    return node


def null_paths(value, path):
    """Every null field under value, walking dicts only (lists are values), in insertion order."""
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [p for key, sub in value.items() for p in null_paths(sub, f"{path}.{key}")]
    return []


def excluded(status, path, key_mode):
    if path in ds.NULL_BY_DESIGN or (key_mode and path in ds.KEY_MODE_NULL):
        return True
    companion = ds.COMPANION_FIELDS.get(path)
    return companion is not None and resolve(status, companion) in (None, _ABSENT)  # absent counts as null


def expected_from_sections(status, key_mode):
    """The spec of missing_paths(): every null field of the sections, in answer order, less the exclusions."""
    return [p for section in ds.MISSING_SECTIONS for p in null_paths(status.get(section), section)
            if not excluded(status, p, key_mode)]


# --- generated answers for missing_paths() -------------------------------------

# Field names the exclusions are about, plus ordinary ones (no dots: a path is split on them).
KNOWN = sorted({p.split(".", 1)[1] for p in ds.NULL_BY_DESIGN | ds.KEY_MODE_NULL | set(ds.COMPANION_FIELDS)}
               | {v.split(".", 1)[1] for v in ds.COMPANION_FIELDS.values()} | {"hrv", "hrv_range", "balanced_low"})
keys = st.sampled_from(KNOWN) | st.from_regex(r"[a-z][a-z_0-9]{0,7}", fullmatch=True)
leaves = st.none() | st.none() | st.integers(-5, 500) | st.floats(allow_nan=False, allow_infinity=False) \
    | st.text(max_size=4) | st.booleans() | st.lists(st.none() | st.integers(), max_size=3)
values = st.recursive(leaves, lambda inner: st.dictionaries(keys, inner, max_size=5), max_leaves=14)
# Every answer has all the sections (a section itself may be null), plus fields "missing" never walks.
sections = st.fixed_dictionaries({name: values for name in ds.MISSING_SECTIONS},
                                 optional={"flags": values, "thresholds": values, "date": values})


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class MissingPathsProperties(unittest.TestCase):

    @settings(**PROPS)
    @given(sections, st.booleans())
    def test_lists_exactly_the_null_fields_less_the_exclusions_in_answer_order(self, status, key_mode):
        out = ds.missing_paths(status, key_mode=key_mode)
        self.assertEqual(out, expected_from_sections(status, key_mode))

    @settings(**PROPS)
    @given(sections, st.booleans())
    def test_every_listed_path_is_null_and_listed_once(self, status, key_mode):
        out = ds.missing_paths(status, key_mode=key_mode)
        self.assertEqual(len(out), len(set(out)), out)
        for path in out:
            self.assertIsNone(resolve(status, path), path)
            self.assertIn(path.split(".", 1)[0], ds.MISSING_SECTIONS)

    @settings(**PROPS)
    @given(sections, st.booleans())
    def test_exclusions_are_never_listed(self, status, key_mode):
        out = set(ds.missing_paths(status, key_mode=key_mode))
        self.assertFalse(out & ds.NULL_BY_DESIGN)
        if key_mode:
            self.assertFalse(out & ds.KEY_MODE_NULL)
        for stamp, value in ds.COMPANION_FIELDS.items():
            if stamp in out:
                self.assertNotIn(resolve(status, value), (None, _ABSENT), stamp)

    @settings(**PROPS)
    @given(sections)
    def test_key_mode_only_drops_key_mode_fields(self, status):
        full = ds.missing_paths(status)
        keyed = ds.missing_paths(status, key_mode=True)
        self.assertEqual(keyed, [p for p in full if p not in ds.KEY_MODE_NULL])

    @settings(**PROPS)
    @given(sections, st.booleans())
    def test_answer_is_not_changed(self, status, key_mode):
        before = copy.deepcopy(status)
        ds.missing_paths(status, key_mode=key_mode)
        self.assertEqual(status, before)


# --- whole answers from the fixtures -------------------------------------------

BASE = {name: fixture(file) for name, file in SOURCES.items()}


def punchable(value, path=()):
    """Paths (tuples of keys / list indexes) of every dict field in a payload, nested ones included."""
    out = []
    if isinstance(value, dict):
        for key, sub in value.items():
            out.append(path + (key,))
            out += punchable(sub, path + (key,))
    elif isinstance(value, list):
        for k, sub in enumerate(value[:2]):
            out += punchable(sub, path + (k,))
    return out


PUNCHABLE = {name: punchable(payload) for name, payload in BASE.items()}


def punch(payload, paths):
    """A copy of payload with the fields at paths set to null (a path under an earlier null is skipped)."""
    payload = copy.deepcopy(payload)
    for path in paths:
        node = payload
        for step in path[:-1]:
            node = node[step] if isinstance(node, (dict, list)) else None
            if node is None:
                break
        if isinstance(node, dict) and path[-1] in node:
            node[path[-1]] = None
    return payload


@st.composite
def payloads(draw):
    """The fixtures, each source kept, failed (None), empty ({} / []) or with some fields nulled."""
    out = {}
    for name, payload in BASE.items():
        mode = draw(st.sampled_from(("keep", "keep", "punch", "punch", "none", "empty")))
        if mode == "none":
            out[name] = None
        elif mode == "empty":
            out[name] = draw(st.sampled_from(({}, [])))
        elif mode == "punch" and PUNCHABLE[name]:
            out[name] = punch(payload, draw(st.lists(st.sampled_from(PUNCHABLE[name]), min_size=1, max_size=6)))
        else:
            out[name] = copy.deepcopy(payload)
    return out


def planned(name, structured, hard):
    entry = {"scheduleDate": DAY.isoformat(), "workoutName": name, "workoutType": "running",
             "estimatedDurationInSecs": 3600, "estimatedDistanceInMeters": None, "associatedActivityId": None}
    curated = None
    if structured:
        step = {"type": "interval", "end_condition": "time", "end_condition_value": 600.0,
                "target_type": "heart.rate.zone", "target_zone": 5 if hard else 2}
        curated = {"name": "w", "sport": "running", "segments": [{"order": 1, "sport": "running", "steps": [step]}]}
    return ds.classify_workout(entry, curated)


workout_lists = st.none() | st.lists(st.builds(planned, st.sampled_from(("Easy", "Intervals")), st.booleans(),
                                               st.booleans()), max_size=2)
athletes = st.none() | st.fixed_dictionaries({k: st.none() | st.just(v) for k, v in ATHLETE.items()})
needed = st.lists(st.sampled_from([k for k, _ in ds.ATHLETE_FIELDS]), unique=True, max_size=3).map(tuple)


def build(data, today, tomorrow, athlete, athlete_needed, key_mode):
    return ds.daily_status_from(data, day=DAY, zone=TZ, now=NOW, today=today, tomorrow=tomorrow,
                                athlete=athlete, athlete_needed=athlete_needed, key_mode=key_mode)


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class DailyMissingProperties(unittest.TestCase):

    @settings(**BUILD_PROPS)
    @given(payloads(), workout_lists, workout_lists, athletes, needed, st.booleans())
    def test_missing_in_the_answer(self, data, today, tomorrow, athlete, athlete_needed, key_mode):
        status = build(data, today, tomorrow, athlete, athlete_needed, key_mode)
        missing = status["missing"]
        listed = set(missing)
        self.assertEqual(len(missing), len(listed), missing)  # no duplicates
        # Every listed path is null in the answer (the earlier-day HRV inputs have no field of their own).
        for path in missing:
            if VIRTUAL.fullmatch(path):
                continue
            self.assertIsNone(resolve(status, path), path)
        # Every null field not excluded is listed; the exclusions never are.
        for section in ds.MISSING_SECTIONS:
            for path in null_paths(status.get(section), section):
                if excluded(status, path, key_mode):
                    self.assertNotIn(path, listed)
                else:
                    self.assertIn(path, listed)
        self.assertFalse(listed & ds.NULL_BY_DESIGN)
        if key_mode:
            self.assertFalse(listed & ds.KEY_MODE_NULL)
        # Order: the null fields the text prints come first, in TEXT_FIELDS order (key mode too).
        text_null = [p for p in ds.TEXT_FIELDS if resolve(status, p) is None]
        self.assertEqual(missing[:len(text_null)], text_null)
        # Workouts without details, and the athlete thresholds a range target needed, are listed.
        for key in ("today", "tomorrow"):
            for i, w in enumerate(status[key] or []):
                self.assertEqual(f"{key}[{i}].structured" in listed, w.get("structured") is None)
        for key, field in ds.ATHLETE_FIELDS:
            path = f"thresholds_athlete.{field}"
            if key in athlete_needed and (athlete or {}).get(key) is None:
                self.assertIn(path, listed)
            elif path in listed:
                self.fail(f"{path} listed though not needed or known")

    @settings(**BUILD_PROPS)
    @given(payloads(), workout_lists, workout_lists)
    def test_key_mode_lists_a_subset(self, data, today, tomorrow):
        full = build(data, today, tomorrow, None, (), False)["missing"]
        keyed = build(data, today, tomorrow, None, (), True)["missing"]
        self.assertEqual(keyed, [p for p in full if p not in ds.KEY_MODE_NULL])


class MissingExamples(unittest.TestCase):
    """Fixed cases, run with or without hypothesis."""

    def test_companion_timestamp_follows_its_value(self):
        status = {"recovery": {"readiness": None, "readiness_at": None, "recovery_hours": 12,
                               "recovery_hours_at": None, "readiness_latest": None},
                  "load": {}, "today": [], "today_done": [], "tomorrow": []}
        self.assertEqual(ds.missing_paths(status), ["recovery.readiness", "recovery.recovery_hours_at"])

    def test_all_sources_failed(self):
        status = build({name: None for name in SOURCES}, None, None, None, (), False)
        missing = status["missing"]
        self.assertEqual(missing[:len(ds.TEXT_FIELDS)], list(ds.TEXT_FIELDS))
        self.assertEqual(len(missing), len(set(missing)))
        self.assertNotIn("recovery.readiness_latest", missing)
        self.assertNotIn("recovery.readiness_at", missing)


if __name__ == "__main__":
    unittest.main()
