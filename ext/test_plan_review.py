"""plan_review: the weekly review rules A1-A8 at their boundaries, and accepted changes as a new plan version.
Standard library only."""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import unittest

import plan_engine as pe
import plan_review as pr

PID = "a" * 32
W = dt.date(2026, 11, 30)            # the reviewed week
PREV = W - dt.timedelta(days=7)
NEXT = W + dt.timedelta(days=7)
TODAY = NEXT                         # the Monday after the reviewed week


def _s(key, day, role, km, minutes):
    return {"key": key, "date": day.isoformat(), "role": role, "km": km, "minutes": minutes,
            "hard_or_long": role not in ("easy", "strides")}


def _k(start, days):
    return pe.session_key(start + dt.timedelta(days=days))


def _week(n, start, scale=1.0):
    return {"week": n, "start": start.isoformat(), "end": (start + dt.timedelta(days=6)).isoformat(),
            "kind": "normal", "sessions": [
                _s(_k(start, 1), start + dt.timedelta(days=1), "easy", round(10 * scale, 1), 60),
                _s(_k(start, 2), start + dt.timedelta(days=2), "threshold", round(10 * scale, 1), 50),
                _s(_k(start, 4), start + dt.timedelta(days=4), "easy", round(10 * scale, 1), 60),
                _s(_k(start, 6), start + dt.timedelta(days=6), "long", round(20 * scale, 1), 120)]}


def body():
    return {"weeks": [_week(1, PREV), _week(2, W), _week(3, NEXT, 1.1)], "inputs": {}}


