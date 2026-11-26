"""The no-invented-numbers check: every number in the answer must come from a tool result of the same trial.

answer_numbers() reads the numbers in an answer: decimals, "1,234", signed values ("-5", "+3"), percents,
ranges ("5-8 %" is 5 and 8, "between -5 and -8" is -5 and -8), durations ("6:15", "1:02:30", "6 h 15 min"),
paces ("5:30 /km") and a unit after a number (km, m, mi, h, min, s, ms, kg, lb, %, bpm, km/h, k). It skips
what is not a measured value: dates ("2026-03-18", "18 March", "March 1-7", "Wed 18"), times of day ("06:30",
"7 am", "at 6:30"), time-zone offsets, list markers ("1. "), a scale after a number ("52/100", "8 out of
10"), and digits inside words or ordinals ("VO2max", "Z2", "3rd", "0badc0de"). "6h45" is one duration.
Numbers written as words ("three") are not read.

tool_numbers() reads every number in the tool result texts (JSON or not, strings included), and every "h:mm"
there also as hours and minutes; the thresholds, thresholds_athlete and sources subtrees, time-stamp fields and
dates are left out (pool_text()). Exact simple arithmetic on JSON tool values is added too (derived_numbers():
sums and differences of same-unit values within one list or one object, list totals, differences of two
distances, start/end spans, and the pace of an activity from its distance and duration, also per 100 m);
ratios, percent changes, averages and date spans are not. A window length in a key name ("avg_28d") grounds
"28-day".

An answer number with no unit takes the unit of its markdown table column header ("| km |", "Pace (min/km)")
or a "km"/"mi" written before it ("km 7.7 to 14") or after the range it starts ("from 13.4 to 15.2 km"). General guidance about sleep, drink and fuel amounts ("aim for
7-8 hours", "the 7-9 hours most adults need", "7-9 hours usually recommended", "go to bed 30-45 minutes
earlier", "drink 500 ml") is not data about the athlete and is skipped.

check() matches each answer number against the tool numbers, allowing:
- rounding to the precision the answer shows (6.25 -> "6.3" or "6"; 87.5 -> "88 %"; 6.5 == 6.50);
- unit conversions selected by the answer's unit (m <-> km <-> mi, s <-> min <-> h, ms, cm -> km, kg <-> lb,
  m/s -> km/h and pace, a fraction -> percent);
- durations: "6:15" == 6.25 h == 375 min == 22,500 s; "5:30 /km" == 330 s/km or 3.03 m/s; a pace may also be
  cut to whole seconds (298.6 s/km as "4:58 /km");
- a positive answer number against a negative tool number ("5 below" for -5), never the reverse.
A unit word may follow a hyphen ("a 67-minute ride"); a hyphen after a letter is not a sign (Hebrew "ב-6");
Hebrew dates ("18–24 במאי 2026") and a season with its year ("autumn 2025", "בסתיו 2025") are skipped like
English dates.
Numbers in the user's prompt, and in the tool descriptions and schemas the model was given ("max_long_km 10
to 32"), are exempt. Whole numbers 0-10 with no unit that match nothing are reported as
"soft" (counts, list positions, "Zone 2") and do not fail the check (SOFT_MAX).

Standard library only. Pure functions, no I/O.
"""

import json
import math
import re
from dataclasses import dataclass

SOFT_MAX = 10  # unmatched whole numbers 0..SOFT_MAX without a unit are soft (reported, not failing)

_M = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
      r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_W = r"mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
# Hebrew month names, with the one-letter prefixes written onto them ("במאי": in May, "מאפריל": from April).
_HM = (r"(?:[בלמוה]|עד\s)?(?:ינואר|פברואר|מרץ|מרס|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|"
       r"דצמבר)")
_ORD = r"(?:st|nd|rd|th)?"
_TO = r"\s*(?:-|–|—|to|and|through|until|till)\s*"
_I = re.IGNORECASE

