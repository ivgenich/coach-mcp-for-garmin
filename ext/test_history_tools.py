"""Tests for history_tools: argument checks, the "No data" texts, the answer shape (dates, numbers, why,
followed_by, missing, index), flags and order, key mode, database failures, privacy of logs, text hygiene
and registration.

    cd ext && python3 -m unittest test_history_tools -v

The store is history_store.MemoryHistoryStore filled by evals/history/athlete.py (the same synthetic athlete
as the retrieval evals). Searches need the history packages (config/requirements-history.txt) and skip
without them; argument checks and fixed texts are standard library.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

import history_store as hs
import history_tools as ht
import tool_errors

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from evals.history import athlete as A  # noqa: E402

HAVE_LC = all(importlib.util.find_spec(m) is not None
              for m in ("langchain_core", "langchain_classic", "langsmith", "numpy"))
LC_SKIP = "needs the history packages (config/requirements-history.txt)"
HAVE_ANYIO = importlib.util.find_spec("anyio") is not None and importlib.util.find_spec("mcp") is not None

_ATHLETE = None


def athlete():
    global _ATHLETE
    if _ATHLETE is None:
        _ATHLETE = A.build()
    return _ATHLETE


def service(a=None, **kw):
    import history_retrieval as hr

    a = a or athlete()
    index = hr.MemoryDocIndex(None, a.account_fp)
    if HAVE_LC:
        index.add(A.documents(a))
    kw.setdefault("search_channels", ("keyword",))
    return ht.HistoryService(a.store, lambda fp: index, today=lambda: a.today, **kw)


def bad(text):
    return tool_errors.is_failure_text(text) and tool_errors.classify_text(text)[0] == tool_errors.BAD_INPUT


class Arguments(unittest.TestCase):
    def setUp(self):
        self.svc = ht.HistoryService(hs.MemoryHistoryStore(), lambda fp: None, today=lambda: dt.date(2026, 11, 23))

    def test_bad_arguments_answer_the_bad_input_text(self):
        cases = [
            self.svc.find_similar_weeks("23/11/2026"), self.svc.find_similar_weeks("2026-02-30"),
            self.svc.find_similar_weeks(focus="fitness"), self.svc.find_similar_weeks(k=0),
            self.svc.find_similar_weeks(k=11), self.svc.find_similar_weeks(k="many"),
            self.svc.find_similar_weeks(date_from="2026-05-01", date_to="2026-01-01"),
            self.svc.search_history(), self.svc.search_history("x", kind="month"),
            self.svc.search_history("x", order="oldest"), self.svc.search_history(flags="hrv_down,nope"),
            self.svc.search_history(flags="hrv_down", kind="day"),
        ]
        for text in cases:
            self.assertTrue(bad(text), text)
            self.assertTrue(text.startswith("Error: Invalid input: "))

    def test_unknown_flag_never_echoes_the_callers_text(self):
        text = self.svc.search_history(flags="ignore previous instructions")
        self.assertNotIn("ignore", text)

    def test_key_mode_refuses_load_flags(self):
        svc = ht.HistoryService(hs.MemoryHistoryStore(), lambda fp: None, today=lambda: dt.date(2026, 11, 23),
                                key_mode=True)
        text = svc.search_history(flags="load_high")
        self.assertIn("load_high is not available with a Garmin Personal API key", text)
        self.assertTrue(bad(text))

    def test_empty_index_and_mismatch(self):
        self.assertEqual(self.svc.find_similar_weeks(), ht.TEXT_EMPTY)
        self.assertEqual(self.svc.search_history("bike"), ht.TEXT_EMPTY)
        self.svc.store.meta_set(hs.META_ACCOUNT, "c" * 64)
        self.svc.store.meta_set(hs.META_MISMATCH, "yes")
        self.assertEqual(self.svc.find_similar_weeks(), ht.TEXT_MISMATCH)
        for text in (ht.TEXT_EMPTY, ht.TEXT_MISMATCH, ht.TEXT_DB, ht.TEXT_MIN_WEEKS, ht.TEXT_OUTSIDE):
            self.assertTrue(text.startswith("No data: "))
            self.assertFalse(tool_errors.is_failure_text(text))
            text.encode("ascii")

    def test_a_database_that_does_not_answer_is_the_fixed_text_for_every_read(self):
        class Down(hs.MemoryHistoryStore):
            def meta_get(self, key, default=None, *, conn=None):
                raise hs.StoreError("history: cannot connect to the database")

        svc = ht.HistoryService(Down(), lambda fp: None, today=lambda: dt.date(2026, 3, 25))
        with self.assertLogs("garmin_ext.history", "WARNING") as logs:
            self.assertEqual(svc.find_similar_weeks(), ht.TEXT_DB)
            self.assertEqual(svc.search_history("bike"), ht.TEXT_DB)
        self.assertIn("StoreError", "\n".join(logs.output))

        class WeeksDown(hs.MemoryHistoryStore):
            def weeks(self, *a, **kw):
                raise hs.StoreError("history: database error")

        store = WeeksDown()
        store.meta_set(hs.META_ACCOUNT, "c" * 64)
        svc = ht.HistoryService(store, lambda fp: None, today=lambda: dt.date(2026, 3, 25))
        with self.assertLogs("garmin_ext.history", "WARNING"):
            self.assertEqual(svc.find_similar_weeks(), ht.TEXT_DB)
            self.assertEqual(svc.search_history("", kind="day", flags="rest_day"), ht.TEXT_DB)

    def test_complete_needs_every_older_day_stored_and_final(self):
        import history_extract as hx

        today = dt.date(2026, 3, 25)
        fp = "c" * 64
        store = hs.MemoryHistoryStore()
        store.meta_set(hs.META_ACCOUNT, fp)
        start = today - dt.timedelta(days=60)
        day = start
        while day < today:
            store.upsert_day(fp, day, {"mode": "key", "rhr": 50, "missing": []}, hx.is_final_day(day, today))
            day += dt.timedelta(days=1)
        svc = ht.HistoryService(store, lambda fp: None, today=lambda: today)
        self.assertFalse(svc.index_block(fp)["complete"], "no floor and not a year back")
        store.meta_set(hs.META_BACKFILL_FLOOR, start.isoformat())
        self.assertTrue(svc.index_block(fp)["complete"])
        # A day that stayed open (a source failed on every try) counts as missing.
        store._days[(fp, start + dt.timedelta(days=5))]["final"] = False
        self.assertFalse(svc.index_block(fp)["complete"])
        store._days[(fp, start + dt.timedelta(days=5))]["final"] = True
        # Probe days stored before the floor do not move the index start.
        store.upsert_day(fp, start - dt.timedelta(days=21), {"mode": "key", "missing": []}, True)
        block = svc.index_block(fp)
        self.assertEqual((block["from"], block["complete"]), (start.isoformat(), True))

    def _owner_store(self, days_to, final_weeks):
        """The owner's case: a final stored day for every date up to `days_to` and final weeks ending on the
        Sundays in `final_weeks`, plus one week that is stored but not final (it must not count)."""
        fp = "c" * 64
        store = hs.MemoryHistoryStore()
        store.meta_set(hs.META_ACCOUNT, fp)
        store.meta_set(hs.META_BACKFILL_FLOOR, "2026-10-01")
        day = dt.date(2026, 10, 1)
        while day <= days_to:
            store.upsert_day(fp, day, {"mode": "key", "rhr": 50, "missing": []}, True)
            day += dt.timedelta(days=1)
        for end in final_weeks:
            store.upsert_week(fp, end - dt.timedelta(days=6), {"mode": "key", "rhr_avg": 50}, True)
        newest = max(final_weeks)
        store.upsert_week(fp, newest + dt.timedelta(days=1), {"mode": "key", "rhr_avg": 50}, False)
        return fp, store

    def test_stale_when_the_newest_final_week_is_behind(self):
        """Live: the daily index reached 23 Nov, the newest week was 9-15 Nov, and stale stayed false."""
        weeks = [dt.date(2026, 11, 1), dt.date(2026, 11, 8), dt.date(2026, 11, 15)]
        fp, store = self._owner_store(dt.date(2026, 11, 23), weeks)
        cases = ((dt.date(2026, 11, 24), False),   # 9-15 Nov is the newest week due
                 (dt.date(2026, 11, 25), False),   # Sunday 22 Nov is final today; the worker gets a day
                 (dt.date(2026, 11, 26), True),    # 16-22 Nov should be built by now
                 (dt.date(2026, 11, 27), True))
        for today, stale in cases:
            with self.subTest(today=today):
                svc = ht.HistoryService(store, lambda fp: None, today=lambda today=today: today)
                block = svc.index_block(fp)
                self.assertEqual(block["weeks_to"], "2026-11-15", "the non-final week 16-22 Nov does not count")
                self.assertEqual(block["stale"], stale)
                # The day rule alone flags nothing here (newest day 23 Nov, at most 3 days before yesterday).
        self.assertEqual(ht.expected_week_end(dt.date(2026, 11, 26)), dt.date(2026, 11, 22))
        self.assertEqual(ht.expected_week_end(dt.date(2026, 11, 25)), dt.date(2026, 11, 15))
        self.assertEqual(ht.newest_final_week_end([]), None)

    def test_the_not_indexed_note_says_when_the_week_appears(self):
        note = ht.not_indexed_note(dt.date(2026, 11, 22))
        self.assertIn("2026-11-16 to 2026-11-22", note)
        self.assertIn("from about 2026-11-25", note)
        note.encode("ascii")

    def test_descriptions(self):
        for d in (ht.FIND_DESCRIPTION, ht.SEARCH_DESCRIPTION):
            self.assertLessEqual(len(d), 2048)
            self.assertIn("Does not call Garmin", d)
            self.assertIn("how the athlete felt", d)
            d.encode("ascii")
        self.assertIn("English", ht.SEARCH_DESCRIPTION)

    def test_flag_lists_match_the_rules(self):
        import daily_status
        import history_extract as hx

        self.assertEqual(ht.WEEK_FLAG_IDS, hx.WEEK_FLAGS)
        self.assertTrue(set(ht.DAY_FLAG_IDS) <= set(daily_status.FLAG_LEVELS))
        for fid in ht.known_ids("week"):
            self.assertIn(fid, ht.SEARCH_DESCRIPTION)


@unittest.skipUnless(HAVE_LC, LC_SKIP)
class SimilarWeeks(unittest.TestCase):
    def setUp(self):
        self.a = athlete()
        self.svc = service(self.a)

    def test_answer_shape_with_dates_numbers_why_followed_by(self):
        ref = self.a.episodes["hrv_drop_3"][0]
        out = json.loads(self.svc.find_similar_weeks(ref, "overall", 5))
        self.assertEqual(out["kind"], "similar_weeks")
        self.assertEqual(out["reference"]["start"], ref)
        self.assertEqual(set(out["index"]), {"from", "to", "weeks", "weeks_to", "days", "complete", "stale"})
        self.assertTrue(out["index"]["complete"])
        self.assertFalse(out["index"]["stale"])
        starts = [r["start"] for r in out["results"]]
        self.assertIn(self.a.episodes["hrv_drop_1"][0], starts[:3])
        top = out["results"][0]
        for key in ("rank", "start", "end", "level", "flags", "tags", "numbers", "why", "followed_by", "missing",
                    "similarity", "matched_on"):
            self.assertIn(key, top)
        self.assertEqual(top["matched_on"], ["numbers"])
        self.assertIn("plan", top["missing"])
        stored = {w["start"].isoformat(): w for w in self.a.store.weeks(self.a.account_fp, self.a.week0,
                                                                         self.a.today)}
        for r in out["results"]:
            n = stored[r["start"]]["numbers"]
            for k, v in r["numbers"].items():
                self.assertEqual(v, round(n[k], 1) if isinstance(n[k], float) else n[k], "numbers are stored values")
            self.assertGreater(abs((dt.date.fromisoformat(r["start"]) - dt.date.fromisoformat(ref)).days), 14)
            self.assertLessEqual(len(r["followed_by"]), 2)
        after = self.a.episodes["after_drop_1"][0]
        drop1 = next(r for r in out["results"] if r["start"] == self.a.episodes["hrv_drop_1"][0])
        self.assertEqual(drop1["followed_by"][0]["start"], after)

    def test_default_reference_is_last_complete_week(self):
        out = json.loads(self.svc.find_similar_weeks())
        self.assertEqual(out["reference"]["start"], A.LAST_WEEK_START.isoformat())
        self.assertNotIn("note", out["reference"])

    def test_outside_index(self):
        text = self.svc.find_similar_weeks("2019-01-01")
        self.assertTrue(text.startswith("No data: 2019-01-01 is not in the history index"))

    def test_two_week_user_gets_the_minimum_text(self):
        store = hs.MemoryHistoryStore()
        a = self.a
        for w in a.store.weeks(a.account_fp, a.week0, a.week0 + dt.timedelta(days=13)):
            store.upsert_week(a.account_fp, w["start"], w["numbers"], True)
        for d in a.store.days(a.account_fp, a.week0, a.week0 + dt.timedelta(days=13)):
            store.upsert_day(a.account_fp, d["date"], d["numbers"], True)
        store.meta_set(hs.META_ACCOUNT, a.account_fp)
        import history_retrieval as hr

        svc = ht.HistoryService(store, lambda fp: hr.MemoryDocIndex(None, fp), today=lambda: a.today)
        text = svc.find_similar_weeks()
        self.assertTrue(text.startswith("No data: similar-week search needs at least 8 indexed weeks"), text)
        self.assertIn("2 so far", text)

    def test_database_failure_is_a_fixed_text_and_logs_no_detail(self):
        import history_retrieval as hr

        with mock.patch.object(hr, "search", side_effect=hr.HistoryUnavailable("database")), \
                self.assertLogs("garmin_ext.history", "WARNING") as logs:
            self.assertEqual(self.svc.find_similar_weeks(), ht.TEXT_DB)
        self.assertIn("HistoryUnavailable", "\n".join(logs.output))

    def test_answer_goes_through_text_hygiene(self):
        import text_hygiene

        with mock.patch.object(text_hygiene, "clean_tool_answer", side_effect=lambda t: "CLEANED") as clean:
            self.assertEqual(self.svc.find_similar_weeks(), "CLEANED")
        clean.assert_called_once()

    def test_model_missing_drops_vector_channel(self):
        import history_embed

        class Emb:
            def load(self):
                raise history_embed.ModelUnavailable("backoff")

        index = mock.Mock(embeddings=Emb())
        self.assertEqual(self.svc._channels(index, ("numbers", "vector")), ("numbers",))
        self.assertEqual(self.svc._channels(index, ("vector",)), ht.FALLBACK_CHANNELS)


@unittest.skipUnless(HAVE_LC, LC_SKIP)
class SearchHistory(unittest.TestCase):
    def setUp(self):
        self.a = athlete()
        self.svc = service(self.a)

    def test_flags_newest_first(self):
        out = json.loads(self.svc.search_history(flags="short_nights", order="newest"))
        starts = [r["start"] for r in out["results"]]
        self.assertEqual(starts, sorted(self.a.episodes["sleep_debt_2"] + self.a.episodes["sleep_debt_1"],
                                        reverse=True))
        first = out["results"][0]
        self.assertEqual(first["why"][0], f"has short_nights (nights_under_6h {first['numbers']['nights_under_6h']})")
        self.assertIn("flags", first["matched_on"])

    def test_every_reason_names_stored_numbers_of_its_result(self):
        # A user can check why a result matched: each reason quotes values from the result's own numbers.
        searches = [dict(flags="short_nights"), dict(flags="hrv_down"), dict(query="heavy bike block"),
                    dict(query="taper week"), dict(kind="day", flags="short_sleep", k=3),
                    dict(kind="day", query="high resting heart rate", k=3),
                    dict(query="training moderate", order="newest", k=10), dict(query="training moderate", k=10),
                    dict(kind="day", query="sleep normal", k=10)]
        plain = 0
        for args in searches:
            with self.subTest(**args):
                out = json.loads(self.svc.search_history(**args))
                for r in out["results"]:
                    self.assertTrue(r["start"] and r["end"])
                    self.assertTrue(r["why"], r)     # every result shape, also a pure word or meaning match
                    if not (r["flags"] or r["tags"]):
                        plain += 1
                        self.assertEqual(len(r["why"]), 1)
                        self.assertRegex(r["why"][0], r"^(text|meaning|text and meaning) match \(.+\)$")
                    for reason in r["why"]:
                        inner = reason[reason.index("(") + 1:-1] if "(" in reason else ""
                        for pair in filter(None, inner.split(", ")):
                            key, value = pair.rsplit(" ", 1)
                            self.assertEqual(str(r["numbers"][key]), value, (reason, r["numbers"]))
        self.assertGreater(plain, 0, "no result without a flag or tag was checked")

    def test_plain_match_why_names_main_numbers_never_the_query(self):
        week = {"total_hours": 9.6, "sleep_avg_h": 7.1, "hrv_delta_ms": -1, "sessions": 6, "rhr_avg": 48.0}
        self.assertEqual(ht._text_why({}, [], ["zzqx"], week, ["keywords"], "week"),
                         ["text match (total_hours 9.6, sleep_avg_h 7.1, hrv_delta_ms -1, sessions 6)"])
        day = {"sleep_h": 6.2, "hrv": 51, "rhr": 47, "sessions": 1}
        self.assertEqual(ht._text_why({}, [], [], day, ["meaning"], "day"),
                         ["meaning match (sleep_h 6.2, hrv 51, rhr 47, sessions 1)"])
        self.assertEqual(ht._text_why({}, [], [], {"stress_avg": 30}, ["keywords", "meaning"], "day"),
                         ["text and meaning match (stress_avg 30)"])
        self.assertEqual(ht._text_why({}, [], [], {}, ["keywords"], "week"), ["text match (no stored numbers)"])

    def test_words_newest_first_keeps_relevant_weeks_only(self):
        # Common words ("heavy" is in "training heavy", "bike" in every week) must not let the latest
        # unrelated week come first: the newest order applies to the relevant results only.
        bike = set(self.a.episodes["bike_block_1"] + self.a.episodes["bike_block_2"])
        out = json.loads(self.svc.search_history("heavy bike", order="newest"))
        starts = [r["start"] for r in out["results"]]
        self.assertTrue(starts)
        self.assertEqual(starts, sorted(starts, reverse=True))
        self.assertTrue(set(starts) <= bike, starts)
        self.assertEqual(starts[0], max(bike))
        newest = max(w["start"] for w in self.a.store.weeks(self.a.account_fp, self.a.week0, self.a.today))
        self.assertNotIn(newest.isoformat(), bike)
        self.assertNotIn(newest.isoformat(), starts)

    def test_relevance_floor(self):
        import history_retrieval as hr

        contents = {"a": "bike heavy week", "b": "bike week", "c": "bike week", "d": "bike heavy week"}
        self.assertEqual(hr.relevance_floor(["b", "a", "c", "d"], contents, ["heavy", "bike"], 3), ["a"])
        self.assertEqual(hr.relevance_floor(["b", "c"], contents, ["bike"], 1), ["b"])
        self.assertEqual(hr.relevance_floor(["b", "c"], contents, [], 5), ["b", "c"])       # meaning only
        self.assertEqual(hr.relevance_floor(["b", "c"], contents, ["zzz"], 5), ["b", "c"])  # in no document

    def test_why_item_leaves_out_missing_numbers(self):
        self.assertEqual(ht._why_item("load_high", {"acwr": 1.6}), "has load_high (acwr 1.6)")
        self.assertEqual(ht._why_item("load_high", {}), "has load_high")
        self.assertEqual(ht._text_why({"tags": ["taper"]}, [], [], {"total_hours": 3.1}),
                         ["has taper (total_hours 3.1)"])

    def test_day_range_without_days_is_not_called_empty(self):
        text = self.svc.search_history("short sleep", kind="day", date_from="2020-01-01", date_to="2020-02-01")
        self.assertEqual(text, ht.TEXT_NOTHING.format(n=0, kind="day"))

    def test_words(self):
        out = json.loads(self.svc.search_history("heavy bike block"))
        starts = {r["start"] for r in out["results"]}
        self.assertTrue(starts & set(self.a.episodes["bike_block_1"] + self.a.episodes["bike_block_2"]))
        self.assertEqual(out["results"][0]["matched_on"], ["keywords"])
        self.assertTrue(out["results"][0]["why"])

    def test_days(self):
        out = json.loads(self.svc.search_history(kind="day", flags="short_sleep", order="newest", k=3))
        self.assertEqual(out["results"][0]["kind"], "day")
        self.assertIn("sleep_h", out["results"][0]["numbers"])
        self.assertTrue(out["results"][0]["followed_by"])

    def test_date_range(self):
        lo = self.a.episodes["hrv_drop_2"][0]
        out = json.loads(self.svc.search_history(flags="hrv_down", date_from=lo))
        self.assertTrue(all(r["start"] >= lo for r in out["results"]))

    def test_hebrew_query_notes_no_words(self):
        text = self.svc.search_history("\u05e9\u05d1\u05d5\u05e2 \u05e8\u05db\u05d9\u05d1\u05d4 \u05db\u05d1\u05d3")
        self.assertTrue(text.startswith("No data: nothing") or "No English words" in text, text)

    def test_nothing_matches(self):
        text = self.svc.search_history(flags="hrv_down,taper")
        self.assertTrue(text.startswith("No data: nothing in the stored history matches"), text)

    def test_logs_never_hold_the_query(self):
        import history_retrieval as hr

        with self.assertLogs("garmin_ext", "DEBUG") as logs:
            logging_canary = "zebracanary"
            self.svc.search_history(logging_canary)
            with mock.patch.object(hr, "search", side_effect=hr.HistoryUnavailable("database")):
                self.svc.search_history(logging_canary)
        self.assertNotIn("zebracanary", "\n".join(logs.output))


@unittest.skipUnless(HAVE_ANYIO and HAVE_LC, "needs the history packages (config/requirements-history.txt) and mcp")
class Register(unittest.TestCase):
    def test_registers_two_tools_and_they_answer(self):
        import asyncio

        got = {}

        def adder(fn, name=None, description=None, **kw):
            got[name] = (fn, description)

        ht.register(adder, mock.Mock(garmin_auth="full"), service=service())
        self.assertEqual(set(got), set(ht.TOOL_NAMES))
        self.assertEqual(got["find_similar_weeks"][1], ht.FIND_DESCRIPTION)
        text = asyncio.run(got["search_history"][0](flags="hrv_down", order="newest"))
        self.assertEqual(json.loads(text)["kind"], "history_search")


if __name__ == "__main__":
    unittest.main()