class Review(unittest.TestCase):
    """Every session of both weeks done at its planned km, RPE 4, no pain, neutral health: no change."""

    def setUp(self):
        self.links, self.linked, self.runs, self.feedback = {}, {}, {}, {}
        aid = 100
        for w in body()["weeks"][:2]:
            for s in w["sessions"]:
                aid += 1
                self.links[s["key"]] = "done"
                self.linked[s["key"]] = aid
                self.runs[aid] = pr.Run(pr.Km(s["km"]), pe.MetersPerSecond(3.0))
                self.feedback[s["key"]] = pr.Feedback(4, 0, False)
        self.health = pr.Health()
        self.today = TODAY

    def review(self, **kw):
        return pr.review_week(PID, body(), W, self.today, self.links, self.linked, self.runs, self.feedback,
                              kw.get("health", self.health))

    def kinds(self, **kw):
        return sorted((c["rule"], c["kind"]) for c in self.review(**kw)["changes"])

    def run_km(self, key, km):
        self.runs[self.linked[key]] = dataclasses.replace(self.runs[self.linked[key]], km=pr.Km(km))

    def test_quiet_week_has_no_change(self):
        out = self.review()
        self.assertEqual(out["changes"], [])
        self.assertEqual(out["metrics"]["completion_pct"], 100.0)
        self.assertEqual(out["metrics"]["planned_km"], 50.0)
        self.assertEqual(out["rules_version"], pr.REVIEW_RULES_VERSION)

    def test_a1_completion_boundary(self):
        self.run_km("20261206-run", 5.0)          # 35 of 50 km: exactly 70%
        self.assertEqual(self.kinds(), [])
        self.run_km("20261206-run", 4.9)          # 69.8%
        out = self.review()
        (c,) = out["changes"]
        self.assertEqual((c["rule"], c["kind"]), ("A1", "repeat_volume"))
        self.assertEqual((c["from"]["km"], c["to"]["km"]), (55.0, 50.0))
        self.assertEqual(c["evidence"], {"metric": "completion_pct", "value": 69.8, "threshold": 70})
        self.assertIn("69.8%", c["reason"])
        self.assertTrue(c["change_id"].startswith("c") and len(c["change_id"]) == 17)

    def test_open_sessions_are_not_due(self):
        self.links["20261206-run"] = None          # not settled yet: neither due nor done
        out = self.review()
        self.assertEqual(out["metrics"]["due_km"], 30.0)
        self.assertEqual(out["changes"], [])

    def test_a2_hrv_or_short_nights(self):
        self.assertEqual(self.kinds(health=pr.Health(flags=("hrv_down",), hrv_7d=50.0, hrv_28d=55.0)),
                         [("A2", "early_deload")])
        c = self.review(health=pr.Health(flags=("short_nights",), short_nights=3))["changes"][0]
        self.assertEqual(c["to"]["km"], 39.0)   # 0.78 x 50
        self.assertEqual(c["evidence"]["metric"], "short_nights")
        self.assertEqual(self.kinds(health=pr.Health(short_nights=2)), [])

    def test_acwr_alone_never_triggers(self):
        self.assertEqual(self.kinds(health=pr.Health(acwr=1.9)), [])
        for key in ("20261201-run", "20261204-run"):
            self.feedback[key] = pr.Feedback(6, 0, False)
        self.assertEqual(self.kinds(health=pr.Health(acwr=1.3)), [])        # not above 1.3
        c = self.review(health=pr.Health(acwr=1.31))["changes"][0]           # with easy RPE 6
        self.assertEqual((c["rule"], c["evidence"]["metric"]), ("A2", "acwr"))
        self.assertEqual(c["evidence"]["also"][0]["metric"], "easy_rpe_mean")
        self.feedback["20261204-run"] = pr.Feedback(5, 0, False)                   # mean 5.5
        self.assertEqual(self.kinds(health=pr.Health(acwr=1.31)), [])

    def test_acwr_is_cited_beside_hrv(self):
        c = self.review(health=pr.Health(flags=("hrv_down",), hrv_7d=50.0, hrv_28d=56.0, acwr=1.4))["changes"][0]
        self.assertEqual(c["evidence"]["metric"], "hrv_drop_ms")
        self.assertEqual(c["evidence"]["also"], [{"metric": "acwr", "value": 1.4, "threshold": 1.3}])

    def test_a3_missed_long_run(self):
        self.links["20261206-run"] = "missed"
        kinds = self.kinds()
        self.assertIn(("A3", "long_run_km"), kinds)
        c = next(c for c in self.review()["changes"] if c["rule"] == "A3")
        self.assertEqual((c["from"]["km"], c["to"]["km"]), (22.0, 20.0))
        self.assertEqual(c["evidence"]["threshold"], 22.0)
        self.links["20261206-run"] = "replaced"
        self.assertNotIn(("A3", "long_run_km"), self.kinds())

    def test_a4_easy_rpe_two_weeks(self):
        for key in ("20261124-run", "20261127-run", "20261201-run", "20261204-run"):
            self.feedback[key] = pr.Feedback(6, 0, False)
        self.assertEqual(self.kinds(), [("A4", "cut_week"), ("A4", "easy_band")])
        band = next(c for c in self.review()["changes"] if c["kind"] == "easy_band")
        self.assertEqual(band["to"], {"slowdown": 0.03})
        cut = next(c for c in self.review()["changes"] if c["kind"] == "cut_week")
        self.assertEqual(cut["to"]["km"], 49.5)
        self.feedback["20261127-run"] = pr.Feedback(5, 0, False)    # last week 5.5
        self.assertEqual(self.kinds(), [])

    def test_a5_quality_above_target(self):
        planned = 10000 / (50 * 60)                            # 3.333 m/s
        aid = self.linked["20261202-run"]
        self.feedback["20261202-run"] = pr.Feedback(5, 0, False)
        self.runs[aid] = pr.Run(pr.Km(10.0), pe.MetersPerSecond(planned * 1.02 + 1e-6))
        c = self.review()["changes"][0]
        self.assertEqual((c["rule"], c["kind"], c["edits"]), ("A5", "threshold_retest", []))
        self.assertIn("advice", c)
        self.runs[aid] = pr.Run(pr.Km(10.0), pe.MetersPerSecond(planned * 1.019))
        self.assertEqual(self.kinds(), [])
        self.runs[aid] = pr.Run(pr.Km(10.0), pe.MetersPerSecond(planned * 1.05))
        self.feedback["20261202-run"] = pr.Feedback(6, 0, False)
        self.assertEqual(self.kinds(), [])

    def test_a6_pain_or_sharp(self):
        self.feedback["20261201-run"] = pr.Feedback(4, 3, False)
        self.assertEqual(self.kinds(), [])
        self.feedback["20261201-run"] = pr.Feedback(4, 4, False)
        out = self.review()["changes"]
        self.assertEqual(sorted(c["kind"] for c in out), ["drop_quality", "hold_progression"])
        self.assertTrue(all("physiotherapist or doctor" in c["advice"] for c in out))
        self.feedback["20261201-run"] = pr.Feedback(4, 1, True)
        out = self.review()["changes"]
        self.assertEqual((out[0]["evidence"]["metric"], out[0]["evidence"]["threshold"]), ("sharp", True))

    def test_a6_always_surfaces_its_advice(self):
        """a6: next week already lighter and without quality, or no next week: an advice-only change."""
        b = body()
        for s in b["weeks"][2]["sessions"]:
            if s["role"] == "threshold":
                s["role"] = "strides"
            s["km"] = round(s["km"] * 0.7, 1)
        self.feedback["20261204-run"] = pr.Feedback(4, 8, True)
        out = pr.review_week(PID, b, W, self.today, self.links, self.linked, self.runs, self.feedback, self.health)
        (c,) = out["changes"]
        self.assertEqual((c["rule"], c["kind"], c["edits"]), ("A6", "pain_advice", []))
        self.assertIn("physiotherapist or doctor", c["advice"])
        self.assertEqual(c["evidence"]["value"], 8)
        b["weeks"] = b["weeks"][:2]          # no next week at all
        out = pr.review_week(PID, b, W, self.today, self.links, self.linked, self.runs, self.feedback, self.health)
        self.assertEqual([c["kind"] for c in out["changes"]], ["pain_advice"])

    def test_a7_after_a_missed_week(self):
        for s in body()["weeks"][1]["sessions"]:
            self.links[s["key"]] = "missed"
        for s in body()["weeks"][0]["sessions"]:
            self.run_km(s["key"], s["km"] * 0.8)                 # last week: 40 km done
        c = next(c for c in self.review()["changes"] if c["rule"] == "A7")
        self.assertEqual(c["to"]["km"], 44.0)                   # 1.10 x 40
        self.assertEqual(c["evidence"], {"metric": "last_done_week_km", "value": 40.0, "threshold": 1.1})
        self.links["20261201-run"] = "done"                          # not a missed week
        self.assertNotIn("A7", {c["rule"] for c in self.review()["changes"]})

    def test_a8_red_day_with_quality_today(self):
        self.today = NEXT + dt.timedelta(days=2)               # 20300508-run's day
        self.assertEqual(self.kinds(health=pr.Health(today_level="yellow")), [])
        c = self.review(health=pr.Health(today_level="red"))["changes"][0]
        self.assertEqual((c["rule"], c["kind"]), ("A8", "easy_today"))
        self.today = NEXT + dt.timedelta(days=1)               # an easy day
        self.assertEqual(self.kinds(health=pr.Health(today_level="red")), [])

    def test_no_change_raises_and_no_change_for_an_ended_next_week(self):
        self.links["20261206-run"] = "missed"
        self.feedback["20261201-run"] = pr.Feedback(4, 6, True)
        for c in self.review(health=pr.Health(flags=("hrv_down",)))["changes"]:
            if "km" in (c["to"] or {}):
                self.assertLess(c["to"]["km"], c["from"]["km"])
        self.today = NEXT + dt.timedelta(days=7)
        # The next week has ended: only A6's advice, which changes no session.
        out = self.review(health=pr.Health(flags=("hrv_down",)))["changes"]
        self.assertEqual([(c["kind"], c["edits"]) for c in out], [("pain_advice", [])])

    def test_change_ids_are_stable(self):
        self.run_km("20261206-run", 1.0)
        a, b = self.review()["changes"][0], self.review()["changes"][0]
        self.assertEqual(a["change_id"], b["change_id"])
        self.assertNotEqual(a["change_id"], pr.change_id(PID, PREV, "A1", "repeat_volume"))