# Spans removed before numbers are read (dates, times of day, offsets, list markers), in this order.
_EXEMPT = [re.compile(p, _I | re.M) for p in (
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b",
    r"(?<![\d.])\d{1,2}/\d{1,2}/\d{2,4}(?!\d)(?!\.\d)",
    rf"\b\d{{1,2}}{_ORD}{_TO}\d{{1,2}}{_ORD}\s+(?:of\s+)?(?:{_M})\b\.?(?:,?\s+\d{{4}})?",
    rf"\b(?:{_M})\.?\s+\d{{1,2}}{_ORD}{_TO}\d{{1,2}}{_ORD}\b(?:,?\s+\d{{4}})?",
    rf"\b(?:{_M})\.?\s+\d{{1,2}}{_ORD}\b(?:,?\s+\d{{4}})?",
    rf"\b\d{{1,2}}{_ORD}\s+(?:of\s+)?(?:{_M})\b\.?(?:,?\s+\d{{4}})?",
    rf"\b(?:{_M})\.?\s+\d{{4}}\b",
    # Hebrew dates (live run 20261125T200810Z, H07): "18–24 במאי 2026", "27 באוקטובר 2025", "מאפריל 2025".
    rf"(?<![\d.])\d{{1,2}}(?:{_TO}\d{{1,2}})?\s*{_HM}(?![א-ת])(?:,?\s+\d{{4}})?",
    rf"{_HM}(?![א-ת])\s+\d{{4}}\b",
    # A season and year: "autumn 2025", "בסתיו 2025" (in autumn 2025).
    r"(?:\b(?:spring|summer|autumn|fall|winter)|(?<![א-ת])[בלמוה]?(?:אביב|קיץ|סתיו|חורף))\s+(?:of\s+)?"
    r"(?:19|20)\d\d\b",
    rf"\b(?:{_W})\b\.?,?\s+(?:the\s+)?(?:[1-9]|[12]\d|3[01]){_ORD}\b(?![.,]\d)(?!\s?[%/a-z])",
    r"\b(?:utc|gmt)\s*[+-]\s*\d{1,2}(?::?\d{2})?\b",
    r"(?<![\d:])\d{1,2}(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)(?![a-z])",
    r"(?<![\d:])(?:0\d|1[3-9]|2[0-3]):[0-5]\d(?::[0-5]\d)?(?![\d:])",
    r"\b(?:at|by|until|till)\s+(?:about\s+|around\s+)?\d{1,2}:[0-5]\d(?![\d:])",
    r"(?<=\d)\s?/\s?(?:10|100)\b(?![.,]\d)(?!\s?m(?![a-z]))",
    r"\bout of (?:10|100)\b(?![.,]\d)",
    r"^\s*(?:[-*]\s+)?\d{1,3}[.)]\s",
    # General guidance that is not data about the athlete: sleep, drink and fuel amounts after "aim for" /
    # "get closer to" / "drink", and a range "most adults need" (live run T04: "the 7–9 hours most adults
    # need"). Only these units: a target pace, HR or load stays a number that must be grounded.
    r"\b(?:aim(?:s|ing)? for|get closer to|try to get|drink(?:ing)?)\s+(?:about\s+|around\s+|roughly\s+)?"
    r"\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?\s*(?:h|hrs?|hours?|ml|l|litres?|liters?|g|grams?)(?![a-z])",
    r"\b\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?\s*(?:h|hrs?|hours?|ml|l|litres?|liters?|g|grams?)"
    r"(?:\s+(?:range|window))?\s+(?:that\s+)?(?:most|many|healthy)\s+(?:adults|people|athletes|runners)\b",
    # "Most adults need 7–9 hours", "the 7–9 hours usually recommended", "going to bed 30–45 minutes earlier"
    # (live Opus run 20261124T090917Z, T04): generic sleep advice, not a value of the athlete's night.
    r"\b(?:most|many|healthy)\s+(?:adults|people|athletes|runners)\s+(?:need|aim for|should get|do best on)\s+"
    r"(?:about\s+|around\s+)?\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?\s*(?:h|hrs?|hours?)(?![a-z])",
    r"\b\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?\s*(?:h|hrs?|hours?)\s+(?:usually\s+|generally\s+|commonly\s+)?"
    r"recommended\b",
    r"\b(?:go(?:ing)?|get(?:ting)?)\s+to\s+bed\s+(?:about\s+|around\s+)?\d+(?:\s*(?:-|–|to)\s*\d+)?\s*"
    r"(?:min|mins|minutes)\s+earlier\b",
)]

