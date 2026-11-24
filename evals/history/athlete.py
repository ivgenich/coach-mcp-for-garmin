"""A seeded synthetic athlete: 78 weeks of stored day and week numbers with planted episodes.

The numbers go through the same code as real history: history_extract.week_numbers (the weekly rules and
flags) and history_store.MemoryHistoryStore (the allowlist), and the documents through history_docs. Only
the day numbers are made up here. Weeks 0-59 are full mode (Garmin training load present), weeks 60-77 key
mode (no load), like an owner who switched to a Personal API key.

Planted episodes (week index -> what changes):
- hrv_drop_1/2/3 (12, 38, 64): nightly HRV about 14 ms lower, Garmin status UNBALANCED, resting HR up,
  stress up, sleep a little shorter; the week after each is a low week (40% volume): after_drop_1/2/3.
- sleep_debt_1/2 (25, 55): 4-5 nights under 6 h, low sleep scores.
- load_spike_1/2 (33, 46): volume x1.8 and Garmin ACWR HIGH (full mode).
- bike_block_1 (20-22), bike_block_2 (50-51): most hours on the bike.
- swim_block (61-62): most hours in the pool (key mode).
- taper_1/2 (30, 70): volume about 45% of the weeks before.
- rest_week_1/2/3 (5, 43, 74): under 2 hours of training.
- travel_gap (58): 4 days with no data at all.
- high_stress_1/2 (16, 67): all-day stress about 55.
- short_sleep_days: single nights under 5 h in otherwise normal weeks (day queries).
Standard library only; the same seed gives the same bytes.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXT = os.path.join(REPO, "ext")
if EXT not in sys.path:
    sys.path.insert(0, EXT)

import history_extract as hx  # noqa: E402
import history_store as hs  # noqa: E402

SEED = 20261124
WEEKS = 78
LAST_WEEK_START = dt.date(2026, 11, 16)  # a Monday
TODAY = dt.date(2026, 11, 23)  # every day stored and final; the last complete week is LAST_WEEK_START
KEY_FROM_WEEK = 60
LEAD_IN_DAYS = 28
ACCOUNT_FP = "a1" * 32

EPISODES: Dict[str, List[int]] = {
    "hrv_drop_1": [12], "hrv_drop_2": [38], "hrv_drop_3": [64],
    "after_drop_1": [13], "after_drop_2": [39], "after_drop_3": [65],
    "sleep_debt_1": [25], "sleep_debt_2": [55],
    "load_spike_1": [33], "load_spike_2": [46],
    "bike_block_1": [20, 21, 22], "bike_block_2": [50, 51],
    "swim_block": [61, 62],
    "taper_1": [30], "taper_2": [70],
    "rest_week_1": [5], "rest_week_2": [43], "rest_week_3": [74],
    "travel_gap": [58],
    "high_stress_1": [16], "high_stress_2": [67],
}
SHORT_SLEEP_DAYS = (100, 260, 410)  # day offsets from week 0's Monday: single very short nights

# A normal week: (weekday, sport, minutes). Monday is rest.
PLAN = ((1, "run", 60), (2, "bike", 75), (3, "run", 45), (4, "swim", 45), (5, "bike", 150), (6, "run", 90))
SPEED_KMH = {"run": 10.0, "bike": 28.0, "swim": 2.5, "other": 6.0}


@dataclasses.dataclass
class Athlete:
    store: Any
    account_fp: str
    today: dt.date
    week0: dt.date
    episodes: Dict[str, List[str]]  # name -> week starts (ISO)
    short_sleep_days: List[str]

    def week_start(self, index: int) -> str:
        return (self.week0 + dt.timedelta(days=7 * index)).isoformat()


def _episode_of(week: int) -> Optional[str]:
    for name, weeks in EPISODES.items():
        if week in weeks:
            return name
    return None


def _week_plan(week: int, rng: random.Random) -> List[tuple]:
    """(weekday, sport, minutes) sessions of one week, episodes applied."""
    ep = _episode_of(week) or ""
    season = 1.0 + 0.15 * math.sin(2 * math.pi * week / 26.0)
    factor = season * rng.uniform(0.9, 1.1)
    plan = list(PLAN)
    if ep.startswith("bike_block"):
        plan = [(1, "bike", 90), (2, "bike", 120), (3, "run", 30), (4, "bike", 90), (5, "bike", 240),
                (6, "bike", 180)]
    elif ep == "swim_block":
        plan = [(1, "swim", 90), (2, "swim", 75), (3, "run", 30), (4, "swim", 90), (5, "swim", 60),
                (6, "swim", 90)]
    elif ep.startswith("load_spike"):
        factor *= 1.8
    elif ep.startswith("taper"):
        factor *= 0.45
    elif ep.startswith("after_drop"):
        factor *= 0.4
    elif ep.startswith("rest_week"):
        plan = [(2, "run", 30), (5, "bike", 45)]
    return [(wd, sport, max(10, int(round(minutes * factor * rng.uniform(0.85, 1.15))))) for wd, sport, minutes
            in plan]


def _day_numbers(day_index: int, week: int, weekday: int, sessions: List[tuple], mode: str, hrv_hist: List[int],
                 rhr_hist: List[int], rng: random.Random) -> Dict[str, Any]:
    ep = _episode_of(week) or ""
    hrv = rng.gauss(62, 4)
    rhr = rng.gauss(47, 1.5)
    sleep_h = rng.gauss(7.4, 0.45)
    score = rng.gauss(80, 5)
    stress = rng.gauss(27, 4)
    status = "BALANCED"
    if ep.startswith("hrv_drop"):
        hrv -= 14 + rng.uniform(0, 3)
        rhr += 5
        stress += 9
        sleep_h -= 0.4
        status = "UNBALANCED"
    elif ep.startswith("sleep_debt") and weekday in (0, 1, 2, 3, 4):
        sleep_h = rng.gauss(5.3, 0.25)
        score = rng.gauss(55, 5)
        hrv -= 4
    elif ep.startswith("high_stress"):
        stress = rng.gauss(55, 3)
        hrv -= 3
    elif ep.startswith("load_spike"):
        rhr += 2
        hrv -= 3
    if day_index in SHORT_SLEEP_DAYS:
        sleep_h, score = 4.6, 42
    hrv_i, rhr_i = int(round(hrv)), int(round(rhr))
    hrv_hist.append(hrv_i)
    rhr_hist.append(rhr_i)
    n: Dict[str, Any] = {
        "mode": mode,
        "sleep_seconds": int(round(max(3.0, sleep_h) * 3600)), "sleep_score": int(round(min(100, max(20, score)))),
        "deep_min": int(rng.gauss(85, 10)), "rem_min": int(rng.gauss(95, 12)), "awake_min": int(abs(rng.gauss(20, 8))),
        "hrv": hrv_i, "hrv_7d": int(round(sum(hrv_hist[-7:]) / len(hrv_hist[-7:]))), "hrv_status": status,
        "hrv_low_upper": 50, "hrv_balanced_low": 54, "hrv_balanced_upper": 70,
        "rhr": rhr_i, "rhr_7d": int(round(sum(rhr_hist[-7:]) / len(rhr_hist[-7:]))),
        "stress_avg": int(round(max(5, stress))), "body_battery_high": int(round(min(100, rng.gauss(86, 5)))),
        "body_battery_low": int(round(max(5, rng.gauss(24, 5)))),
        "missing": [],
    }
    total = 0
    for sport in hx.SPORTS:
        mins = [m for _wd, s, m in sessions if s == sport]
        n[f"{sport}_sessions"] = len(mins)
        n[f"{sport}_seconds"] = float(sum(mins) * 60)
        n[f"{sport}_meters"] = float(round(sum(mins) / 60.0 * SPEED_KMH[sport] * 1000))
        total += len(mins)
    n["sessions"] = total
    return n


def _empty_day(mode: str) -> Dict[str, Any]:
    return {"mode": mode, "missing": ["stats", "sleep", "hrv", "activities"]}


def build(seed: int = SEED, store: Any = None) -> Athlete:
    """Generate the athlete into `store` (a new MemoryHistoryStore by default) and return it."""
    rng = random.Random(seed)
    store = store if store is not None else hs.MemoryHistoryStore()
    week0 = LAST_WEEK_START - dt.timedelta(days=7 * (WEEKS - 1))
    first_day = week0 - dt.timedelta(days=LEAD_IN_DAYS)
    days: Dict[dt.date, Dict[str, Any]] = {}
    hrv_hist: List[int] = []
    rhr_hist: List[int] = []
    day = first_day
    week_plans: Dict[int, List[tuple]] = {}
    while day <= LAST_WEEK_START + dt.timedelta(days=6):
        week = (day - week0).days // 7
        mode = "key" if week >= KEY_FROM_WEEK else "full"
        if week not in week_plans:
            week_plans[week] = _week_plan(week, rng)
        sessions = [s for s in week_plans[week] if s[0] == day.weekday()]
        if _episode_of(week) == "travel_gap" and day.weekday() in (1, 2, 3, 4):
            days[day] = _empty_day(mode)
        else:
            days[day] = _day_numbers((day - week0).days, week, day.weekday(), sessions, mode, hrv_hist, rhr_hist,
                                     rng)
        day += dt.timedelta(days=1)
    for d, n in days.items():
        store.upsert_day(ACCOUNT_FP, d, n, True)
    chronic = None
    for w in range(WEEKS):
        start = week0 + dt.timedelta(days=7 * w)
        mode = "key" if w >= KEY_FROM_WEEK else "full"
        load = None
        hours = sum(m for _wd, _s, m in week_plans[w]) / 60.0
        acute = round(hours * 70.0, 1)
        prev_chronic = chronic
        chronic = acute if chronic is None else round(0.75 * chronic + 0.25 * acute, 1)
        if mode == "full":
            ratio = round(acute / chronic, 2) if chronic else None
            spike = (_episode_of(w) or "").startswith("load_spike")
            status = "HIGH" if spike or (ratio and ratio > 1.5) else ("LOW" if ratio and ratio < 0.8 else "OPTIMAL")
            load = {"load_acute": acute, "load_chronic": chronic, "acwr": ratio, "acwr_status": status,
                    "load_optimal_min": round(chronic * 0.8, 1), "load_optimal_max": round(chronic * 1.4, 1),
                    "vo2max_run": 54.0, "vo2max_bike": 52.0, "training_status": "PRODUCTIVE",
                    "training_balance": "BALANCED"}
        numbers = hx.week_numbers(start, days, mode=mode, load=load,
                                  chronic_week_ago=prev_chronic if mode == "full" else None)
        store.upsert_week(ACCOUNT_FP, start, numbers, True)
    store.meta_set(hs.META_ACCOUNT, ACCOUNT_FP)
    store.meta_set(hs.META_BACKFILL_FLOOR, first_day.isoformat())
    episodes = {name: [(week0 + dt.timedelta(days=7 * w)).isoformat() for w in weeks]
                for name, weeks in EPISODES.items()}
    shorts = [(week0 + dt.timedelta(days=i)).isoformat() for i in SHORT_SLEEP_DAYS]
    return Athlete(store, ACCOUNT_FP, TODAY, week0, episodes, shorts)


def documents(athlete: Athlete, kinds: tuple = ("week", "day")) -> List[Any]:
    """The LangChain Documents of every stored week and day (history_docs), oldest first."""
    import history_docs

    out = []
    fp = athlete.account_fp
    hi = athlete.today
    if "week" in kinds:
        for row in athlete.store.weeks(fp, athlete.week0, hi):
            out.append(history_docs.to_langchain(history_docs.build_document(athlete.store, fp, "week",
                                                                              row["start"])))
    if "day" in kinds:
        for row in athlete.store.days(fp, athlete.week0, hi):
            doc = history_docs.build_document(athlete.store, fp, "day", row["date"])
            if doc is not None:
                out.append(history_docs.to_langchain(doc))
    return out
