"""Deep test phase: the weekly review rules A1-A8 at their exact thresholds, and accepted changes applied to an
engine-built stored plan on a Monday, Wednesday, Saturday and Sunday (docs/training-plan.md, "Plan review
rules v1" and "Weekly review").

    cd ext && python3 -m unittest test_deep_engine_review -v

Standard library only.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import unittest
from typing import Any, Dict, List, Optional, Tuple

import plan_engine as pe
import plan_review as pr
import weekly_status as ws
from test_deep_engine_matrix import PROFILES, snapshot

D = dt.date
PID = "d" * 32
PREV, W, NEXT = D(2026, 11, 23), D(2026, 11, 30), D(2026, 12, 7)


def _s(key: str, day: dt.date, role: str, km: float, minutes: Optional[float]) -> Dict[str, Any]:
    return {"key": key, "date": day.isoformat(), "role": role, "km": km, "minutes": minutes,
            "hard_or_long": role in pe.HARD_ROLES}


def _k(start, days):
    return pe.session_key(start + dt.timedelta(days=days))


def _week(n: int, start: dt.date, scale: float = 1.0, quality: bool = True) -> Dict[str, Any]:
    sessions = [_s(_k(start, 1), start + dt.timedelta(days=1), "easy", round(10 * scale, 1), 60),
                _s(_k(start, 4), start + dt.timedelta(days=4), "easy", round(10 * scale, 1), 60),
                _s(_k(start, 6), start + dt.timedelta(days=6), "long", round(20 * scale, 1), 120)]
    if quality:
        sessions.insert(1, _s(_k(start, 2), start + dt.timedelta(days=2), "threshold", round(10 * scale, 1), 50))
    return {"week": n, "start": start.isoformat(), "end": (start + dt.timedelta(days=6)).isoformat(),
            "kind": "normal", "sessions": sessions}


def body(next_scale: float = 1.1, next_quality: bool = True, weeks: int = 3) -> Dict[str, Any]:
    out = [_week(1, PREV), _week(2, W), _week(3, NEXT, next_scale, next_quality)]
    return {"weeks": out[:weeks], "inputs": {}}


class ReviewBase(unittest.TestCase):
    """Both weeks done at their planned km, RPE 4, no pain, neutral health: no change."""

    def setUp(self) -> None:
        self.body = body()
        self.links: Dict[str, Optional[str]] = {}
        self.linked: Dict[str, Optional[int]] = {}
        self.runs: Dict[int, pr.Run] = {}
        self.feedback: Dict[str, pr.Feedback] = {}
        aid = 500
        for w in self.body["weeks"][:2]:
            for s in w["sessions"]:
                aid += 1
                self.links[s["key"]] = "done"
                self.linked[s["key"]] = aid
                self.runs[aid] = pr.Run(pr.Km(s["km"]), pe.MetersPerSecond(s["km"] * 1000 / (s["minutes"] * 60)))
                self.feedback[s["key"]] = pr.Feedback(4, 0, False)
        self.health = pr.Health()
        self.today = NEXT

    def review(self, health: Optional[pr.Health] = None, today: Optional[dt.date] = None,
               b: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return pr.review_week(PID, b or self.body, W, today or self.today, self.links, self.linked, self.runs,
                              self.feedback, health or self.health)

    def kinds(self, **kw: Any) -> List[Tuple[str, str]]:
        return sorted((c["rule"], c["kind"]) for c in self.review(**kw)["changes"])

    def run_km(self, key: str, km: Optional[float]) -> None:
        aid = self.linked[key]
        assert aid is not None
        self.runs[aid] = dataclasses.replace(self.runs[aid], km=pr.Km(km) if km is not None else None)

    def run_speed(self, key: str, factor: float) -> None:
        aid = self.linked[key]
        assert aid is not None
        s = next(x for w in self.body["weeks"] for x in w["sessions"] if x["key"] == key)
        self.runs[aid] = dataclasses.replace(self.runs[aid],
                                             speed_mps=pe.MetersPerSecond(s["km"] * 1000 / (s["minutes"] * 60) * factor))

    def rpe_easy(self, week: int, rpe: int) -> None:
        for s in self.body["weeks"][week - 1]["sessions"]:
            if s["role"] in ("easy", "strides"):
                self.feedback[s["key"]] = pr.Feedback(rpe, 0, False)


class Thresholds(ReviewBase):
    def test_the_rules_table(self):
        self.assertEqual(pr.REVIEW_RULES, {
            "completion_min_pct": 70, "hrv_drop_ms": 5, "short_nights": 3, "acwr_max": 1.3, "acwr_easy_rpe": 6,
            "deload_factor": 0.78, "easy_rpe": 6, "easy_band_slowdown": 0.03, "easy_rpe_week_cut": 0.10,
            "quality_rpe_max": 5, "quality_speed_over": 1.02, "pain": 4, "missed_week_cap": 1.10,
            "red_level": "red"})
        self.assertEqual(ws.THRESHOLDS["hrv_drop_ms"], 5)
        self.assertEqual(ws.THRESHOLDS["short_nights"], 3)

    def test_quiet_week(self):
        out = self.review()
        self.assertEqual(out["changes"], [])
        self.assertEqual(out["metrics"]["completion_pct"], 100.0)
        self.assertEqual(out["metrics"]["next_week"], {"week_start": NEXT.isoformat(), "planned_km": 55.0})

    def test_a1_at_70_percent(self):
        for long_km, expect in ((5.0, []), (4.95, [("A1", "repeat_volume")]), (4.9, [("A1", "repeat_volume")]),
                                (0.0, [("A1", "repeat_volume")])):
            with self.subTest(long_km=long_km):
                self.run_km("20261206-run", long_km)
                out = self.review()
                self.assertEqual(sorted((c["rule"], c["kind"]) for c in out["changes"]), expect,
                                 out["metrics"]["completion_pct"])
                if expect:
                    c = out["changes"][0]
                    self.assertEqual((c["from"]["km"], c["to"]["km"]), (55.0, 50.0))
                    self.assertEqual(c["evidence"]["threshold"], 70)
        # a run longer than planned counts up to its planned km; open sessions are not due
        self.run_km("20261206-run", 40.0)
        self.assertEqual(self.review()["metrics"]["done_km"], 50.0)
        self.links["20261206-run"] = None
        out = self.review()
        self.assertEqual((out["metrics"]["due_km"], out["metrics"]["sessions"]["open"]), (30.0, 1))

    def test_a1_partial_missed_replaced(self):
        self.links["20261206-run"] = "partial"
        self.run_km("20261206-run", None)          # partial without a distance counts nothing: 30 of 50
        self.assertEqual(self.kinds(), [("A1", "repeat_volume")])
        self.links["20261206-run"] = "done_late"   # done without a distance counts the planned km
        self.assertEqual(self.kinds(), [])
        self.links["20261206-run"] = "replaced"    # replaced: due, nothing done
        self.assertEqual(self.review()["metrics"]["completion_pct"], 60.0)
        self.assertEqual(self.kinds(), [("A1", "repeat_volume")])

    def test_a1_only_when_lower(self):
        b = body(next_scale=0.9)                # next week 45 km < this week's 50
        self.run_km("20261206-run", 0.0)
        self.assertEqual(self.kinds(b=b), [])

    def test_a2_from_weekly_status_flags(self):
        def flags(a7: float, a28: float, nights: int) -> Tuple[str, ...]:
            return tuple(f["id"] for f in ws.evaluate_flags({"hrv": {"avg_7d": a7, "avg_28d": a28},
                                                             "sleep": {"nights_under_6h": nights}}))
        self.assertEqual(flags(55.0, 60.0, 2), ("hrv_down",))
        self.assertEqual(flags(55.1, 60.0, 2), ())
        self.assertEqual(flags(60.0, 60.0, 3), ("short_nights",))
        for f, expect in ((flags(55.0, 60.0, 2), True), (flags(55.1, 60.0, 2), False),
                          (flags(60.0, 60.0, 3), True), (flags(60.0, 60.0, 2), False)):
            with self.subTest(flags=f):
                out = self.review(pr.Health(flags=f, hrv_7d=55.0, hrv_28d=60.0, short_nights=3))
                a2 = [c for c in out["changes"] if c["rule"] == "A2"]
                self.assertEqual(bool(a2), expect)
                if a2:
                    self.assertEqual(a2[0]["to"]["km"], round(50 * 0.78, 1))
                    self.assertEqual(a2[0]["edits"], [{"op": "cap_week", "week_start": NEXT.isoformat(), "km": 39.0}])

    def test_a2_acwr_needs_another_signal(self):
        cases = [(1.3, 6, False), (1.31, 6, True), (1.31, 5, False), (2.0, None, False), (None, 9, False)]
        for acwr, rpe, expect in cases:
            with self.subTest(acwr=acwr, rpe=rpe):
                self.setUp()
                if rpe is not None:
                    self.rpe_easy(2, rpe)
                out = self.review(pr.Health(acwr=acwr))
                a2 = [c for c in out["changes"] if c["rule"] == "A2"]
                self.assertEqual(bool(a2), expect, out["changes"])
                if a2:
                    self.assertEqual(a2[0]["evidence"]["also"][0]["metric"], "easy_rpe_mean")
        # mean easy RPE 5.5 (5 and 6) is under 6
        self.setUp()
        self.feedback["20261201-run"], self.feedback["20261204-run"] = pr.Feedback(5, 0, False), pr.Feedback(6, 0, False)
        self.assertFalse([c for c in self.review(pr.Health(acwr=1.5))["changes"] if c["rule"] == "A2"])
        # beside HRV, ACWR is cited in evidence.also
        out = self.review(pr.Health(flags=("hrv_down",), hrv_7d=50, hrv_28d=60, acwr=1.4))
        a2 = next(c for c in out["changes"] if c["rule"] == "A2")
        self.assertEqual(a2["evidence"]["also"], [{"metric": "acwr", "value": 1.4, "threshold": 1.3}])
        self.assertEqual(a2["evidence"]["value"], 10.0)

    def test_a3_missed_long_run(self):
        self.links["20261206-run"] = "missed"
        out = self.review()
        a3 = [c for c in out["changes"] if c["rule"] == "A3"]
        self.assertEqual(len(a3), 1)
        self.assertEqual((a3[0]["from"]["km"], a3[0]["to"]["km"]), (22.0, 20.0))
        self.links["20261206-run"] = "partial"
        self.assertFalse([c for c in self.review()["changes"] if c["rule"] == "A3"])
        self.links["20261206-run"] = "missed"
        self.assertFalse([c for c in self.review(b=body(next_scale=1.0))["changes"] if c["rule"] == "A3"])

    def test_a4_easy_rpe_6_in_both_weeks(self):
        for this, prev, expect in ((6, 6, True), (6, 5, False), (5, 6, False), (9, 7, True)):
            with self.subTest(this=this, prev=prev):
                self.setUp()
                self.rpe_easy(2, this)
                self.rpe_easy(1, prev)
                ks = self.kinds()
                self.assertEqual(("A4", "easy_band") in ks, expect)
                self.assertEqual(("A4", "cut_week") in ks, expect)
        self.setUp()
        self.rpe_easy(1, 6)
        self.rpe_easy(2, 6)
        out = self.review()
        cut = next(c for c in out["changes"] if c["kind"] == "cut_week")
        self.assertEqual(cut["to"]["km"], 49.5)
        for current, target in ((0.0, 0.03), (0.08, 0.1), (0.09, 0.1)):
            b = body()
            b["inputs"] = {"easy_band_slowdown": current}
            band = [c for c in self.review(b=b)["changes"] if c["kind"] == "easy_band"]
            self.assertEqual(band[0]["to"]["slowdown"], target)
        b = body()
        b["inputs"] = {"easy_band_slowdown": 0.1}
        self.assertEqual([c["kind"] for c in self.review(b=b)["changes"] if c["rule"] == "A4"], ["cut_week"])

    def test_a5_quality_speed_and_rpe(self):
        for factor, rpe, expect in ((1.02, 5, True), (1.019, 5, False), (1.05, 6, False), (1.05, 1, True)):
            with self.subTest(factor=factor, rpe=rpe):
                self.setUp()
                self.run_speed("20261202-run", factor)
                self.feedback["20261202-run"] = pr.Feedback(rpe, 0, False)
                a5 = [c for c in self.review()["changes"] if c["rule"] == "A5"]
                self.assertEqual(bool(a5), expect)
                if a5:
                    self.assertEqual((a5[0]["edits"], a5[0]["advice"]), ([], pr.ADVICE_RETEST))
        self.setUp()
        self.run_speed("20261202-run", 1.1)
        self.feedback["20261202-run"] = pr.Feedback(4, 0, False)
        self.links["20261202-run"] = "missed"
        self.assertFalse([c for c in self.review()["changes"] if c["rule"] == "A5"])

    def test_a6_pain_4_or_sharp_always_with_advice(self):
        scenarios = {"next_higher": body(), "next_lower": body(next_scale=0.9),
                     "next_lower_no_quality": body(next_scale=0.9, next_quality=False),
                     "no_next_week": body(weeks=2)}
        for name, b in scenarios.items():
            for pain, sharp, expect in ((3, False, False), (4, False, True), (0, True, True), (10, True, True)):
                with self.subTest(scenario=name, pain=pain, sharp=sharp):
                    self.setUp()
                    self.feedback["20261201-run"] = pr.Feedback(4, pain, sharp)
                    out = self.review(b=b)
                    a6 = [c for c in out["changes"] if c["rule"] == "A6"]
                    self.assertEqual(bool(a6), expect)
                    if expect:
                        self.assertTrue(all(c["advice"] == pr.ADVICE_PAIN for c in a6))
                        self.assertIn("physiotherapist or doctor", " ".join(c["reason"] for c in a6)
                                      + " ".join(c["advice"] for c in a6))
                        metric = a6[0]["evidence"]["metric"]
                        self.assertEqual(metric, "sharp" if pain < 4 else "pain")
        # an ended next week: advice only
        self.setUp()
        self.feedback["20261201-run"] = pr.Feedback(4, 5, False)
        out = self.review(today=NEXT + dt.timedelta(days=7))
        self.assertEqual([(c["rule"], c["kind"]) for c in out["changes"]], [("A6", "pain_advice")])

    def test_a7_after_a_missed_week(self):
        for s in self.body["weeks"][1]["sessions"]:
            self.links[s["key"]] = "missed"
        # 1.10 x 50 = 55.0 is not below next week's 55.0: no A7; a bigger next week gets it
        self.assertFalse([c for c in self.review()["changes"] if c["rule"] == "A7"])
        out = self.review(b=body(next_scale=1.2))
        a7 = [c for c in out["changes"] if c["rule"] == "A7"]
        self.assertEqual([(c["from"]["km"], c["to"]["km"]) for c in a7], [(60.0, 55.0)])
        # two missed weeks: back to the last week with a run
        for s in self.body["weeks"][0]["sessions"]:
            self.links[s["key"]] = "missed"
        out = self.review(b=body(next_scale=1.2))
        self.assertFalse([c for c in out["changes"] if c["rule"] == "A7"])   # nothing done before either

    def test_a8_red_day_with_quality_today(self):
        today = NEXT + dt.timedelta(days=2)   # next week's threshold day
        for level, expect in (("red", True), ("yellow", False), ("green", False), (None, False)):
            with self.subTest(level=level):
                out = self.review(pr.Health(today_level=level), today=today)
                a8 = [c for c in out["changes"] if c["rule"] == "A8"]
                self.assertEqual(bool(a8), expect)
                if a8:
                    self.assertEqual(a8[0]["edits"], [{"op": "easy_day", "date": today.isoformat()}])
        out = self.review(pr.Health(today_level="red"), today=today + dt.timedelta(days=1))
        self.assertFalse([c for c in out["changes"] if c["rule"] == "A8"])

    def test_change_ids_are_stable_and_distinct(self):
        self.rpe_easy(1, 7)
        self.rpe_easy(2, 7)
        self.links["20261206-run"] = "missed"
        self.feedback["20261204-run"] = pr.Feedback(7, 6, True)
        h = pr.Health(flags=("hrv_down",), hrv_7d=40, hrv_28d=50)
        a, b = self.review(h), self.review(h)
        self.assertEqual(a, b)
        ids = [c["change_id"] for c in a["changes"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.startswith("c") and len(i) == 17 for i in ids))


# --- accepted changes on an engine-built stored plan -----------------------------------------------------------------

def stored(inputs: pe.PlanInputs, snap: pe.Snapshot) -> Dict[str, Any]:
    return pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))


class Apply(unittest.TestCase):
    BUILT = D(2026, 11, 25)

    def setUp(self) -> None:
        snap = snapshot(self.BUILT, PROFILES["club40"], loads={})
        self.inputs = pe.PlanInputs(race_date=D(2027, 3, 21), goal_time_s=13500,
                                    b_races=(pe.BRace(D(2027, 1, 16), "tri_half"),))
        self.body = stored(self.inputs, snap)

    def review_all(self, body: Dict[str, Any], week: dt.date, today: dt.date) -> Dict[str, Any]:
        """A review of `week` with every rule triggered that can be (completion low, HRV down, the long run
        missed, easy RPE 7 in both weeks, pain 6 sharp, a red day)."""
        sessions = pr.sessions_of(body)
        links: Dict[str, Optional[str]] = {}
        fb: Dict[str, pr.Feedback] = {}
        for s in sessions:
            if s.date < week + dt.timedelta(days=7) and s.date >= week - dt.timedelta(days=7):
                links[s.key] = "missed" if s.role == "long" or s.date.weekday() == 2 else "done"
                if s.role in ("easy", "strides"):
                    fb[s.key] = pr.Feedback(7, 6 if s.date >= week else 0, s.date >= week)
        health = pr.Health(flags=("hrv_down", "short_nights"), hrv_7d=40, hrv_28d=50, short_nights=4, acwr=1.5,
                           today_level="red")
        return pr.review_week(PID, body, week, today, links, {}, {}, fb, health)

    def check_patch(self, base: Dict[str, Any], plan: Dict[str, Any], today: dt.date,
                    eased_today: bool = False) -> None:
        self.assertEqual(pr.no_raise(base, plan, today), [])
        self.assertLessEqual(set(pr.patch_problems(plan, today)), set(pr.patch_problems(base, today)))
        now = {s["key"]: s for w in plan["weeks"] for s in w["sessions"]}
        for w in base["weeks"]:
            for s in w["sessions"]:
                if s["date"] < today.isoformat() or (s["date"] == today.isoformat() and not eased_today):
                    self.assertEqual(now.get(s["key"]), s, s["key"])
        for w, w0 in zip(plan["weeks"], base["weeks"]):
            if w["sessions"] != w0["sessions"]:   # a patched week's totals are recounted
                self.assertEqual(w["planned_km"], round(sum(s["km"] for s in w["sessions"]
                                                            if s["role"] not in ("race", "b_race")), 1))
                self.assertLessEqual(w["planned_km"], w0["planned_km"] + 0.1)

    def test_every_change_applied_on_mon_wed_sat_sun(self):
        for week in (D(2026, 11, 30), D(2026, 12, 7), D(2027, 1, 4), D(2027, 2, 1)):
            nxt = week + dt.timedelta(days=7)
            for offset in (0, 1, 2, 5, 6):   # Monday to Wednesday (Tuesday: a red quality day), Saturday, Sunday
                today = nxt + dt.timedelta(days=offset)
                out = self.review_all(self.body, week, today)
                changes = out["changes"]
                for order in (1, -1):
                    with self.subTest(week=week, day=today.strftime("%a"), order=order):
                        plan, accepted, refused, applied = pr.apply_changes(self.body, changes[::order], today)
                        self.assertEqual(sorted(accepted + [r["change_id"] for r in refused]),
                                         sorted(c["change_id"] for c in changes))
                        self.assertLessEqual(set(applied), set(accepted))
                        self.check_patch(self.body, plan, today, eased_today=any(c["rule"] == "A8" for c in changes))
                        if applied:
                            self.assertEqual(plan["content_hash"],
                                             pe.content_hash(pe.RULES_VERSION, plan["inputs"], plan["weeks"]))
                            self.assertEqual(plan["review"]["applied"], applied)
                        else:
                            self.assertEqual(plan["weeks"], self.body["weeks"])

    def test_one_change_at_a_time_on_each_day(self):
        week = D(2026, 12, 7)
        nxt = week + dt.timedelta(days=7)
        for offset in range(7):
            today = nxt + dt.timedelta(days=offset)
            for c in self.review_all(self.body, week, today)["changes"]:
                with self.subTest(day=today.strftime("%a"), rule=c["rule"], kind=c["kind"]):
                    plan, accepted, refused, applied = pr.apply_changes(self.body, [c], today)
                    self.check_patch(self.body, plan, today, eased_today=c["rule"] == "A8")
                    if c["kind"] in ("early_deload", "repeat_volume", "hold_progression") and applied:
                        nw = next(w for w in plan["weeks"] if w["start"] == nxt.isoformat())
                        self.assertLessEqual(nw["planned_km"], c["to"]["km"] + 0.15)
                    if c["kind"] == "drop_quality" and applied:
                        nw = next(w for w in plan["weeks"] if w["start"] == nxt.isoformat())
                        self.assertFalse([s for s in nw["sessions"] if s["role"] in pe.QUALITY_ROLES
                                          and s["date"] > today.isoformat()])
                    if c["kind"] in ("pain_advice", "threshold_retest"):
                        self.assertEqual(applied, [])
                        self.assertEqual(accepted, [c["change_id"]])

    def test_a8_eases_only_todays_quality_session(self):
        quality = [s for w in self.body["weeks"] for s in w["sessions"] if s["role"] in pe.QUALITY_ROLES]
        for q in quality[:8]:
            today = D.fromisoformat(q["date"])
            out = self.review_all(self.body, pe.monday(today) - dt.timedelta(days=7), today)
            a8 = [c for c in out["changes"] if c["rule"] == "A8"]
            with self.subTest(day=q["date"]):
                self.assertEqual(len(a8), 1)
                plan, accepted, refused, applied = pr.apply_changes(self.body, a8, today)
                self.assertEqual(applied, [a8[0]["change_id"]])
                self.check_patch(self.body, plan, today, eased_today=True)
                now = {s["key"]: s for w in plan["weeks"] for s in w["sessions"]}
                self.assertEqual((now[q["key"]]["role"], now[q["key"]]["km"]), ("easy", q["km"]))
                changed = [k for k, s in now.items() if s != next(x for w in self.body["weeks"]
                                                                  for x in w["sessions"] if x["key"] == k)]
                self.assertEqual(changed, [q["key"]])

    def test_done_km_count_against_a_week_cap(self):
        # without the B race: rule 7 holds the long runs before it short, so the week cap's long-run cut would
        # lower the later long runs (long_step) and tip an 80%-easy week, which refuses the change; without
        # predictions too: the easy band from the marathon prediction tips that week the same way
        self.body = stored(dataclasses.replace(self.inputs, b_races=()),
                           snapshot(self.BUILT, PROFILES["club40"], loads={}, predictions=()))
        nxt = D(2026, 12, 14)
        today = nxt + dt.timedelta(days=3)   # Thursday
        nw = next(w for w in self.body["weeks"] if w["start"] == nxt.isoformat())
        past = [s for s in nw["sessions"] if s["date"] < today.isoformat() and s["role"] not in ("race", "b_race")]
        self.assertTrue(past)
        change = {"change_id": "c" + "0" * 16, "edits": [{"op": "cap_week", "week_start": nxt.isoformat(),
                                                          "km": round(nw["planned_km"] * 0.7, 1)}]}
        links = {s["key"]: "done" for s in past}
        done = pr.done_km(self.body, links)
        for got in (None, done, {k: 0.0 for k in done}):
            with self.subTest(done=got):
                plan, accepted, refused, applied = pr.apply_changes(self.body, [change], today, got)
                self.check_patch(self.body, plan, today)
                pw = next(w for w in plan["weeks"] if w["start"] == nxt.isoformat())
                ran = sum((got or {s["key"]: s["km"] for s in past}).get(s["key"], 0.0) for s in past)
                rest = sum(s["km"] for s in pw["sessions"] if s["date"] >= today.isoformat()
                           and s["role"] not in ("race", "b_race"))
                self.assertLessEqual(ran + rest, change["edits"][0]["km"] + 0.1)

    def test_a_plan_without_lt_speed_patches_without_bands(self):
        snap = snapshot(self.BUILT, PROFILES["no_lt"])
        body = stored(pe.PlanInputs(race_date=D(2027, 3, 21)), snap)
        week, today = D(2026, 12, 7), D(2026, 12, 16)
        changes = self.review_all(body, week, today)["changes"]
        self.assertTrue(changes)
        plan, accepted, refused, applied = pr.apply_changes(body, changes, today)
        self.check_patch(body, plan, today, eased_today=True)
        for w in plan["weeks"]:
            for s in w["sessions"]:
                for st in s["steps"]:
                    self.assertNotIn("speed_mps", st)

    def test_the_slowdown_round_trips_through_the_inputs(self):
        week, today = D(2026, 12, 7), D(2026, 12, 15)
        band = [c for c in self.review_all(self.body, week, today)["changes"] if c["kind"] == "easy_band"]
        plan, _a, _r, applied = pr.apply_changes(self.body, band, today)
        self.assertEqual(applied, [band[0]["change_id"]])
        self.assertEqual(plan["inputs"]["easy_band_slowdown"], 0.03)
        inputs = pr.inputs_from_echo(plan["inputs"])
        self.assertEqual(inputs.easy_band_slowdown, 0.03)
        self.assertEqual(inputs.echo(), plan["inputs"])
        again, _a, _r, applied2 = pr.apply_changes(plan, band, today)
        self.assertEqual(applied2, [])   # already at the slowdown: nothing to patch

    def test_two_accepted_cuts_of_one_week_keep_the_lower_whatever_their_order(self):
        snap = pe.sample_snapshot(self.BUILT)
        body = stored(pe.PlanInputs(race_date=D(2027, 3, 26), goal_time_s=3 * 3600 + 45 * 60), snap)
        week, nxt = D(2026, 12, 7), D(2026, 12, 14)
        sessions = pr.sessions_of(body)
        links = {s.key: "done" for s in sessions if s.date < nxt}
        fb = {s.key: pr.Feedback(7, 0, False) for s in sessions if s.date < nxt and s.role in ("easy", "strides")}
        out = pr.review_week(PID, body, week, nxt, links, {}, {}, fb,
                             pr.Health(flags=("hrv_down",), hrv_7d=50, hrv_28d=60))
        cuts = [c for c in out["changes"] if c["kind"] in ("early_deload", "cut_week")]
        self.assertEqual(len(cuts), 2)
        lowest = min(c["to"]["km"] for c in cuts)
        got = []
        for order in (cuts, cuts[::-1]):
            plan, _a, _r, _applied = pr.apply_changes(body, order, nxt)
            got.append(next(w for w in plan["weeks"] if w["start"] == nxt.isoformat())["planned_km"])
        self.assertEqual(got[0], got[1])
        self.assertAlmostEqual(got[0], lowest, delta=0.15)


if __name__ == "__main__":
    unittest.main()