_UNITS = (
    ("kmh", r"km/h|kph|km per hour"),
    ("pace_100m", r"/\s?100\s?m|per 100\s?m"),
    ("pace_km", r"/\s?km|min/km|per km|per kilomet(?:er|re)"),
    ("pace_mi", r"/\s?mi|min/mi|per mile"),
    ("%", r"%|percent|per cent"),
    ("km", r"km|kms|kilomet(?:er|re)s?"),
    ("mi", r"mi|miles?"),
    ("ms", r"ms|milliseconds?"),
    ("min", r"min|mins|minutes?"),
    ("h", r"h|hr|hrs|hours?"),
    ("s", r"s|sec|secs|seconds?"),
    ("m", r"m|meters?|metres?"),
    ("kg", r"kg|kilos?|kilograms?"),
    ("lb", r"lbs?|pounds?"),
    ("bpm", r"bpm"),
    ("k", r"k"),
    ("x", r"x|×"),
)
_UNIT_RE = re.compile(r"\s?(?:" + "|".join(f"(?P<u{i}>{rx})" for i, (_, rx) in enumerate(_UNITS)) + r")(?![a-z])",
                      _I)

_HMIN_RE = re.compile(r"(?<![\w.])(\d{1,3})\s?h\s?([0-5]\d)(?![\d.:])", _I)  # "6h45": 6 h 45 min
_COMPOUND_RE = re.compile(r"(?<![\w.])(\d{1,3})\s*(?:h|hr|hrs|hours?)\s*(?:and\s+)?(\d{1,2})\s*"
                          r"(?:m|min|mins|minutes?)(?![a-z])", _I)
_CLOCK_RE = re.compile(r"(?<![\w.:])(\d{1,3}):([0-5]\d)(?::([0-5]\d))?(?![\d:])")
_PLAIN_RE = re.compile(r"(?<![a-wyzA-WYZ_\d.,])([+-]?)(\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.(\d+))?")
_TOOL_NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?|(?<![\w.])\d{1,3}(?:,\d{3})+(?![\d.])")
# A window length in a key name ("avg_28d", "hrv_7d", "last_28_days"): the answer may name the window ("28-day
# average", "over 28 days"; live Opus run 20261124T090917Z, F07/K02/P01).
_WINDOW_KEY_RE = re.compile(r"\"[a-z_]*?_(\d{1,3})_?(?:d|days?|w|wk|weeks?)(?:_[a-z_]*)?\"\s*:", _I)
_TOOL_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,3}):([0-5]\d)(?::([0-5]\d))?(?!\d)")

_CONV = {
    None: (1,),
    "x": (1,), "bpm": (1,),
    "%": (1, 100),
    "km": (1, 1 / 1000, 1 / 100000, 1.609344),
    "k": (1, 1 / 1000),
    "m": (1, 1000, 1 / 100, 1 / 60, 60),
    "mi": (1, 1 / 1609.344, 1 / 1.609344, 1 / 160934.4),
    "h": (1, 1 / 60, 1 / 3600, 1 / 3600000),
    "min": (1, 60, 1 / 60, 1 / 60000),
    "s": (1, 60, 3600, 1 / 1000),
    "ms": (1, 1000),
    "kg": (1, 1 / 1000, 1 / 2.20462262),
    "lb": (1, 2.20462262, 1 / 453.59237),
    "kmh": (1, 3.6),
}


@dataclass(frozen=True)
class Num:
    """One number read from an answer. kind is "plain", "clock" (a:b or a:b:c) or "pace" (a:b per km/mi)."""
    text: str
    value: float
    decimals: int = 0
    unit: str | None = None
    kind: str = "plain"
    parts: tuple = ()

    def __str__(self):
        return self.text


@dataclass
class NumberReport:
    """What check() found: numbers matched, exempt (in the prompt), unmatched and soft-unmatched."""
    matched: list
    exempt: list
    unmatched: list
    soft: list

    @property
    def passed(self) -> bool:
        return not self.unmatched

    def detail(self) -> str:
        parts = [f"{len(self.matched)} matched"]
        if self.exempt:
            parts.append(f"{len(self.exempt)} from the prompt")
        if self.unmatched:
            parts.append("unmatched: " + ", ".join(repr(n.text) for n in self.unmatched))
        if self.soft:
            parts.append("soft (small whole numbers, not failing): " + ", ".join(repr(n.text) for n in self.soft))
        return "; ".join(parts)