INPUTS = pe.PlanInputs(race_date=dt.date(2027, 2, 26), goal_time_s=3 * 3600 + 30 * 60,
                       b_races=(pe.BRace(dt.date(2027, 1, 29), "tri_half"),))
REVIEW_DAY = dt.date(2026, 12, 6)


class Accept(unittest.TestCase):
    """Accepted changes patch the stored version (built on Sunday 2026-12-06): only the targeted sessions change."""

    @classmethod
    def setUpClass(cls):
        # without predictions (MP from the goal, easy on LT multiples): with the marathon prediction's easy band
        # a cap's long-run cut tips the week of 2027-01-04 under 80% easy and these edits are refused
        snap = dataclasses.replace(pe.sample_snapshot(REVIEW_DAY), prediction_s=())
        cls.stored = pe.plan_answer(pe.build_plan(INPUTS, snap, what_if_runs=False))

    def change(self, cid, *edits, kind="repeat_volume"):
        return {"change_id": cid, "kind": kind, "edits": list(edits)}

    def week(self, plan, start):
        return next(w for w in plan["weeks"] if w["start"] == start)

    def sessions(self, plan, pred=lambda s: True):
        return {s["key"]: s for w in plan["weeks"] for s in w["sessions"] if pred(s)}

    def assert_only(self, new, changed_dates, today):
        """Every session outside `changed_dates` is exactly the stored one (a later long run may only be
        shorter: the 2 km step); nothing up to today changed except a date listed."""
        old = self.sessions(self.stored)
        now = self.sessions(new)
        for key, s in old.items():
            if s["date"] in changed_dates:
                continue
            if s["role"] == "long" and s["date"] > today.isoformat() and key in now:
                self.assertLessEqual(now[key]["km"], s["km"])
                continue
            self.assertEqual(now.get(key), s, key)
        self.assertTrue(set(now) <= set(old))
        self.assertEqual(pr.no_raise(self.stored, new, today), [])
        self.assertEqual(set(pr.patch_problems(new, today)) - set(pr.patch_problems(self.stored, today)), set())

    def test_inputs_round_trip(self):
        full = dataclasses.replace(INPUTS, overrides=(pe.Override("cap_week_km", week_start=W, km=30.0),
                                                      pe.Override("long_run_day", week_start=W, weekday=5)),
                                   easy_band_slowdown=0.03, course_id=12)
        self.assertEqual(pr.inputs_from_echo(full.echo()), full)

    def test_a_cap_on_monday_wednesday_and_saturday_keeps_the_rest_of_the_week(self):
        start = "2026-12-07"
        stored_week = self.week(self.stored, start)
        for today in (dt.date(2026, 12, 7), dt.date(2026, 12, 9), dt.date(2026, 12, 12)):
            with self.subTest(today=today):
                limit = stored_week["planned_km"] - 6
                new, accepted, refused, applied = pr.apply_changes(
                    self.stored, [self.change("c" + "1" * 16, {"op": "cap_week", "week_start": start, "km": limit})],
                    today)
                cut = [s["date"] for s in stored_week["sessions"] if s["date"] > today.isoformat()]
                self.assertEqual(refused, [])
                self.assert_only(new, set(cut), today)
                week = self.week(new, start)
                # The runs up to today count as planned (no done km given); only later ones were cut.
                if applied:
                    self.assertLessEqual(week["planned_km"], max(limit, sum(
                        s["km"] for s in stored_week["sessions"] if s["date"] <= today.isoformat())) + 0.05)
                    self.assertNotEqual(new["content_hash"], self.stored["content_hash"])
                for s in stored_week["sessions"]:
                    if s["date"] <= today.isoformat():
                        self.assertIn(s, week["sessions"])

    def test_done_km_count_in_the_current_week(self):
        start, today = "2026-12-07", dt.date(2026, 12, 9)
        stored_week = self.week(self.stored, start)
        past = [s for s in stored_week["sessions"] if s["date"] < today.isoformat()]
        self.assertTrue(past)
        limit = stored_week["planned_km"] - 2
        edit = {"op": "cap_week", "week_start": start, "km": limit}
        as_planned, _, _, _ = pr.apply_changes(self.stored, [self.change("c" + "1" * 16, edit)], today)
        nothing_run, _, _, _ = pr.apply_changes(self.stored, [self.change("c" + "1" * 16, edit)], today, done={})
        # With the past runs not run, less has to be cut from the rest of the week.
        self.assertGreaterEqual(self.week(nothing_run, start)["planned_km"], self.week(as_planned, start)["planned_km"])

    def test_held_deload_and_dropped_quality_cut_the_next_week(self):
        """wk0: the stored plan's week of 2026-12-14 is cut to 78% and its quality made easy, applied on its
        Monday or its Wednesday."""
        nxt = "2026-12-14"
        week = self.week(self.stored, nxt)
        quality = [s for s in week["sessions"] if s["role"] in pe.QUALITY_ROLES]
        for today in (dt.date(2026, 12, 14), dt.date(2026, 12, 16)):
            with self.subTest(today=today):
                changes = [self.change("c" + "1" * 16, {"op": "cap_week", "week_start": nxt,
                                                        "km": round(week["planned_km"] * 0.78, 1)}, kind="early_deload"),
                           self.change("c" + "2" * 16, {"op": "easy_quality", "week_start": nxt}, kind="drop_quality")]
                new, accepted, refused, applied = pr.apply_changes(self.stored, changes, today)
                self.assertEqual(refused, [])
                new_week = self.week(new, nxt)
                self.assertLess(new_week["planned_km"], week["planned_km"])
                later_quality = [s for s in new_week["sessions"] if s["role"] in pe.QUALITY_ROLES
                                 and s["date"] > today.isoformat()]
                self.assertEqual(later_quality, [])
                self.assertIn("c" + "1" * 16, applied)
                if any(s["date"] > today.isoformat() for s in quality):
                    self.assertIn("c" + "2" * 16, applied)

    def test_a8_eases_only_todays_quality(self):
        q = next(s for w in self.stored["weeks"] for s in w["sessions"] if s["role"] in pe.QUALITY_ROLES)
        today = dt.date.fromisoformat(q["date"])
        new, accepted, refused, applied = pr.apply_changes(
            self.stored, [self.change("c" + "3" * 16, {"op": "easy_day", "date": q["date"]}, kind="easy_today")], today)
        self.assertEqual(applied, ["c" + "3" * 16])
        same = self.sessions(new)[q["key"]]
        self.assertEqual((same["role"], same["km"], same["date"]), ("easy", q["km"], q["date"]))
        self.assert_only(new, {q["date"]}, today - dt.timedelta(days=1))

    def test_the_easy_band_slows_only_later_easy_and_long_runs(self):
        today = dt.date(2026, 12, 9)
        new, _acc, _ref, applied = pr.apply_changes(
            self.stored, [self.change("c" + "4" * 16, {"op": "easy_band", "slowdown": 0.03}, kind="easy_band")], today)
        self.assertEqual(applied, ["c" + "4" * 16])
        self.assertEqual(new["inputs"]["easy_band_slowdown"], 0.03)
        old, now = self.sessions(self.stored), self.sessions(new)
        for key, s in old.items():
            if s["date"] <= today.isoformat() or s["role"] not in ("easy", "long", "strides"):
                self.assertEqual(now[key], s)
            else:
                # strides: the jog between them is shorter at the slower easy pace
                self.assertLessEqual(now[key]["km"], s["km"])
                self.assertGreaterEqual(now[key]["km"], round(s["km"] - (0.1 if s["role"] == "strides" else 0), 1))
                self.assertLess(now[key]["steps"][0]["speed_mps"][1], s["steps"][0]["speed_mps"][1])
        self.assertEqual(pr.no_raise(self.stored, new, today), [])

    def test_a_slower_easy_run_stays_under_the_easy_cap(self):
        # The daily report calls a run of 75 minutes or more long: a slower band must not make an easy run long.
        today = dt.date(2026, 12, 9)
        stored = copy.deepcopy(self.stored)
        later = [s for w in stored["weeks"] for s in w["sessions"]
                 if s["role"] == "easy" and s["date"] > today.isoformat()]
        later[0]["km"] = 14.0
        new, _acc, _ref, applied = pr.apply_changes(
            stored, [self.change("c" + "4" * 16, {"op": "easy_band", "slowdown": 0.05}, kind="easy_band")], today)
        self.assertEqual(applied, ["c" + "4" * 16])
        for s in self.sessions(new).values():
            if s["role"] == "easy" and s["date"] > today.isoformat():
                mid = sum(s["steps"][0]["speed_mps"]) / 2
                self.assertLessEqual(s["km"] * 1000 / mid, pe.RULES["easy_max_min"] * 60 + 1e-6, s)
        self.assertLess(self.sessions(new)[later[0]["key"]]["km"], 14.0)
        self.assertEqual(pr.no_raise(stored, new, today), [])

    def test_a_shorter_long_run_keeps_the_step_and_too_short_is_refused(self):
        today = dt.date(2026, 12, 7)
        week = self.week(self.stored, "2026-12-07")
        long = next(s for s in week["sessions"] if s["role"] == "long")
        new, _acc, refused, applied = pr.apply_changes(
            self.stored, [self.change("c" + "5" * 16, {"op": "cap_long", "week_start": "2026-12-07",
                                                        "km": long["km"] - 3})], today)
        self.assertEqual((refused, applied), ([], ["c" + "5" * 16]))
        self.assertLessEqual(self.sessions(new)[long["key"]]["km"], long["km"] - 3 + 0.05)
        self.assertEqual(set(pr.patch_problems(new, today)) - set(pr.patch_problems(self.stored, today)), set())
        _new, _acc, refused, applied = pr.apply_changes(
            self.stored, [self.change("c" + "6" * 16, {"op": "cap_long", "week_start": "2026-12-07", "km": 2.0})],
            today)
        self.assertEqual((refused[0]["rule"], applied), ("easy_min_km", []))

    def test_a_refused_lower_cap_falls_back_to_the_next(self):
        """caps: a cap so low it breaks the easy share is refused; the other cap of the week still applies,
        in either order."""
        week = next(w for w in self.stored["weeks"] if w["kind"] == "normal"
                    and any(s["role"] in pe.QUALITY_ROLES for s in w["sessions"]))
        start = week["start"]
        today = dt.date.fromisoformat(start) - dt.timedelta(days=1)
        quality = next(s for s in week["sessions"] if s["role"] in pe.QUALITY_ROLES)
        low = self.change("c" + "a" * 16, {"op": "cap_week", "week_start": start, "km": quality["km"] + 1})
        high = self.change("c" + "b" * 16, {"op": "cap_week", "week_start": start, "km": week["planned_km"] - 4})
        for order in ([low, high], [high, low]):
            new, accepted, refused, applied = pr.apply_changes(self.stored, order, today)
            self.assertEqual([r["change_id"] for r in refused], ["c" + "a" * 16])
            self.assertEqual(applied, ["c" + "b" * 16])
            self.assertLessEqual(self.week(new, start)["planned_km"], week["planned_km"] - 4 + 0.05)

    def test_past_week_refused_cap_above_held_and_advice_accepted_without_a_patch(self):
        today = REVIEW_DAY
        past = self.change("c" + "6" * 16, {"op": "cap_week", "week_start": "2026-11-23", "km": 10})
        high = self.change("c" + "7" * 16, {"op": "cap_week", "week_start": "2026-12-07", "km": 500})
        advice = self.change("c" + "8" * 16, kind="pain_advice")
        new, accepted, refused, applied = pr.apply_changes(self.stored, [past, high, advice], today)
        self.assertEqual(accepted, ["c" + "7" * 16, "c" + "8" * 16])
        self.assertEqual(applied, [])
        self.assertEqual(refused[0]["rule"], "past_week")
        self.assertEqual(new["content_hash"], self.stored["content_hash"])
        self.assertEqual(new["weeks"], self.stored["weeks"])

    def test_bad_and_unknown_edits_are_refused(self):
        bad = [self.change("c" + "1" * 16, {"op": "raise_week", "week_start": "2026-12-07"}),
               self.change("c" + "2" * 16, {"op": "cap_week", "week_start": "2026-12-07"}),
               self.change("c" + "3" * 16, {"op": "easy_day", "date": "2026-12-01"})]
        _new, accepted, refused, applied = pr.apply_changes(self.stored, bad, REVIEW_DAY)
        self.assertEqual([r["rule"] for r in refused], ["unknown_edit", "bad_edit", "past_day"])
        self.assertEqual((accepted, applied), ([], []))


