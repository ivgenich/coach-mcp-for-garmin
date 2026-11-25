"""The history-search tools: find_similar_weeks and search_history (HISTORY_SEARCH=on, ROLE=app).

Both read only Postgres (the stored day and week numbers of history_store, the documents in
history.doc_vectors) and never call Garmin, so they are fast and cannot trip Garmin's rate limit.
Retrieval is history_retrieval's LangChain composition; this module validates the arguments, picks the
reference week, builds the answer and keeps every text fixed.

Answer (compact JSON): "index" (from, to, weeks, weeks_to, days, complete, stale, mode), the "reference" week for
find_similar_weeks, and "results": each with its dates, the stored numbers, a code-built "why", what
"followed_by" in the next 1-2 weeks (days: the next 3 days), "matched_on" (numbers, keywords, meaning) and
what is "missing". Every number is a stored value computed by the server's rules; the descriptions tell
the model to report them with their dates and not to compute new ones. Answers go through
text_hygiene.clean_tool_answer like upstream's.

"No data: ..." texts (not errors) for: an empty or still-filling index, a different Garmin account, a date
outside the index, fewer than 8 usable weeks, a database that does not answer. Bad arguments answer
tool_errors' bad-input text. Nothing logs the query, a date asked for or a value.

Standard library at import time; history_store, history_docs and history_retrieval are imported when the
tools run (their LangChain and Postgres packages only with HISTORY=1 builds).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import text_hygiene
import tool_errors

log = logging.getLogger("garmin_ext.history")

TOOL_NAMES = ("find_similar_weeks", "search_history")
MAX_K = 10
DEFAULT_K = 5
STALE_DAYS = 3
FOLLOW_WEEKS = 2
FOLLOW_DAYS = 3
HISTORY_START = dt.date(2000, 1, 1)
# Channels per tool, set from the retrieval ablation (docs/history-search.md, evals/history).
SIMILAR_CHANNELS = ("numbers",)
SEARCH_CHANNELS = ("vector",)
FALLBACK_CHANNELS = ("keyword",)  # when the local model cannot load

WEEK_FLAG_IDS = ("hrv_down", "short_nights", "volume_jump", "load_high")
DAY_FLAG_IDS = ("low_hrv", "hrv_low_trend", "high_rhr", "short_sleep", "sleep_debt")
KEY_MODE_ABSENT = ("load_high", "load_low")
WEEK_NUMBERS = ("hrv_avg_7d", "hrv_delta_ms", "hrv_status", "sleep_avg_h", "nights_under_6h", "sleep_avg_score",
                "rhr_avg", "stress_avg", "body_battery_high_avg", "total_hours", "change_pct", "sessions",
                "run_hours", "bike_hours", "swim_hours", "other_hours", "acwr", "acwr_status", "load_acute")
DAY_NUMBERS = ("sleep_score", "hrv", "hrv_7d", "hrv_status", "rhr", "rhr_7d", "stress_avg", "body_battery_high",
               "body_battery_low", "sessions")
WEEK_NOT_STORED = ("plan", "gear", "long_session")

NOTE = ("Numbers are stored values computed by the server's rules. Cite them with their dates; do not compute "
        "new ones or fill gaps. similarity ranks results, it is not a percentage.")
TEXT_EMPTY = ("No data: the history index is empty so far. Indexing runs in the background at a slow, safe pace "
              "and fills about a year of history over several days; try again later. get_weekly_status with an "
              "end_date still reads any past week from Garmin.")
TEXT_MISMATCH = ("No data: the stored history belongs to a different Garmin account, so it is not used. The "
                 "server owner can delete it by setting HISTORY_FORGET=yes once.")
TEXT_DB = ("No data: the history database did not answer; try again in a few minutes. Live tools such as "
           "get_weekly_status still work.")
TEXT_MIN_WEEKS = ("No data: similar-week search needs at least 8 indexed weeks with enough numbers; {n} so far. "
                  "get_weekly_status compares any single week with the week before it.")
TEXT_OUTSIDE = ("No data: {day} is not in the history index (it covers {start} to {end}). get_weekly_status with "
                "that end_date reads it from Garmin.")
TEXT_NOTHING = "No data: nothing in the stored history matches this search ({n} {kind}s searched)."

_COMMON = (
    "Searches the server's stored history of daily and weekly numbers. Does not call Garmin. Every number is "
    "a stored value computed by the server's rules: report them with their dates, do not compute new ones and "
    "do not fill gaps. The server has no record of how the athlete felt; 'tired' is judged from sleep, HRV, "
    "resting heart rate and stress, so say so. If index.complete is false or index.stale is true, say the "
    "history is partial or out of date. "
)

FIND_DESCRIPTION = _COMMON + (
    "Finds past weeks (Monday to Sunday) whose numbers are most like a reference week: HRV, sleep, resting "
    "heart rate, stress, training hours per sport, load. Use it for 'weeks like this one' and 'when was I last "
    "in this state'. Each result has why (the closest numbers, result vs reference) and followed_by (the next "
    "1-2 weeks), so you can say what happened after similar weeks. The 2 weeks around the reference are "
    "never returned.\n\n"
    "Args:\n"
    "    end_date: A day in the reference week, YYYY-MM-DD. Empty = the last complete week.\n"
    "    focus: overall (default), recovery (sleep, HRV, resting HR, stress) or training (hours, sports, load).\n"
    "    k: Number of weeks, 1-10 (default 5).\n"
    "    date_from: Only weeks starting on or after this date (YYYY-MM-DD), optional.\n"
    "    date_to: Only weeks starting on or before this date (YYYY-MM-DD), optional."
)

SEARCH_DESCRIPTION = _COMMON + (
    "Finds past weeks or days by words and by flags: 'a heavy bike block', 'a taper', 'weeks with short "
    "nights', 'when did HRV last drop'. Write query in English (translate the user's words). For 'when did "
    "I last ...' use order=newest with flags. Each result has why, followed_by and matched_on.\n\n"
    "Args:\n"
    "    query: English words, optional when flags are given.\n"
    "    kind: week (default) or day.\n"
    "    flags: Comma-separated flag or tag ids the result must have. Week: hrv_down, short_nights, "
    "volume_jump, load_high, run_heavy, bike_heavy, swim_heavy, volume_drop, taper, rest_week, no_training, "
    "load_low, hrv_low, sleep_debt, sleep_good, high_stress, recovery_low, gap. Day: low_hrv, hrv_low_trend, "
    "high_rhr, short_sleep, sleep_debt, long_sleep, high_stress, rest_day, run_day, bike_day, swim_day, "
    "long_session, double_day.\n"
    "    date_from, date_to: Optional range, YYYY-MM-DD.\n"
    "    order: relevance (default) or newest.\n"
    "    k: Number of results, 1-10 (default 5)."
)


class BadInput(ValueError):
    """A bad argument; str() is the detail for tool_errors' bad-input text (fixed words and code ids)."""