def _normalise(text):
    return (text.replace("−", "-").replace(" ", " ").replace(" ", " ").replace(" ", " ")
            .replace("‑", "-"))


def _blank(text, start, end):
    return text[:start] + " " * (end - start) + text[end:]


def _strip_exempt(text):
    for rx in _EXEMPT:
        text = rx.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _unit_at(text, pos):
    m = _UNIT_RE.match(text, pos)
    if not m and text[pos:pos + 1] == "-":  # "a 67-minute ride", "a 10-km run": a word unit after a hyphen
        m = _UNIT_RE.match(text, pos + 1)
        if m and not m.group(0)[:1].isalpha():
            m = None
    if not m:
        return None, pos
    return _UNITS[int(m.lastgroup[1:])][0], m.end()


_PACES = ("pace_km", "pace_mi", "pace_100m")
_UNIT_ONLY_RE = re.compile(r"^\s?(?:" + "|".join(rx for _, rx in _UNITS) + r")$", _I)
_PREFIX_UNIT_RE = re.compile(r"\b(km|mi)\s?(?:\d+(?:\.\d+)?\s*(?:-|–|to)\s*)?$", _I)
_RANGE_UNIT_RE = re.compile(r"\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s?(km|mi)(?![a-z])", _I)


def _header_unit(cell):
    """The unit a markdown table header names for its column: "Pace" / "Pace (min/km)" -> pace, "km",
    "Dist (km)", "Last night (ms)" -> that unit; None when it names none."""
    cell = cell.strip().lower()
    if "pace" in cell:
        return "pace_mi" if re.search(r"\bmi\b|mile", cell) else "pace_km"
    m = re.search(r"\(([^)]*)\)\s*$", cell)
    for text in ([m.group(1)] if m else []) + [cell]:
        if _UNIT_ONLY_RE.match(text.strip()):
            return _unit_at(text.strip(), 0)[0]
    return None


def _table_units(text):
    """{(start, end): unit} for each cell of a markdown table whose header names a unit."""
    out = {}
    lines = text.split("\n")
    pos, header = 0, None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [(m.start() + pos, m.end() + pos, m.group(1)) for m in re.finditer(r"(?<=\|)([^|]*)(?=\|)", line)]
            if header is None:
                header = [_header_unit(c[2]) for c in cells]
            elif not re.fullmatch(r"[|:\-\s]*", stripped):
                for i, (start, end, _) in enumerate(cells):
                    if i < len(header) and header[i]:
                        out[(start, end)] = header[i]
        else:
            header = None
        pos += len(line) + 1
    return out


def _column_unit(cells, pos):
    for (start, end), unit in cells.items():
        if start <= pos < end:
            return unit
    return None


