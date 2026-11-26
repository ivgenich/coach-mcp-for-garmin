"""Property-based tests (Hypothesis) for plan_review:

    python3 -m unittest discover -s ext -p "test_properties_plan_review.py" -v

Skipped ("needs hypothesis") where hypothesis is not installed. Runs are derandomized and keep no example
database. The property: whatever mix of review changes is accepted, on whatever day, the patched version never
has more load than the stored version (no week with more km, no longer long run, no more quality sessions, no
new hard day), breaks no patch rule the stored version kept, and leaves every session up to today as it was
(today's only eased by an easy_day edit of today).
"""

import datetime as dt
import unittest

import plan_engine as pe
import plan_review as pr

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st
    HAVE_HYPOTHESIS = True
except ImportError:  # the class below is skipped; the shim only lets the module load
    HAVE_HYPOTHESIS = False

    class _Absent:
        def __getattr__(self, name):
            return self

        def __call__(self, *args, **kwargs):
            return self

    st = HealthCheck = _Absent()

    def given(*args, **kwargs):
        def keep(fn):
            return fn
        return keep

    settings = given

NEEDS_HYPOTHESIS = "needs hypothesis"
PROPS = dict(max_examples=150, deadline=None, derandomize=True, database=None,
             suppress_health_check=[HealthCheck.too_slow])
INPUTS = pe.PlanInputs(race_date=dt.date(2028, 2, 25), goal_time_s=3 * 3600 + 52 * 60,
                       b_races=(pe.BRace(dt.date(2028, 1, 28), "tri_half"),))
START = dt.date(2027, 11, 24)
_STORED = {}


def stored(day):
    """The stored version built on `day` (one of three), cached across examples."""
    if day not in _STORED:
        _STORED[day] = pe.plan_answer(pe.build_plan(INPUTS, pe.sample_snapshot(day), what_if_runs=False))
    return _STORED[day]


@st.composite
def case(draw):
    built = START + dt.timedelta(days=draw(st.sampled_from((0, 5, 11))))
    today = built + dt.timedelta(days=draw(st.integers(0, 30)))
    monday = pe.monday(today)
    weeks = [(monday + dt.timedelta(days=7 * k)).isoformat() for k in range(-1, 4)]
    edits = []
    for _ in range(draw(st.integers(1, 6))):
        op = draw(st.sampled_from(("cap_week", "cut_week", "cap_long", "easy_quality", "easy_day", "easy_band")))
        week = draw(st.sampled_from(weeks))
        if op == "cap_week":
            edits.append({"op": op, "week_start": week, "km": draw(st.floats(5, 120))})
        elif op == "cut_week":
            edits.append({"op": op, "week_start": week, "share": draw(st.floats(0.0, 0.4))})
        elif op == "cap_long":
            edits.append({"op": op, "week_start": week, "km": draw(st.floats(4, 35))})
        elif op == "easy_quality":
            edits.append({"op": op, "week_start": week})
        elif op == "easy_day":
            edits.append({"op": op, "date": (today + dt.timedelta(days=draw(st.integers(0, 10)))).isoformat()})
        else:
            edits.append({"op": op, "slowdown": draw(st.floats(0.0, 0.1))})
    changes = [{"change_id": "c%016x" % i, "kind": "repeat_volume", "edits": [e]} for i, e in enumerate(edits)]
    done = draw(st.one_of(st.none(), st.just({})))
    return built, today, changes, done


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class NeverRaises(unittest.TestCase):
    @settings(**PROPS)
    @given(case())
    def test_accepted_changes_never_raise_load(self, c):
        built, today, changes, done = c
        base = stored(built)
        plan, accepted, refused, applied = pr.apply_changes(base, changes, today, done)
        self.assertEqual(sorted(accepted + [r["change_id"] for r in refused]),
                         sorted(ch["change_id"] for ch in changes))
        self.assertLessEqual(set(applied), set(accepted))
        self.assertEqual(pr.no_raise(base, plan, today), [])
        self.assertLessEqual(set(pr.patch_problems(plan, today)), set(pr.patch_problems(base, today)))
        eased = {e["date"] for ch in changes for e in ch["edits"] if e["op"] == "easy_day"}
        now = {x["key"]: x for w in plan["weeks"] for x in w["sessions"]}
        for w in base["weeks"]:
            for x in w["sessions"]:
                if x["date"] < today.isoformat() or (x["date"] == today.isoformat() and x["date"] not in eased):
                    self.assertEqual(now.get(x["key"]), x)
        if not applied:
            self.assertEqual(plan["weeks"], base["weeks"])


if __name__ == "__main__":
    unittest.main()
