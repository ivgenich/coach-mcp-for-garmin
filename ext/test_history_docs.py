"""Tests for history_docs (fixed-template texts, tags, metadata). Standard library only:

    cd ext && python3 -m unittest test_history_docs -v
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import unittest
import uuid

import history_docs as hd
import history_extract as hx
import history_store as hs

FP = "c" * 64
MONDAY = dt.date(2026, 3, 16)


def week(**kw):
    n = {"mode": "full", "rules_version": 1, "level": "yellow", "flags": ["hrv_down", "short_nights"],
         "missing": ["plan", "gear", "long_session"], "hrv_avg_7d": 51.0, "hrv_avg_28d": 58.0, "hrv_delta_ms": -7.0,
         "hrv_status": "UNBALANCED", "sleep_avg_h": 6.13, "sleep_nights": 7, "nights_under_6h": 3,
         "total_hours": 9.37, "prev_total_hours": 7.0, "change_pct": 34.0, "sessions": 6, "days_with_data": 7,
         "bike_hours": 6.41, "bike_km": 181.3, "bike_sessions": 3, "run_hours": 2.96, "run_km": 31.7,
         "run_sessions": 3, "swim_sessions": 0, "other_sessions": 0, "acwr_status": "HIGH", "acwr": 1.41,
         "stress_avg": 33.3}
    n.update(kw)
    return hx.check_numbers("week", n)


def day(**kw):
    n = {"mode": "key", "sleep_seconds": 5 * 3600 + 17, "hrv": 43, "hrv_7d": 58, "hrv_status": "LOW",
         "hrv_balanced_low": 55, "rhr": 51, "rhr_7d": 50, "stress_avg": 61, "sessions": 2, "run_sessions": 1,
         "run_seconds": 5000.0, "run_meters": 16000.0, "bike_sessions": 1, "bike_seconds": 3700.0,
         "bike_meters": 31000.0, "missing": []}
    n.update(kw)
    return hx.check_numbers("day", n)


class Texts(unittest.TestCase):
    def test_week_text_is_fixed_words_and_bands(self):
        doc = hd.week_document({"start": MONDAY, "numbers": week(), "final": True})
        text = doc["page_content"]
        self.assertEqual(text, hd.week_document({"start": MONDAY, "numbers": week(), "final": True})["page_content"])
        self.assertTrue(text.startswith("Week 2026-03-16 to 2026-03-22. Level yellow. Flags: hrv_down, short_nights."))
        self.assertIn("HRV below its 28-day average.", text)
        self.assertIn("Training heavy, up a lot on the week before: run 3 sessions, bike 3 sessions.", text)
        self.assertIn("Load high.", text)
        text.encode("ascii")
        # Exact numbers never appear in the text; they are in the metadata.
        stripped = re.sub(r"\d{4}-\d{2}-\d{2}|28-day|under 6 h", "", text)
        for value in ("51", "58", "6.13", "9.37", "181", "34", "1.41"):
            self.assertNotIn(value, stripped)
        self.assertEqual(doc["metadata"]["numbers"]["bike_km"], 181.3)

    def test_key_mode_week_says_load_not_available(self):
        doc = hd.week_document({"start": MONDAY, "numbers": week(mode="key", acwr_status=None, acwr=None)})
        self.assertIn("Load not available.", doc["page_content"])

    def test_day_text(self):
        doc = hd.day_document({"date": dt.date(2026, 3, 18), "numbers": day(), "final": True, "empty": False},
                              prev=day(sleep_seconds=5 * 3600))
        text = doc["page_content"]
        self.assertTrue(text.startswith("Day 2026-03-18, Wednesday. Level red."))
        self.assertIn("sleep_debt", text)
        self.assertIn("HRV status low.", text)
        self.assertIn("Training: run 1 session, bike 1 session.", text)
        text.encode("ascii")
        self.assertEqual(doc["metadata"]["level"], "red")
        self.assertIsNone(hd.day_document({"date": dt.date(2026, 3, 18), "numbers": day(), "empty": True}))


class Tags(unittest.TestCase):
    def test_week_tags(self):
        tags = hd.week_tags(week())
        for tag in ("bike_heavy", "hrv_down", "sleep_debt", "recovery_low"):
            self.assertIn(tag, tags)
        self.assertNotIn("run_heavy", tags)
        self.assertNotIn("gap", tags)
        self.assertEqual(tags, [t for t in hd.WEEK_TAGS if t in tags])   # fixed order

    def test_taper_rest_gap_drop(self):
        before = [week(total_hours=10.0), week(total_hours=12.0)]
        taper = hd.week_tags(week(total_hours=5.0, level="green", flags=[], change_pct=-50.0, prev_total_hours=10.0,
                                  bike_hours=1.0, nights_under_6h=0), before)
        self.assertIn("taper", taper)
        self.assertIn("volume_drop", taper)
        rest = hd.week_tags(week(total_hours=1.0, sleep_nights=2, days_with_data=3, flags=[], level="green"))
        self.assertIn("rest_week", rest)
        self.assertIn("gap", rest)
        self.assertNotIn("taper", hd.week_tags(week(total_hours=5.0), before[:1]))

    def test_day_tags(self):
        tags = hd.day_tags(day(), ["short_sleep"])
        for tag in ("short_sleep", "high_stress", "double_day", "run_day", "bike_day", "long_session"):
            self.assertIn(tag, tags)
        self.assertIn("rest_day", hd.day_tags(day(sessions=0, run_sessions=0, bike_sessions=0), []))
        self.assertIn("partial", hd.day_tags(day(missing=["stats"]), []))


class Metadata(unittest.TestCase):
    def test_ids_are_stable_uuids(self):
        a = hd.doc_id("week", MONDAY)
        self.assertEqual(a, hd.doc_id("week", "2026-03-16"))
        self.assertNotEqual(a, hd.doc_id("day", MONDAY))
        uuid.UUID(a)

    def test_metadata_is_json_and_allowlisted(self):
        doc = hd.week_document({"start": MONDAY, "numbers": week(), "final": False})
        meta = doc["metadata"]
        json.dumps(meta)
        self.assertEqual((meta["kind"], meta["start"], meta["end"], meta["final"]),
                         ("week", "2026-03-16", "2026-03-22", False))
        self.assertEqual(meta["sports"], ["run", "bike"])
        self.assertEqual(meta["text_version"], hd.TEXT_VERSION)
        self.assertTrue(set(meta["numbers"]) <= set(hx.WEEK_FIELDS))
        self.assertTrue(set(meta["tags"]) <= set(hd.WEEK_TAGS))


class FromStore(unittest.TestCase):
    def test_build_document_reads_neighbours(self):
        s = hs.MemoryHistoryStore()
        d = dt.date(2026, 3, 18)
        s.upsert_day(FP, d - dt.timedelta(days=1), day(sleep_seconds=5 * 3600), True)
        s.upsert_day(FP, d, day(), True)
        doc = hd.build_document(s, FP, "day", d)
        self.assertIn("sleep_debt", doc["metadata"]["flags"])
        self.assertIsNone(hd.build_document(s, FP, "day", d + dt.timedelta(days=1)))
        s.upsert_week(FP, MONDAY - dt.timedelta(days=14), week(total_hours=10.0), True)
        s.upsert_week(FP, MONDAY - dt.timedelta(days=7), week(total_hours=12.0), True)
        s.upsert_week(FP, MONDAY, week(total_hours=5.0, level="green", flags=[], bike_hours=1.0), True)
        self.assertIn("taper", hd.build_document(s, FP, "week", MONDAY)["metadata"]["tags"])
        with self.assertRaises(ValueError):
            hd.build_document(s, FP, "month", MONDAY)

    @unittest.skipUnless(importlib.util.find_spec("langchain_core"), "needs langchain_core")
    def test_to_langchain(self):
        doc = hd.week_document({"start": MONDAY, "numbers": week()})
        lc = hd.to_langchain(doc)
        self.assertEqual((lc.id, lc.page_content, lc.metadata["kind"]), (doc["id"], doc["page_content"], "week"))


if __name__ == "__main__":
    unittest.main()