def answer_numbers(text) -> list:
    """The numbers in `text` (an answer or a user prompt), in order, as Num. A number with no unit of its own
    takes the unit its markdown table column header names ("| km |", "Pace (min/km)"; a clock under a pace
    header is a pace), or a "km"/"mi" written before it ("km 7.7 to 14") or after the distance range it
    starts ("13.4 to 15.2 km")."""
    if not isinstance(text, str) or not text:
        return []
    norm = _normalise(text)
    cells = _table_units(norm)
    work = _strip_exempt(norm)
    found = []
    for m in _COMPOUND_RE.finditer(work):
        h, mi = int(m.group(1)), int(m.group(2))
        found.append((m.start(), Num(m.group(0).strip(), h + mi / 60, 0, "h", "clock", (h, mi))))
    for m in list(_COMPOUND_RE.finditer(work)):
        work = _blank(work, m.start(), m.end())
    for m in list(_HMIN_RE.finditer(work)):
        h, mi = int(m.group(1)), int(m.group(2))
        found.append((m.start(), Num(m.group(0).strip(), h + mi / 60, 0, "h", "clock", (h, mi))))
        work = _blank(work, m.start(), m.end())
    for m in _CLOCK_RE.finditer(work):
        parts = tuple(int(g) for g in m.groups() if g is not None)
        unit, end = _unit_at(work, m.end())
        if unit is None and _column_unit(cells, m.start()) in _PACES:
            unit = _column_unit(cells, m.start())
        kind = "pace" if unit in _PACES else "clock"
        found.append((m.start(), Num(m.group(0), parts[0] + parts[1] / 60, 0, unit if kind == "pace" else None,
                                     kind, parts)))
        work = _blank(work, m.start(), end if kind == "pace" else m.end())
    for m in _PLAIN_RE.finditer(work):
        sign, whole, frac = m.group(1), m.group(2), m.group(3)
        start = m.start()
        if sign and start > 0 and work[start - 1].isalpha():
            # A hyphen after a letter is no sign: Hebrew writes a prefix before a number with one ("ב-6",
            # "ל-7.5": in 6, to 7.5; live run 20261125T200810Z, H07).
            sign, start = "", start + 1
        end = m.end()
        unit, unit_end = _unit_at(work, end)
        if unit is None and end < len(work) and work[end].isalpha():
            continue  # digits run into a word: "3rd", "0badc0de", "5K-ish"
        if unit in _PACES:
            unit = None  # "5 /km" without a clock: read as a plain number
        if unit is None:
            col = _column_unit(cells, m.start())
            prefix = _PREFIX_UNIT_RE.search(work, max(0, m.start() - 20), m.start())
            if col and col not in _PACES:
                unit = col
            elif prefix:
                unit = prefix.group(1).lower()
            else:  # "from 13.4 to 15.2 km": the first end of a distance range takes the range's unit
                ahead = _RANGE_UNIT_RE.match(work, end)
                unit = ahead.group(1).lower() if ahead else None
        value = float(whole.replace(",", "") + ("." + frac if frac else ""))
        if sign == "-":
            value = -value
        text_span = work[start:unit_end if unit else end].strip()
        found.append((start, Num(text_span, value, len(frac or ""), unit, "plain")))
    found.sort(key=lambda pair: pair[0])
    return [num for _, num in found]


# Parts of a status JSON that are not values about the athlete: the rule thresholds, the raw sources, and
# time stamps. Numbers that occur only there are not grounds for an answer number.
# Thresholds are real tool output the model may quote ("10 ms would trigger a flag"); estimates of a missing
# metric are caught by the cases' re: bans instead. Source timings are not data about the athlete.
POOL_SKIP_KEYS = frozenset({"sources"})
_STAMP_KEY_RE = re.compile(r"timestamp|^generated_at$|_at$|epoch", _I)
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")


def _prune(value):
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items()
                if k not in POOL_SKIP_KEYS and not (_STAMP_KEY_RE.search(str(k)) and not isinstance(v, (dict, list)))}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def pool_text(text) -> str:
    """The part of a tool result whose numbers count: JSON without POOL_SKIP_KEYS subtrees and time-stamp
    fields, and with dates and date-times removed (they would add their parts as numbers)."""
    try:
        text = json.dumps(_prune(json.loads(text)), ensure_ascii=False)
    except (ValueError, RecursionError):
        pass
    return _ISO_RE.sub(" ", text)


# Simple arithmetic on tool values that an answer may state (sums, differences, paces): exact values only.
# Keys count as one quantity when they end in the same unit word ("distance_meters", "elevation_gain_m",
# "weekly_avg_hrv_ms", "prev_km"); keys without a unit word (ids, counts, scores) give no derived values.
_UNIT_KEY_RE = re.compile(r"(?:^|_)(m|meters|km|mi|s|sec|seconds|ms|min|minutes|h|hours|bpm|kg)$", _I)
_DIST_KEYS = ("distance_meters", "distance_m")
_DUR_KEYS = ("duration_seconds", "duration_s", "moving_duration_seconds", "elapsed_duration_seconds")
DERIVED_LIST_MAX = 60  # longer lists (samples, profiles) give no pairwise values
DERIVED_DOC_MAX = 40   # a result with more values of one dimension gives no document-wide differences
# Distances only: HRV and other "_ms" values are not durations, and time spans come from start/end pairs.
_DIM = {"m": ("len", 1), "meters": ("len", 1), "km": ("len", 1000), "mi": ("len", 1609.344)}


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _unit_word(key):
    m = _UNIT_KEY_RE.search(str(key))
    return m.group(1).lower() if m else None


_RACE_M = {"marathon": 42195.0, "half": 21097.5, "half_marathon": 21097.5}


