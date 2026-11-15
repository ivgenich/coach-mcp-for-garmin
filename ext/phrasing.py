"""Numbers check for English text written about the deterministic reports.

Code computes, a model only words. daily_status / weekly_status decide every
number, flag, level and recommendation and render a deterministic English
template. These checks decide whether a text written from that status (by a
model or anyone else) may stand in for it:

- check_text(): every number in it traces back to the status (rules below),
  it stays within the word limit, uses no tag but <b>/</b>, and is not empty;
- check_anchors(): it carries the level word (as a whole word, any case),
  every flag label and the recommendation verbatim, and names no other level;
- check_style(): no Markdown, no links, bare domains, @mentions or /commands,
  at most one emoji, and that one must be the level marker (green U+1F7E2,
  yellow U+1F7E1, red U+1F534, incomplete U+26AA);
- check_number_words(): no English number words ("forty") that the template
  does not use itself.

Each returns a list of problems, empty when the text passes; check_all() runs
all four in that order. Nothing in the server calls them yet. check_all() is
for a full report message, which must carry the level, every flag label and
the recommendation; a one- or two-sentence summary (such as the report page's
summary_en) fails check_anchors() by design, so check it with check_text(),
check_style() and check_number_words(). The template argument is read as
plain text or HTML (tags stripped, entities decoded); render_text() now gives
Markdown, which these checks do not read yet. Standard library only.

What the checks compare. Text and status strings are normalized the same way:
tags stripped and entities decoded (text only), invisible characters dropped
(Unicode categories Cf, Mn, Me: zero-width spaces, bidi marks, soft hyphens,
keycap and variation marks), thin spaces between digits removed, dashes and
quotes unified, whitespace collapsed.

Free text in the status (workout, activity and gear names, flag labels, the
recommendation, any string with letters) is data from outside or carries
thresholds: its digits and level words prove nothing on their own. They count
only inside a verbatim copy of the whole string, or of the shortened form
"<start of the name>…" that the template itself shows. Those copies are cut
out of the text before anything else is checked.

Numbers rule (check_text), on what is left. Tokens are
re.findall(r"\\d+(?:[.:,]\\d+)*") after NFKC. A token passes if it is
  (a) a date form of an ISO date value in the status (date/start/end, the
      weekly long session's date): d.m, dd.mm, d.m.yyyy, dd.mm.yyyy, d.m.yy,
      yyyy-mm-dd, or English "13 Jan", "13 January 2026", "Jan 13", "January
      13th, 2026" and day ranges such as "11-17 Jan". An English date is cut
      out whole when every day in it is such a date (its day and year are then
      not numbers to check);
  (b) one of 1 2 3 4 5 7 28 (zones, "7 days", "28 days") or len(flags);
  (c) a token of a digits-only value string such as "6:51" or "04:38"
      (H:MM compares without a leading zero);
  (d) within rounding of a numeric value v in the status:
      |t - |v|| <= 0.5 * 10^-decimals(t), so "5.8" and "6" match 5.75, "51"
      does not match 52 and "7" matches -7; an H:MM token matches a *_hours
      value (or a minutes value / 60) within one minute; "12,480" is the
      integer 12480 and "7,5" is the decimal 7.5 (never two small numbers);
  (e) repeated in the template's own wording: the word holding the number has
      the same previous and next word as somewhere in the template (so
      "under 6 h" may be repeated, "sleep 6 h" may not).
Characters that are numbers without being digits (½, ², Ⅳ) always fail.
The status is read without "text", "debug", "sources", "generated_at",
"thresholds" and "thresholds_athlete": none of them is a value a text may
quote. A threshold, in particular, is not a measured value; it may appear only
inside its flag label or in the template's own wording.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

MAX_WORDS = {"daily": 150, "weekly": 300}
SMALL_NUMBERS = frozenset({1, 2, 3, 4, 5, 7, 28})
LEVEL_MARKERS = {
    "green": "\U0001F7E2",
    "yellow": "\U0001F7E1",
    "red": "\U0001F534",
    "incomplete": "⚪",
}
LEVEL_WORDS = ("Green", "Yellow", "Red", "Incomplete data")
HIDDEN_KEYS = ("text", "debug", "sources", "generated_at", "thresholds", "thresholds_athlete")
# Number words the model may not introduce (it must write digits, which are checked).
# Compounds ("twenty-five") are two words to the tokenizer, both found here.
NUMBER_WORDS = frozenset(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety "
    "hundred thousand million dozen half "
    # Plurals that state a number too ("HRV in the sixties", "hundreds of km"),
    # and multiplicatives. Not "once", "first" or "second": plain English uses them
    # without meaning a number, and a false hit only rejects a text that was fine.
    "tens twenties thirties forties fifties sixties seventies eighties nineties "
    "dozens hundreds thousands millions twice thrice".split()
)

_TOKEN_RE = re.compile(r"\d+(?:[.:,]\d+)*")
_THOUSANDS_RE = re.compile(r"\d{1,3}(?:,\d{3})+")
_DECIMAL_RE = re.compile(r"\d+\.(\d+)")
_DECIMAL_COMMA_RE = re.compile(r"(\d+),(\d{1,2})")
_HMM_RE = re.compile(r"(\d{1,3}):([0-5]\d)")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z!?][^<>]*>")
_ALLOWED_TAGS = frozenset({"<b>", "</b>"})
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿]")
_INVISIBLE = frozenset({"Cf", "Mn", "Me"})
_THIN_SPACE_RE = re.compile(r"(?<=\d)[\u1680\u2000-\u200a\u202f\u205f\u3000]+(?=\d)")
# One dash and one quote spelling; U+05BE, U+05F3 and U+05F4 are the Hebrew
# maqaf, geresh and gershayim that workout names typed in Hebrew may carry.
_DASH_RE = re.compile("[\u05be‐‑‒–—−]")
_QUOTES = str.maketrans({"\u05f4": '"', "“": '"', "”": '"', "„": '"',
                         "\u05f3": "'", "‘": "'", "’": "'"})
# Look-alikes of the decimal point and comma.
_SEPARATORS = str.maketrans({"٫": ".", "·": ".", "‚": ",", "٬": ",", "،": ","})
_LETTER_RE = re.compile(r"[^\W\d_]")
_WORD_RE = re.compile(r"[A-Za-z]+")
_LEVEL_FOLDED = frozenset(w.casefold() for w in LEVEL_WORDS) | frozenset(LEVEL_MARKERS)
_LEVEL_KEYS = frozenset({"level", "level_en"})
_NAME_KEYS = frozenset({"name", "title", "description", "notes", "label"})
_MASK = "␀"
_MAX_TOKEN_LEN = 15
_MIN_SHORT_NAME = 2
_MARKDOWN_RE = re.compile(r"\*\*|__|```|(?m:^\s*#{1,6}\s)")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?<![\w.-])(?=[\w-]*[^\W\d_])[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?![a-z0-9-])", re.IGNORECASE
)
_MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,}")
_COMMAND_RE = re.compile(r"(?<![\w/.:<@])/[A-Za-z][A-Za-z0-9_]*")
# English dates, matched after _norm (dashes are "-"): "13 Jan", "13 January
# 2026", "11-17 Jan", "Jan 13", "January 13th, 2026", "Jan 11-17". Month names
# are capitalized: "may" is a verb.
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_MONTH_NUMBER = {name: i for i, name in enumerate(_MONTHS, 1)}
_MONTH_NUMBER.update({name[:3]: i for i, name in enumerate(_MONTHS, 1)})
_MONTH_NUMBER["Sept"] = 9
_MONTH = "(" + "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True)) + r")\.?(?![A-Za-z])"
_DAY = r"(\d{1,2})(?!\d)(?:st|nd|rd|th)?"
_YEAR = r"(?:,?\s+(\d{4})(?!\d))?"
_DAY_MONTH_RE = re.compile(rf"(?<![\w.:,]){_DAY}(?:\s?-\s?{_DAY})?\s+{_MONTH}{_YEAR}")
_MONTH_DAY_RE = re.compile(rf"(?<![A-Za-z]){_MONTH}\s+{_DAY}(?:\s?-\s?{_DAY})?(?![\d.:,]\d){_YEAR}")
# "no red flags" is an idiom, not a statement of the level, so it never counts
# as the level anchor. "Yellow Flags: ..." (the level line, then the flags line,
# after _norm folds the newline) is not the idiom.
_LEVEL_IDIOM_RE = re.compile(r"(?<!\w)(?:green|yellow|red)\s+flags?(?!\w)(?!\s*:)", re.IGNORECASE)


# --- what a text may say -----------------------------------------------------------

def phrasing_view(status: Any) -> Dict[str, Any]:
    """The status the checks read: without text, debug, sources,
    generated_at and the thresholds."""
    if not isinstance(status, dict):
        return {}
    return {k: v for k, v in status.items() if k not in HIDDEN_KEYS}


@dataclass
class Allowed:
    """Everything the checks accept, gathered from one status (and its template)."""

    numbers: List[float]  # absolute values of numeric leaves
    minutes: List[float]  # durations: *_hours leaves x 60, minute leaves
    strings: Set[str]  # canonical tokens of digits-only value strings ("6:51")
    dates: Set[str]  # date forms of ISO date strings
    small: Set[int]  # SMALL_NUMBERS and len(flags)
    # Free text whose digits and level words count only inside a verbatim copy
    # (normalized, longest first), including "<start>…" forms the template shows.
    texts: List[str] = field(default_factory=list)
    # (previous word, word, next word) around every number in the template.
    contexts: Set[Tuple[str, str, str]] = field(default_factory=set)


def _leaves(node: Any, key: str = "") -> Iterator[Tuple[str, Any]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, str(k))
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _leaves(item, key)
    else:
        yield key, node


def _is_hours_key(key: str) -> bool:
    return key == "hours" or key.endswith("_hours") or key.endswith("_h")


def _is_minutes_key(key: str) -> bool:
    if key.startswith("optimal_"):  # weekly load bounds, not durations
        return False
    return key == "minutes" or key.endswith("_minutes") or key.endswith("_min")


def _is_name_key(key: str) -> bool:
    return key in _NAME_KEYS or key.endswith("_name")


def _norm(text: Any, markup: bool = True) -> str:
    """For every comparison: what a reader sees, in one spelling.

    markup=True (model text, templates): tags stripped, entities decoded.
    Then: invisible characters dropped, thin spaces between digits removed,
    one dash, one quote, single spaces.
    """
    s = str(text or "")
    if markup:
        s = html.unescape(_TAG_RE.sub("", s))
    s = unicodedata.normalize("NFC", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in _INVISIBLE)
    s = _THIN_SPACE_RE.sub("", s)
    s = _DASH_RE.sub("-", s).translate(_QUOTES)
    return " ".join(s.split())


def _mask(norm_text: str, texts: List[str]) -> str:
    """norm_text with every verbatim, whole-word copy of a free text cut out."""
    for item in texts:
        norm_text = re.sub(r"(?<!\w)" + re.escape(item) + r"(?!\w)", f" {_MASK} ", norm_text)
    return norm_text


def _mask_dates(text: str, dates: Set[str], bad: Optional[List[str]] = None) -> str:
    """text with every English date of the status cut out, like a verbatim name.

    A date ("13 Jan", "Jan 13, 2026", "11-17 Jan") is cut only when each of
    its days, with its year when one is written, is a date of the status.
    Any other date is added to bad (when given) so the caller can reject it
    as a whole: its day alone could pass as a small number or a status value
    ("Tue 10 Nov" on the 11 Nov report). Its digits also stay in the text.
    """
    def cut(m: "re.Match[str]", month_group: int, day_groups: Tuple[int, int], year_group: int) -> str:
        month = _MONTH_NUMBER[m.group(month_group)]
        year = m.group(year_group)
        days = [m.group(g) for g in day_groups if m.group(g)]
        keys = [f"{int(d)}.{month}" + (f".{year}" if year else "") for d in days]
        if all(k in dates for k in keys):
            return f" {_MASK} "
        if bad is not None:
            bad.append(m.group(0).strip())
        return m.group(0)

    text = _DAY_MONTH_RE.sub(lambda m: cut(m, 3, (1, 2), 4), text)
    return _MONTH_DAY_RE.sub(lambda m: cut(m, 1, (2, 3), 4), text)


def _number_text(norm_text: str) -> str:
    """Digits and separators in their plain forms (full-width, Arabic separators)."""
    return unicodedata.normalize("NFKC", norm_text).translate(_SEPARATORS)


def _core(chunk: str) -> str:
    return _MASK if _MASK in chunk else re.sub(r"^\W+|\W+$", "", chunk)


def _context(chunks: List[str], i: int) -> Tuple[str, str, str]:
    return (
        _core(chunks[i - 1]) if i > 0 else "",
        _core(chunks[i]),
        _core(chunks[i + 1]) if i + 1 < len(chunks) else "",
    )


def _canon(token: str) -> str:
    """Canonical form for comparing number tokens as strings."""
    if len(token) > _MAX_TOKEN_LEN:
        return token
    if _THOUSANDS_RE.fullmatch(token):
        return str(int(token.replace(",", "")))
    m = _HMM_RE.fullmatch(token)
    if m:
        return f"{int(m.group(1))}:{m.group(2)}"
    if token.isdigit():
        return str(int(token))
    return token


def _date_forms(year: int, month: int, day: int) -> Set[str]:
    return {
        f"{day}.{month}",
        f"{day:02d}.{month:02d}",
        f"{day}.{month:02d}",
        f"{day:02d}.{month}",
        f"{day}.{month}.{year}",
        f"{day:02d}.{month:02d}.{year}",
        f"{day}.{month}.{year % 100:02d}",
        f"{day:02d}.{month:02d}.{year % 100:02d}",
        f"{year}-{month:02d}-{day:02d}",
    }


def allowed_numbers(status: Any, template: str = "") -> Allowed:
    """Collect what the checks accept from status (see the module docstring).

    template (the deterministic report built from the same status) adds the
    shortened names it shows and the word context of its numbers.
    """
    view = phrasing_view(status)
    numbers: Set[float] = set()
    minutes: Set[float] = set()
    strings: Set[str] = set()
    dates: Set[str] = set()
    texts: Set[str] = set()
    for key, value in _leaves(view):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            if value != value or value in (float("inf"), float("-inf")):
                continue
            numbers.add(abs(float(value)))
            if _is_hours_key(key):
                minutes.add(abs(float(value)) * 60.0)
            elif _is_minutes_key(key):
                minutes.add(abs(float(value)))
        elif isinstance(value, str):
            named = _is_name_key(key)
            m = _ISO_DATE_RE.match(value.strip())
            if m and not named:
                year, month, day = (int(g) for g in m.groups())
                if 1 <= month <= 12 and 1 <= day <= 31:
                    dates |= _date_forms(year, month, day)
                continue
            norm = _norm(value, markup=False)
            if _LETTER_RE.search(norm):
                # A level word is never masked, not even as a workout's name:
                # check_anchors must see it (and a name "Green" cannot hide one).
                if len(norm) >= 2 and norm.casefold() not in _LEVEL_FOLDED and key not in _LEVEL_KEYS:
                    texts.add(norm)
            elif not named:
                for token in _TOKEN_RE.findall(_number_text(norm)):
                    strings.add(_canon(token))
    small = set(SMALL_NUMBERS)
    flags = view.get("flags")
    if isinstance(flags, list):
        small.add(len(flags))

    contexts: Set[Tuple[str, str, str]] = set()
    if isinstance(template, str) and template.strip():
        shown = _norm(template)
        for item in list(texts) if "…" in shown else ():
            # A name the template shortened: "<start of the name>…".
            for cut in range(len(item) - 1, _MIN_SHORT_NAME - 1, -1):
                short = item[:cut].rstrip() + "…"
                if short in shown:
                    texts.add(short)
                    break
        ordered = sorted(texts, key=len, reverse=True)
        chunks = _mask_dates(_number_text(_mask(shown, ordered)), dates).split()
        for i, chunk in enumerate(chunks):
            if _TOKEN_RE.search(chunk):
                contexts.add(_context(chunks, i))
    return Allowed(
        sorted(numbers), sorted(minutes), strings, dates, small,
        sorted(texts, key=len, reverse=True), contexts,
    )


def _numeric(token: str) -> Optional[Tuple[float, int]]:
    """(value, decimals) of a plain, decimal or thousands-grouped token."""
    if _THOUSANDS_RE.fullmatch(token):
        return float(int(token.replace(",", ""))), 0
    if token.isdigit():
        return float(int(token)), 0
    m = _DECIMAL_RE.fullmatch(token)
    if m:
        return float(token), len(m.group(1))
    m = _DECIMAL_COMMA_RE.fullmatch(token)
    if m:  # "7,5": one decimal number, never the two numbers 7 and 5
        return float(f"{int(m.group(1))}.{m.group(2)}"), len(m.group(2))
    return None


def _token_ok(token: str, allowed: Allowed) -> bool:
    if len(token) > _MAX_TOKEN_LEN:
        return False
    if token in allowed.dates:
        return True
    if token.isdigit() and int(token) in allowed.small:
        return True
    if _canon(token) in allowed.strings:
        return True
    m = _HMM_RE.fullmatch(token)
    if m:
        total = int(m.group(1)) * 60 + int(m.group(2))
        return any(abs(total - v) <= 1.0 + 1e-6 for v in allowed.minutes)
    parsed = _numeric(token)
    if parsed is None:
        return False
    value, decimals = parsed
    tolerance = 0.5 * 10 ** (-decimals) + 1e-9
    return any(abs(value - v) <= tolerance for v in allowed.numbers)


def _plain(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text))


def check_text(text: Any, status: Any, *, max_words: int, template: str = "") -> List[str]:
    """Problems with a phrased text (empty list = OK). See the module docstring."""
    raw = text if isinstance(text, str) else ""
    plain = _plain(raw)
    if not re.search(r"\w", plain):  # nothing but spaces, marks, emoji or tags
        return ["empty: no text"]
    problems: List[str] = []
    for tag in _TAG_RE.findall(raw):
        if tag not in _ALLOWED_TAGS:
            problems.append(f"tag_not_allowed: {tag[:24]}")
    words = [w for w in plain.split() if re.search(r"\w", w)]
    if len(words) > max_words:
        problems.append(f"too_many_words: {len(words)} > {max_words}")
    allowed = allowed_numbers(status, template)
    visible = _mask(_norm(raw), allowed.texts)
    for ch in dict.fromkeys(visible):
        if unicodedata.category(ch) in ("No", "Nl"):  # ½, ², Ⅳ: numbers that are not digits
            problems.append(f"number_not_allowed: {ch}")
    bad_dates: List[str] = []
    chunks = _mask_dates(_number_text(visible), allowed.dates, bad_dates).split()
    for i, chunk in enumerate(chunks):
        # yyyy-mm-dd is one date, not three numbers.
        rest = _ISO_DATE_RE.sub(lambda m: " " if m.group(0) in allowed.dates else m.group(0), chunk)
        tokens = _TOKEN_RE.findall(rest)
        if not tokens or _context(chunks, i) in allowed.contexts:
            continue
        for token in tokens:
            if not _token_ok(token, allowed):
                problems.append(f"number_not_allowed: {token[:24]}")
    problems.extend(f"number_not_allowed: {d[:24]}" for d in dict.fromkeys(bad_dates))
    return problems


def _has_phrase(text: str, phrase: str) -> bool:
    """phrase in text as whole words, in any case ("a yellow day" has "Yellow")."""
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.IGNORECASE) is not None


def check_anchors(text: Any, status: Any, *, template: str = "") -> List[str]:
    """The level word, every flag label and the recommendation, verbatim.

    Level words are whole words in any case ("Reduced" holds no "Red"), looked
    for outside verbatim copies of names and labels: a workout called "Green"
    does not make the day green. Labels and the recommendation are copied
    exactly (case included).
    """
    view = phrasing_view(status)
    norm = _norm(text)
    outside = _mask(norm, allowed_numbers(view, template).texts)
    problems: List[str] = []
    level_en = view.get("level_en")
    if isinstance(level_en, str) and level_en.strip():
        level = _norm(level_en, markup=False)
        if not _has_phrase(_LEVEL_IDIOM_RE.sub(" ", outside), level):
            problems.append("level_missing")
        for word in LEVEL_WORDS:
            if word.casefold() != level.casefold() and _has_phrase(outside, word):
                problems.append(f"level_conflict: {word}")
    for flag in view.get("flags") or []:
        label = flag.get("label_en") if isinstance(flag, dict) else None
        if isinstance(label, str) and label.strip() and _norm(label, markup=False) not in norm:
            problems.append(f"flag_missing: {flag.get('id')}")
    recommendation = view.get("recommendation_en")
    if isinstance(recommendation, str) and recommendation.strip():
        if _norm(recommendation, markup=False).rstrip(".") not in norm:
            problems.append("recommendation_missing")
    return problems


def check_style(text: Any, status: Any) -> List[str]:
    """No Markdown, links, domains, @mentions or /commands; at most one emoji,
    and only the level marker."""
    raw = text if isinstance(text, str) else ""
    shown = _norm(raw)
    problems: List[str] = []
    if _MARKDOWN_RE.search(raw):
        problems.append("markdown")
    if _URL_RE.search(raw) or _URL_RE.search(shown) or _DOMAIN_RE.search(shown):
        problems.append("link")
    if _MENTION_RE.search(shown):
        problems.append("mention")
    if _COMMAND_RE.search(shown):
        problems.append("command")
    emojis = _EMOJI_RE.findall(raw)
    marker = LEVEL_MARKERS.get(str(phrasing_view(status).get("level")))
    if len(emojis) > 1:
        problems.append(f"emoji: {len(emojis)} found, at most 1")
    elif emojis and emojis[0] != marker:
        problems.append("emoji: not the level marker")
    return problems


def _number_word(word: str) -> Optional[str]:
    """word in lower case if it is a number word ("Forty" -> "forty"), else None."""
    low = word.lower()
    return low if low in NUMBER_WORDS else None


def check_number_words(text: Any, status: Any, *, template: str = "") -> List[str]:
    """English number words the template does not use itself.

    A number in words would bypass check_text, so the model must write
    digits. Words inside verbatim copies of names and labels are left alone.
    """
    texts = allowed_numbers(status, template).texts

    def found(source: Any) -> List[str]:
        words = _WORD_RE.findall(_mask(_norm(source), texts))
        return [core for core in map(_number_word, words) if core]

    known = set(found(template))
    return [f"number_word: {word}" for word in dict.fromkeys(found(text)) if word not in known]


# --- all checks at once ---------------------------------------------------------

def check_all(text: Any, status: Any, *, kind: str, template: str = "") -> List[str]:
    """Every problem of text against status, in the order check_text,
    check_anchors, check_style, check_number_words; [] when the text passes.

    kind is "daily" or "weekly" and sets the word limit (MAX_WORDS). template
    is the deterministic report text the status renders to: wording repeated
    from it may carry its numbers and number words.
    """
    if kind not in MAX_WORDS:
        raise ValueError(f"kind must be one of {sorted(MAX_WORDS)}, got {kind!r}")
    view = phrasing_view(status)
    return (
        check_text(text, view, max_words=MAX_WORDS[kind], template=template)
        + check_anchors(text, view, template=template)
        + check_style(text, view)
        + check_number_words(text, view, template=template)
    )