class EngineOverrides(unittest.TestCase):
    """The additive engine overrides Stage 4 uses: cap_km and easy by date."""

    def test_cap_km_only_shortens(self):
        snap = pe.sample_snapshot()
        base = pe.build_plan(INPUTS, snap, what_if_runs=False)
        long = next(s for w in base["weeks"] for s in w["sessions"] if s["role"] == "long")
        day = dt.date.fromisoformat(long["date"])
        up = pe.build_plan(dataclasses.replace(INPUTS, overrides=(pe.Override("cap_km", date=day, km=long["km"] + 3),)),
                           snap, what_if_runs=False)
        self.assertEqual(up["overrides"]["refused"][0]["rule"], "raises_load")
        steps = tuple(pe.Override("cap_km", date=dt.date.fromisoformat(d), km=k)
                      for d, k in reversed(pr._long_steps(base, long["date"], long["km"] - 2)))
        self.assertTrue(steps and all(o.kind == "cap_km" for o in steps))
        down = pe.build_plan(dataclasses.replace(INPUTS, overrides=steps + (pe.Override("cap_km", key=long["key"],
                                                                                        km=long["km"] - 2),)),
                             snap, what_if_runs=False)
        self.assertEqual(down["overrides"]["refused"], [])
        q = next(s for w in base["weeks"] for s in w["sessions"] if s["role"] in pe.QUALITY_ROLES)
        bad = pe.build_plan(dataclasses.replace(INPUTS, overrides=(pe.Override("cap_km", key=q["key"], km=3.0),)),
                            snap, what_if_runs=False)
        self.assertEqual(bad["overrides"]["refused"][0]["rule"], "cap_km_role")

    def test_slowdown_out_of_range(self):
        with self.assertRaises(pe.PlanError):
            pe.check_inputs(dataclasses.replace(INPUTS, easy_band_slowdown=0.2))
        self.assertNotIn("easy_band_slowdown", INPUTS.echo())


class RemadeNotes(unittest.TestCase):
    """Rule 7's fixed notes survive a patched session: the first run's as it is, the first long run's with its
    new km; another role drops the long-run note."""

    def test_notes(self):
        paces = pe.Paces(pe.MetersPerSecond(3.85), None, pe.RULES)
        sore = {"key": "20270202-run", "date": "2027-02-02", "note": pe.NOTE_SORE}
        self.assertEqual(pr._remake(sore, "easy", 4000.0, paces)["note"], pe.NOTE_SORE)
        first = {"key": "20270205-run", "date": "2027-02-05", "note": pe.first_long_note(15200.0)}
        out = pr._remake(first, "long", 12000.0, paces)
        self.assertEqual(out["note"], pe.first_long_note(12000.0))
        self.assertIn("Run up to 12.0 km", out["note"])
        self.assertIn("otherwise run 8-9 km easy", out["note"])
        self.assertNotIn("note", pr._remake(first, "easy", 12000.0, paces))


if __name__ == "__main__":
    unittest.main()