def _hms(value):
    """Seconds of an "H:MM:SS" string, else None."""
    m = re.fullmatch(r"(\d{1,2}):([0-5]\d):([0-5]\d)", str(value or "").strip())
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def derived_numbers(value) -> list:
    """Exact simple arithmetic on the numbers of a parsed tool result (after pool pruning):
    - within one list of objects: for each key with a unit word, the sum and both differences of every pair of
      items, and the total over the list ("together 25.7 km", "the long run grew by 1.8 km");
    - within one object: both differences of every two values whose keys end in the same unit word ("5 ms
      below the 7-day average": last_night_avg_hrv_ms vs weekly_avg_hrv_ms);
    - the pace of an object with a distance and a duration, in s/km, s/mi and s/100 m, and for a race object
      (distance marathon or half) its length in metres and the goal pace of its goal_time in s/km and s/mi;
    - the span of an "<x>start" / "<x>end" pair of one object ("6 h 41 min in bed" from sleep_start/sleep_end);
    - the difference of any two distances of the result, in metres, when it holds at most DERIVED_DOC_MAX
      ("the last 6.3 km" = distance_m 14000 - end_km 7.7).
    Ratios, percent changes, averages and date spans are not derived: the server computes those."""
    out = []

    def walk(node):
        if isinstance(node, list):
            items = [x for x in node if isinstance(x, dict)]
            if 2 <= len(items) <= DERIVED_LIST_MAX:
                keys = {k for it in items for k in it if _unit_word(k) and _num(it.get(k)) is not None}
                for k in keys:
                    vals = [_num(it.get(k)) for it in items if _num(it.get(k)) is not None]
                    out.append(sum(vals))
                    for i, a in enumerate(vals):
                        for b in vals[i + 1:]:
                            out.extend((a + b, a - b, b - a))
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            groups = {}
            for k, v in node.items():
                if _num(v) is not None and _unit_word(k):
                    groups.setdefault(_unit_word(k), []).append(_num(v))
            for vals in groups.values():
                for i, a in enumerate(vals):
                    for b in vals[i + 1:]:
                        out.extend((a - b, b - a))
            dist = next((_num(node.get(k)) for k in _DIST_KEYS if _num(node.get(k))), None)
            dur = next((_num(node.get(k)) for k in _DUR_KEYS if _num(node.get(k))), None)
            if dist and dur and dist > 0:
                out.extend((dur / (dist / 1000), dur / (dist / 1609.344), dur / (dist / 100)))
            race_m = _RACE_M.get(str(node.get("distance") or "").lower())
            goal = _hms(node.get("goal_time"))
            if race_m:  # the race's own length: "the full 42.2 km"
                out.append(race_m)
            if race_m and goal:  # plan_assess' race {distance: marathon, goal_time: 3:52:00}: the goal pace
                out.extend((goal / (race_m / 1000), goal / (race_m / 1609.344)))
            for k, v in node.items():  # "sleep_start" / "sleep_end": the span ("6 h 41 min in bed")
                if str(k).endswith("start") and _num(v) is not None:
                    end = _num(node.get(str(k)[:-5] + "end"))
                    if end is not None:
                        out.append(end - _num(v))
            for v in node.values():
                walk(v)

    walk(value)
    # Differences of two distances anywhere in the result, in metres:
    # "the last 6.3 km" = distance_m 14000 - end_km 7.7. Only when the result holds few such values.
    dims = {}

    def collect(node):
        if isinstance(node, dict):
            for k, v in node.items():
                word = _unit_word(k)
                if _num(v) is not None and word in _DIM:
                    dim, factor = _DIM[word]
                    dims.setdefault(dim, []).append(_num(v) * factor)
                else:
                    collect(v)
        elif isinstance(node, list):
            for x in node:
                collect(x)

    collect(value)
    for vals in dims.values():
        if len(vals) <= DERIVED_DOC_MAX:
            for i, a in enumerate(vals):
                for b in vals[i + 1:]:
                    out.extend((a - b, b - a))
    return out