def bad_input_text(detail: str) -> str:
    return tool_errors.client_text(tool_errors.BAD_INPUT, ref=None, detail=detail, today=None, key_mode=False)


def _parse_date(value: Any, name: str) -> Optional[dt.date]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise BadInput(f"{name} must be a date written YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        raise BadInput(f"{name} must be a real calendar date written YYYY-MM-DD") from None


def _parse_k(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            raise BadInput("k must be a whole number from 1 to 10") from None
    if not 1 <= value <= MAX_K:
        raise BadInput("k must be a whole number from 1 to 10")
    return value


def _choice(value: Any, name: str, allowed: Sequence[str], default: str) -> str:
    text = (value if isinstance(value, str) else "").strip().lower() or default
    if text not in allowed:
        raise BadInput(f"{name} must be one of: {', '.join(allowed)}")
    return text


def known_ids(kind: str) -> Tuple[str, ...]:
    import history_docs

    if kind == "week":
        return tuple(dict.fromkeys(WEEK_FLAG_IDS + tuple(history_docs.WEEK_TAGS)))
    return tuple(dict.fromkeys(DAY_FLAG_IDS + tuple(t for t in history_docs.DAY_TAGS if t != "partial"
                                                    and t != "other_day")))


def parse_flags(value: Any, kind: str, key_mode: bool) -> List[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, str):
        raise BadInput("flags must be comma-separated ids")
    known = known_ids(kind)
    out = []
    for part in value.split(","):
        fid = part.strip().lower()
        if not fid:
            continue
        if fid not in known:
            # Only the code's own ids are echoed, never the caller's text.
            raise BadInput(f"unknown {kind} flag; the known ids are listed in the tool description")
        if key_mode and fid in KEY_MODE_ABSENT:
            raise BadInput(f"{fid} is not available with a Garmin Personal API key")
        if fid not in out:
            out.append(fid)
    return out


def _monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    return str(value)[:10]


def _d(value: Any) -> dt.date:
    return value if isinstance(value, dt.date) and not isinstance(value, dt.datetime) else \
        dt.date.fromisoformat(str(value)[:10])


def _round(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 1)
    return v


def _pick(numbers: Mapping[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    return {k: _round(numbers[k]) for k in keys if numbers.get(k) is not None}


def _day_numbers(n: Mapping[str, Any]) -> Dict[str, Any]:
    out = _pick(n, DAY_NUMBERS)
    if n.get("sleep_seconds") is not None:
        out["sleep_h"] = round(n["sleep_seconds"] / 3600.0, 1)
    for sport in ("run", "bike", "swim", "other"):
        secs = n.get(f"{sport}_seconds")
        if secs:
            out[f"{sport}_min"] = int(round(secs / 60.0))
    return out


class HistoryService:
    """The tools' logic over a store and a document index (both injectable for tests and evals).

    store: a history_store.HistoryStore. index_for(account_fp): a history_retrieval document index
    (PgDocIndex or MemoryDocIndex). today(): the local date (REPORT_TZ). cross_encoder: for the optional
    reranker (history_retrieval.RERANK)."""

    def __init__(self, store: Any, index_for: Callable[[str], Any], *, today: Callable[[], dt.date],
                 key_mode: bool = False, cross_encoder: Any = None,
                 similar_channels: Sequence[str] = SIMILAR_CHANNELS,
                 search_channels: Sequence[str] = SEARCH_CHANNELS, rerank: Optional[bool] = None) -> None:
        self.store = store
        self.index_for = index_for
        self.today = today
        self.key_mode = key_mode
        self.cross_encoder = cross_encoder
        self.similar_channels = tuple(similar_channels)
        self.search_channels = tuple(search_channels)
        self.rerank = rerank

    # -- shared

    def _account(self) -> Tuple[Optional[str], Optional[str]]:
        """(account_fp, None) or (None, a No data text)."""
        import history_store as hs

        if self.store.meta_get(hs.META_MISMATCH) == "yes":
            return None, TEXT_MISMATCH
        fp = self.store.meta_get(hs.META_ACCOUNT)
        if not fp:
            return None, TEXT_EMPTY
        return fp, None

    def index_block(self, fp: str, weeks: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
        """complete: every day from the index start to yesterday is stored, every one older than the last
        FINAL_AFTER_DAYS is final (a day whose source failed counts as missing), and the index reaches a year
        back or the backfill floor (the account start). Days stored before the floor (its probes) are left out.

        weeks_to: the end (Sunday) of the newest final stored week, or None; from `weeks` (the stored week
        rows, read here when not given), since the store's coverage also counts weeks that are not final.
        stale: the newest stored day is more than STALE_DAYS before yesterday, or the newest final week ends
        before expected_week_end() (a week is built once its 7 days are final)."""
        import history_extract as hx
        import history_store as hs

        cov = self.store.coverage(fp)
        today = self.today()
        yesterday = today - dt.timedelta(days=1)
        start, end = cov.get("from"), cov.get("to")
        floor = self.store.meta_get(hs.META_BACKFILL_FLOOR)
        if start and floor and floor > start:
            start = floor
        complete = False
        if start and end:
            missing = self.store.missing_days(fp, _d(start), yesterday)
            settled = today - dt.timedelta(days=hx.FINAL_AFTER_DAYS)
            still_open = self.store.open_days(fp, _d(start), settled) if _d(start) <= settled else []
            reached = _d(start) <= today - dt.timedelta(days=364) or bool(floor)
            complete = not missing and not still_open and reached
        if weeks is None:
            weeks = self._weeks(fp)
        weeks_to = newest_final_week_end(weeks)
        stale = bool(end) and _d(end) < yesterday - dt.timedelta(days=STALE_DAYS)
        if weeks_to is not None and _d(weeks_to) < expected_week_end(today):
            stale = True
        block = {"from": start, "to": end, "weeks": cov.get("final_weeks", cov.get("weeks", 0)),
                 "weeks_to": weeks_to, "days": cov.get("days_with_data", cov.get("days", 0)),
                 "complete": complete, "stale": stale}
        modes = self.store.meta_get("index_mode")
        if modes:
            block["mode"] = modes
        return block

    def _weeks(self, fp: str) -> List[Dict[str, Any]]:
        return self.store.weeks(fp, HISTORY_START, self.today() + dt.timedelta(days=7))

    def _days(self, fp: str, a: dt.date, b: dt.date) -> List[Dict[str, Any]]:
        return self.store.days(fp, a, b)

    def _channels(self, index: Any, wanted: Sequence[str]) -> Tuple[str, ...]:
        """Without the local model (download back-off, no runtime) the vector channel is dropped; when that
        leaves nothing, keyword search runs instead."""
        out = self._model_channels(index, wanted)
        return out or tuple(FALLBACK_CHANNELS)

    def _model_channels(self, index: Any, wanted: Sequence[str]) -> Tuple[str, ...]:
        if "vector" not in wanted:
            return tuple(wanted)
        emb = getattr(index, "embeddings", None)
        load = getattr(emb, "load", None)
        if emb is None:
            return tuple(c for c in wanted if c != "vector")
        if callable(load):
            import history_embed

            try:
                load()
            except history_embed.ModelUnavailable:
                return tuple(c for c in wanted if c != "vector")
        return tuple(wanted)

    @staticmethod
    def _week_docs(weeks: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        import history_docs

        out: Dict[str, Dict[str, Any]] = {}
        ordered = sorted(weeks, key=lambda r: _d(r["start"]))
        for i, row in enumerate(ordered):
            before = [r["numbers"] for r in reversed(ordered[max(0, i - 4):i])]
            out[_iso(row["start"])] = history_docs.week_document(
                {**row, "start": _d(row["start"])}, before)
        return out

    @staticmethod
    def _day_docs(days: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        import history_docs

        by_day = {_d(r["date"]): r for r in days}
        out: Dict[str, Dict[str, Any]] = {}
        for day, row in sorted(by_day.items()):
            p1 = by_day.get(day - dt.timedelta(days=1))
            p2 = by_day.get(day - dt.timedelta(days=2))
            doc = history_docs.day_document({**row, "date": day}, p1 and p1["numbers"], p2 and p2["numbers"])
            if doc is not None:
                out[day.isoformat()] = doc
        return out

    def _week_result(self, rank: int, row: Mapping[str, Any], doc: Mapping[str, Any],
                     weeks_by_start: Mapping[str, Mapping[str, Any]], docs: Mapping[str, Mapping[str, Any]],
                     meta: Mapping[str, Any]) -> Dict[str, Any]:
        start = _d(row["start"])
        n = row["numbers"]
        md = doc["metadata"]
        follow = []
        for i in range(1, FOLLOW_WEEKS + 1):
            nxt = (start + dt.timedelta(days=7 * i)).isoformat()
            if nxt in weeks_by_start:
                nn = weeks_by_start[nxt]["numbers"]
                follow.append({"start": nxt, "level": nn.get("level"), "flags": list(nn.get("flags") or []),
                               "tags": list(docs.get(nxt, {}).get("metadata", {}).get("tags", [])),
                               "numbers": _pick(nn, ("hrv_delta_ms", "sleep_avg_h", "total_hours"))})
        missing = sorted(set(md.get("missing") or []) | set(WEEK_NOT_STORED))
        if len(follow) < FOLLOW_WEEKS:
            missing.append("followed_by: later weeks not indexed")
        result = {"rank": rank, "kind": "week", "start": start.isoformat(),
                  "end": (start + dt.timedelta(days=6)).isoformat(), "level": n.get("level"),
                  "flags": list(n.get("flags") or []), "tags": list(md.get("tags") or []),
                  "matched_on": [_CHANNEL_WORD[c] for c in meta.get("matched_on", []) if c in _CHANNEL_WORD],
                  "numbers": _pick(n, WEEK_NUMBERS), "why": list(meta.get("why") or []),
                  "followed_by": follow, "missing": missing, "final": bool(row.get("final"))}
        if meta.get("similarity") is not None:
            result["similarity"] = meta["similarity"]
        if self.key_mode or n.get("mode") == "key":
            result["mode"] = n.get("mode") or "key"
        return result

    def _day_result(self, rank: int, row: Mapping[str, Any], doc: Mapping[str, Any],
                    days_by_date: Mapping[str, Mapping[str, Any]], docs: Mapping[str, Mapping[str, Any]],
                    meta: Mapping[str, Any]) -> Dict[str, Any]:
        day = _d(row["date"])
        md = doc["metadata"]
        follow = []
        for i in range(1, FOLLOW_DAYS + 1):
            nxt = (day + dt.timedelta(days=i)).isoformat()
            if nxt in docs:
                fm = docs[nxt]["metadata"]
                follow.append({"date": nxt, "level": fm.get("level"), "flags": list(fm.get("flags") or [])})
        return {"rank": rank, "kind": "day", "start": day.isoformat(), "end": day.isoformat(),
                "level": md.get("level"), "flags": list(md.get("flags") or []), "tags": list(md.get("tags") or []),
                "matched_on": [_CHANNEL_WORD[c] for c in meta.get("matched_on", []) if c in _CHANNEL_WORD],
                "numbers": _day_numbers(row["numbers"]), "why": list(meta.get("why") or []),
                "followed_by": follow, "missing": sorted(md.get("missing") or []), "final": bool(row.get("final"))}

    @staticmethod
    def _answer(obj: Dict[str, Any]) -> str:
        text = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
        return text_hygiene.clean_tool_answer(text)

    # -- tools

    def find_similar_weeks(self, end_date: str = "", focus: str = "overall", k: Any = DEFAULT_K,
                           date_from: str = "", date_to: str = "") -> str:
        import history_retrieval as hr

        try:
            ref_day = _parse_date(end_date, "end_date")
            focus = _choice(focus, "focus", hr.FOCUSES, "overall")
            k = _parse_k(k)
            lo, hi = _parse_date(date_from, "date_from"), _parse_date(date_to, "date_to")
            if lo and hi and hi < lo:
                raise BadInput("date_to must be on or after date_from")
        except BadInput as exc:
            return bad_input_text(str(exc))
        try:
            return self._find_similar_weeks(ref_day, focus, k, lo, hi)
        except _db_errors() as exc:  # every database read: a fixed text; anything else is a bug for launch
            return self._db_failure(exc)

    def _find_similar_weeks(self, ref_day: Optional[dt.date], focus: str, k: int, lo: Optional[dt.date],
                            hi: Optional[dt.date]) -> str:
        import history_retrieval as hr

        fp, text = self._account()
        if text:
            return text
        weeks = self._weeks(fp)
        if not weeks:
            return TEXT_EMPTY
        by_start = {_iso(w["start"]): w for w in weeks}
        index = self.index_block(fp, weeks)
        note = None
        if ref_day is not None:
            ref_start = _monday(ref_day).isoformat()
            if ref_start not in by_start:
                return TEXT_OUTSIDE.format(day=ref_day.isoformat(), start=index["from"], end=index["to"])
        else:
            ref_start = (_monday(self.today()) - dt.timedelta(days=7)).isoformat()
            if ref_start not in by_start:
                note = not_indexed_note(_d(ref_start) + dt.timedelta(days=6))
                ref_start = max(by_start)
        reference = by_start[ref_start]
        docs = self._week_docs(weeks)
        req_index = self.index_for(fp)
        channels = self._channels(req_index, self.similar_channels)
        req = hr.SearchRequest(kind="week", text=docs[ref_start]["page_content"] if channels != ("numbers",) else "",
                               reference={**reference, "start": ref_start}, focus=focus,
                               date_from=lo.isoformat() if lo else "0001-01-01",
                               date_to=hi.isoformat() if hi else "9999-12-31", k=k, channels=channels,
                               rerank=hr.RERANK if self.rerank is None else self.rerank)
        try:
            found, used = hr.search(req_index, weeks, req, self.cross_encoder)
        except hr.NotEnoughHistory as exc:
            return TEXT_MIN_WEEKS.format(n=exc.usable)
        except _db_errors() as exc:  # a fixed text, no detail; anything else is a bug for launch's wrapper
            return self._db_failure(exc)
        results = []
        for d in found:
            start = d.metadata["start"]
            if start in by_start and start in docs:
                results.append(self._week_result(len(results) + 1, by_start[start], docs[start], by_start, docs,
                                                 d.metadata))
        _metric("find_similar_weeks", "ok" if results else "empty", used)
        ref_n = reference["numbers"]
        answer = {"kind": "similar_weeks", "index": index,
                  "reference": {"start": ref_start, "end": (_d(ref_start) + dt.timedelta(days=6)).isoformat(),
                                "level": ref_n.get("level"), "flags": list(ref_n.get("flags") or []),
                                "numbers": _pick(ref_n, WEEK_NUMBERS), "final": bool(reference.get("final"))},
                  "focus": focus, "results": results, "note": NOTE}
        if note:
            answer["reference"]["note"] = note
        if not results:
            answer["note"] = "No other stored week shares enough numbers with the reference. " + NOTE
        return self._answer(answer)

    def search_history(self, query: str = "", kind: str = "week", flags: str = "", date_from: str = "",
                       date_to: str = "", order: str = "relevance", k: Any = DEFAULT_K) -> str:
        try:
            kind = _choice(kind, "kind", ("week", "day"), "week")
            order = _choice(order, "order", ("relevance", "newest"), "relevance")
            k = _parse_k(k)
            lo, hi = _parse_date(date_from, "date_from"), _parse_date(date_to, "date_to")
            if lo and hi and hi < lo:
                raise BadInput("date_to must be on or after date_from")
            wanted = parse_flags(flags, kind, self.key_mode)
            if query is not None and not isinstance(query, str):
                raise BadInput("query must be text")
            words = (query or "").strip()
            if not words and not wanted:
                raise BadInput("give query words or flags")
        except BadInput as exc:
            return bad_input_text(str(exc))
        try:
            return self._search_history(words, kind, wanted, lo, hi, order, k)
        except _db_errors() as exc:  # every database read: a fixed text; anything else is a bug for launch
            return self._db_failure(exc)

    def _search_history(self, words: str, kind: str, wanted: List[str], lo: Optional[dt.date],
                        hi: Optional[dt.date], order: str, k: int) -> str:
        import history_retrieval as hr

        fp, text = self._account()
        if text:
            return text
        weeks = self._weeks(fp)
        if not weeks and kind == "week":
            return TEXT_EMPTY
        index = self.index_block(fp, weeks)
        if kind == "week":
            rows = {_iso(w["start"]): w for w in weeks}
            docs = self._week_docs(weeks)
        else:
            a = lo or HISTORY_START
            b = hi or self.today()
            day_rows = self._days(fp, a - dt.timedelta(days=2), b + dt.timedelta(days=FOLLOW_DAYS))
            if not day_rows:
                # Days exist outside the asked range (weeks were found above, or the index has a range).
                return TEXT_NOTHING.format(n=0, kind=kind) if (weeks or index.get("from")) else TEXT_EMPTY
            rows = {_iso(r["date"]): r for r in day_rows}
            docs = self._day_docs(day_rows)
        lo_s, hi_s = (lo.isoformat() if lo else "0001-01-01"), (hi.isoformat() if hi else "9999-12-31")

        def keep(start: str) -> bool:
            if not lo_s <= start <= hi_s or start not in docs:
                return False
            md = docs[start]["metadata"]
            have = set(md.get("flags") or []) | set(md.get("tags") or [])
            return all(f in have for f in wanted)

        metas: Dict[str, Dict[str, Any]] = {}
        used: List[str] = []
        if words:
            req_index = self.index_for(fp)
            channels = self._channels(req_index, self.search_channels)
            req = hr.SearchRequest(kind=kind, text=words, date_from=lo_s, date_to=hi_s, k=hr.FETCH_K,
                                   channels=channels, rerank=hr.RERANK if self.rerank is None else self.rerank)
            try:
                found, used = hr.search(req_index, weeks, req, self.cross_encoder)
            except _db_errors() as exc:
                return self._db_failure(exc)
            ranked = [d.metadata["start"] for d in found if keep(d.metadata["start"])]
            metas = {d.metadata["start"]: d.metadata for d in found}
        else:
            ranked = [s for s in docs if keep(s)]
        tokens = hr.query_tokens(words)
        if words and order == "newest":
            # Only results above a relevance floor are re-sorted by date: a common query word matches nearly
            # every document, and the newest of those would otherwise come first.
            contents = {s: docs[s].get("page_content", "") for s in docs if lo_s <= s <= hi_s}
            ranked = hr.relevance_floor(ranked, contents, tokens, k)
        if order == "newest" or not words:
            ranked = sorted(ranked, reverse=True)
        results = []
        for start in ranked[:k]:
            md = docs[start]["metadata"]
            meta = dict(metas.get(start, {}))
            if wanted and "flags" not in meta.get("matched_on", []):
                meta["matched_on"] = list(meta.get("matched_on", [])) + ["flags"]
            if kind == "week":
                result = self._week_result(len(results) + 1, rows[start], docs[start], rows, docs, meta)
            else:
                result = self._day_result(len(results) + 1, rows[start], docs[start], rows, docs, meta)
            result["why"] = _text_why(md, wanted, tokens, result["numbers"], result["matched_on"], kind)
            results.append(result)
        _metric("search_history", "ok" if results else "empty", used)
        if not results:
            n = sum(1 for s in docs if lo_s <= s <= hi_s)
            return TEXT_NOTHING.format(n=n, kind=kind)
        answer = {"kind": "history_search", "index": index, "search": {"kind": kind, "flags": wanted,
                                                                       "order": order, "words": len(tokens)},
                  "results": results, "note": NOTE}
        if words and not tokens:
            answer["search"]["note"] = "No English words in query: only the meaning channel ran."
        return self._answer(answer)

    @staticmethod
    def _db_failure(exc: BaseException) -> str:
        log.warning("history search: the database did not answer (%s)", type(exc).__name__)
        return TEXT_DB


def _db_errors() -> tuple:
    """The failures that mean "the database did not answer" (history_retrieval and history_store map driver
    errors to these, with fixed texts)."""
    import history_retrieval as hr
    import history_store as hs

    return (hr.HistoryUnavailable, hs.StoreError)


_CHANNEL_WORD = {"numbers": "numbers", "keyword": "keywords", "vector": "meaning", "flags": "flags"}


# The stored numbers each flag or tag is decided from (history_extract's flags, history_docs.TAG_RULES), so a
# reason in "why" names the values a user can check. A key the result's numbers lack is left out.
WHY_NUMBERS = {
    # weeks
    "hrv_down": ("hrv_delta_ms",), "hrv_low": ("hrv_avg_7d", "hrv_status"),
    "short_nights": ("nights_under_6h",), "sleep_good": ("sleep_avg_h",),
    "volume_jump": ("change_pct", "total_hours"), "volume_drop": ("change_pct", "total_hours"),
    "taper": ("total_hours", "change_pct"), "rest_week": ("total_hours",), "no_training": ("sessions",),
    "load_high": ("acwr", "acwr_status"), "load_low": ("acwr", "acwr_status"),
    "run_heavy": ("run_hours", "total_hours"), "bike_heavy": ("bike_hours", "total_hours"),
    "swim_heavy": ("swim_hours", "total_hours"), "recovery_low": ("body_battery_high_avg", "hrv_delta_ms"),
    "gap": ("sessions",),
    # weeks and days
    "sleep_debt": ("nights_under_6h", "sleep_avg_h", "sleep_h"), "high_stress": ("stress_avg",),
    # days
    "low_hrv": ("hrv", "hrv_7d"), "hrv_low_trend": ("hrv_7d", "hrv_status"), "high_rhr": ("rhr", "rhr_7d"),
    "short_sleep": ("sleep_h", "sleep_score"), "long_sleep": ("sleep_h",), "rest_day": ("sessions",),
    "double_day": ("sessions",), "run_day": ("run_min",), "bike_day": ("bike_min",), "swim_day": ("swim_min",),
    "other_day": ("other_min",), "long_session": ("run_min", "bike_min", "swim_min", "other_min"),
}
WHY_MAX = 4


def _why_item(tag: str, numbers: Mapping[str, Any]) -> str:
    """'has short_nights (nights_under_6h 5)': the flag or tag and the stored values it is decided from."""
    values = [f"{key} {numbers[key]}" for key in WHY_NUMBERS.get(tag, ()) if numbers.get(key) is not None]
    return f"has {tag} ({', '.join(values)})" if values else f"has {tag}"


# The main stored numbers named when a word or meaning match has no flag or tag to point at.
MAIN_NUMBERS = {
    "week": ("total_hours", "sleep_avg_h", "hrv_delta_ms", "sessions"),
    "day": ("sleep_h", "hrv", "rhr", "sessions"),
}


def _match_why(numbers: Mapping[str, Any], matched_on: Sequence[str], kind: str) -> str:
    """'text match (total_hours 5.0, sleep_avg_h 7.1, hrv_delta_ms -2.0)': how the result was found and its
    main stored numbers. Built from the stored numbers only, never from the query text."""
    how = [w for w in ("keywords", "meaning") if w in matched_on]
    label = {("keywords",): "text match", ("meaning",): "meaning match"}.get(tuple(how), "text and meaning match")
    keys = [key for key in MAIN_NUMBERS.get(kind, ()) if numbers.get(key) is not None]
    if not keys:
        keys = [key for key in numbers if numbers.get(key) is not None][:3]
    values = [f"{key} {numbers[key]}" for key in keys]
    return f"{label} ({', '.join(values)})" if values else f"{label} (no stored numbers)"


def _text_why(md: Mapping[str, Any], wanted: Sequence[str], tokens: Sequence[str],
              numbers: Optional[Mapping[str, Any]] = None, matched_on: Sequence[str] = (),
              kind: str = "week") -> List[str]:
    """Code-built reasons for a word or flag match: the flags asked for, then tags/flags sharing a query word;
    when neither applies (a meaning or keyword match on the text), the result's own flags and tags; with no
    flag or tag at all, how it matched and its main stored numbers. Each names the stored numbers behind it."""
    numbers = numbers or {}
    chosen = list(wanted)
    have = list(md.get("flags") or []) + [t for t in (md.get("tags") or []) if t not in (md.get("flags") or [])]
    for tag in have:
        parts = tag.split("_")
        if tag not in chosen and any(t[:4] == p[:4] for t in tokens for p in parts if len(t) >= 3 and len(p) >= 3):
            chosen.append(tag)
    if not chosen:
        chosen = have
    if not chosen:
        return [_match_why(numbers, matched_on, kind)]
    return [_why_item(tag, numbers) for tag in chosen[:WHY_MAX]]


def _metric(tool: str, outcome: str, channels: Sequence[str]) -> None:
    try:
        import telemetry
    except Exception:  # noqa: BLE001
        return
    attrs = {"coach.tool": tool, "coach.outcome": outcome}
    if channels:
        attrs["coach.history.channels"] = ",".join(channels)
    try:
        telemetry.metric_count("coach.history.searches", 1, attrs=attrs)
    except Exception:  # noqa: BLE001
        pass


# -- registration ----------------------------------------------------------------------------------------------

_SERVICE: Dict[str, Any] = {}
_SERVICE_LOCK = threading.Lock()


def newest_final_week_end(weeks: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The end date (ISO) of the newest final week in `weeks` (stored week rows), or None."""
    ends = [_d(w["end"]) if w.get("end") is not None else _d(w["start"]) + dt.timedelta(days=6)
            for w in weeks if w.get("final")]
    return max(ends).isoformat() if ends else None


def expected_week_end(today: dt.date) -> dt.date:
    """The newest Sunday whose week should be indexed by `today`: the last Sunday at least FINAL_AFTER_DAYS + 1
    days old (its days are final FINAL_AFTER_DAYS after their date; one more day for the worker to build it)."""
    import history_extract as hx

    latest = today - dt.timedelta(days=hx.FINAL_AFTER_DAYS + 1)
    return latest - dt.timedelta(days=(latest.weekday() + 1) % 7)


def not_indexed_note(week_end: dt.date) -> str:
    """The reference note when the last complete week (ending on Sunday `week_end`) is not indexed yet."""
    import history_extract as hx

    start = week_end - dt.timedelta(days=6)
    ready = week_end + dt.timedelta(days=hx.FINAL_AFTER_DAYS)
    return (f"The last complete week ({start.isoformat()} to {week_end.isoformat()}) is not indexed yet: a week "
            f"is indexed once all its days are final, {hx.FINAL_AFTER_DAYS} days after each day, so from about "
            f"{ready.isoformat()}. The reference is the newest stored week.")


def _today() -> dt.date:
    import status_common

    return status_common.today_local(status_common.zone())


def service_from_config(cfg: Any) -> HistoryService:
    """The app's HistoryService over Postgres (built once, on the first tool call)."""
    with _SERVICE_LOCK:
        if "svc" not in _SERVICE:
            import history_retrieval as hr
            import history_store as hs

            dsn = getattr(cfg, "database_url", None) or ""
            store = hs.PgHistoryStore(dsn)
            cross = None
            if hr.RERANK:
                import history_embed

                cross = history_embed.cross_encoder(history_embed.default_root())
            _SERVICE["svc"] = HistoryService(store, lambda fp: hr.doc_index(dsn, fp), today=_today,
                                             key_mode=getattr(cfg, "garmin_auth", "full") == "key",
                                             cross_encoder=cross)
        return _SERVICE["svc"]


def register(adder: Callable[..., Any], cfg: Any, service: Optional[HistoryService] = None) -> None:
    """Register both tools. adder(fn, name=..., description=...) is launch.py's; cfg its FeatureConfig.
    service replaces the Postgres one (tests)."""
    import anyio.to_thread

    import mcp2_compat

    def svc() -> HistoryService:
        return service if service is not None else service_from_config(cfg)

    @mcp2_compat.server_loop
    async def find_similar_weeks(end_date: str = "", focus: str = "overall", k: int = DEFAULT_K,
                                 date_from: str = "", date_to: str = "") -> str:
        return await anyio.to_thread.run_sync(
            lambda: svc().find_similar_weeks(end_date, focus, k, date_from, date_to))

    @mcp2_compat.server_loop
    async def search_history(query: str = "", kind: str = "week", flags: str = "", date_from: str = "",
                             date_to: str = "", order: str = "relevance", k: int = DEFAULT_K) -> str:
        return await anyio.to_thread.run_sync(
            lambda: svc().search_history(query, kind, flags, date_from, date_to, order, k))

    adder(find_similar_weeks, name="find_similar_weeks", description=FIND_DESCRIPTION)
    adder(search_history, name="search_history", description=SEARCH_DESCRIPTION)