def tool_numbers(texts) -> list:
    """Every number in the tool result `texts` (an iterable of str, read through pool_text()), as floats; each
    "h:mm[:ss]" also adds its value in hours and in minutes (and "m:ss" read as minutes and as seconds); a JSON
    result also adds derived_numbers()."""
    pool = []
    for text in texts:
        if not isinstance(text, str):
            continue
        try:
            pool.extend(derived_numbers(_prune(json.loads(text))))
        except (ValueError, RecursionError):
            pass
        text = pool_text(text)
        pool.extend(float(w) for w in _WINDOW_KEY_RE.findall(text))
        for m in _TOOL_NUM_RE.finditer(text):
            try:
                pool.append(float(m.group(0).replace(",", "")))
            except ValueError:
                continue
        for m in _TOOL_CLOCK_RE.finditer(text):
            a, b = int(m.group(1)), int(m.group(2))
            c = int(m.group(3)) if m.group(3) else 0
            pool.extend((a + b / 60 + c / 3600, a * 60 + b + c / 60, a * 3600 + b * 60 + c, a * 60 + b))
    return pool


def _close(x, y, tol):
    return abs(x - y) <= tol + 1e-9


def _plain_matches(num, pool):
    tol = 0.5 * 10 ** (-num.decimals)
    for p in pool:
        for factor in _CONV.get(num.unit, (1,)):
            cand = p * factor
            if _close(num.value, cand, tol) or (num.value >= 0 and cand < 0 and _close(num.value, -cand, tol)):
                return True
    return False


def _clock_matches(num, pool):
    a, b = num.parts[0], num.parts[1]
    c = num.parts[2] if len(num.parts) > 2 else None
    if c is not None:  # h:mm:ss -> hours, tolerance half a second
        hours = a + b / 60 + c / 3600
        cands = [(hours, 1 / 7200, (1, 1 / 60, 1 / 3600, 1 / 3600000))]
    else:
        cands = [(a + b / 60, 1 / 120, (1, 1 / 60, 1 / 3600, 1 / 3600000)),   # h:mm, against h, min, s, ms
                 (a + b / 60, 1 / 120, (60, 1, 1 / 60, 1 / 60000))]          # m:ss as minutes
    for target, tol, factors in cands:
        for p in pool:
            if any(_close(target, p * f, tol) for f in factors):
                return True
    return False


def _pace_matches(num, pool):
    secs = num.parts[0] * 60 + num.parts[1]
    dist = {"pace_km": 1000, "pace_100m": 100}.get(num.unit, 1609.344)
    for p in pool:
        if _close(secs, p, 0.5) or _close(secs, p * 60, 0.5):
            return True
        if p - 1 < secs <= p:  # a pace cut to whole seconds: 298.6 s/km as "4:58 /km"
            return True
        if p > 0 and _close(secs, dist / p, 0.5):  # p in m/s
            return True
    return False


def matches(num, pool) -> bool:
    """True when `num` (a Num) equals a number of `pool` under the rules of this module."""
    if num.kind == "pace":
        return _pace_matches(num, pool)
    if num.kind == "clock":
        return _clock_matches(num, pool)
    return _plain_matches(num, pool)


def _in_prompt(num, prompt_nums):
    for q in prompt_nums:
        if q.kind == num.kind and q.parts == num.parts and num.kind != "plain":
            return True
        if num.kind == "plain" and q.kind == "plain" and math.isclose(abs(q.value), abs(num.value), abs_tol=1e-9):
            return True
    return False


def check(answer, tool_texts, prompt_texts=(), doc_texts=()) -> NumberReport:
    """Match every number of `answer` against the numbers of `tool_texts`; numbers of `prompt_texts` (what the
    user typed) and `doc_texts` (the tool descriptions and schemas) are exempt. Returns a
    NumberReport (passed when nothing but soft numbers is unmatched)."""
    pool = tool_numbers(tool_texts)
    prompt_nums = [n for t in tuple(prompt_texts) + tuple(doc_texts) for n in answer_numbers(t)]
    report = NumberReport([], [], [], [])
    for num in answer_numbers(answer):
        if _in_prompt(num, prompt_nums):
            report.exempt.append(num)
        elif matches(num, pool):
            report.matched.append(num)
        elif (num.kind == "plain" and num.decimals == 0 and num.unit in (None, "x")
              and 0 <= num.value <= SOFT_MAX):
            report.soft.append(num)
        else:
            report.unmatched.append(num)
    return report
