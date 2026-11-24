"""Clean free text from Garmin before a model reads it.

Names, titles, descriptions and notes in Garmin answers are typed by the account owner or by apps and coaches
linked to the account, so they are untrusted text. This module:

- drops invisible, control and bidi characters: every Default_Ignorable_Code_Point of Unicode 18.0 (hard-coded,
  so the result does not depend on the interpreter's unicodedata version), C0 and C1 controls, surrogates,
  noncharacters, U+2800 and the Egyptian hieroglyph format controls U+13430-1343F;
- maps tabs, line breaks and every Zs space to one ASCII space;
- keeps the joiners and variation selectors that readable text needs: ZWJ inside emoji sequences and between
  letters of the joining scripts (Arabic, Indic, Tibetan, Myanmar, Khmer, Mongolian), ZWNJ between letters of
  those scripts, VS15/VS16 right after an emoji or a keycap base, and the three RGI subdivision flags;
- caps a value by code points without cutting a visible unit (a combining sequence, an emoji sequence, a flag);
- replaces markdown images with [image], markdown links with "text [link]", most URLs with [link] (also when
  ":", "/" or "." is escaped or written as a character reference) and HTML tags with [html], and breaks any
  inline-link opener "](" left over;
- cleans every string value of an upstream JSON answer and keeps the JSON valid (clean_tool_answer).

The per-character pass runs in two phases, so cleaning is idempotent: f(f(x)) == f(x).

Standard library only; imports on Python 3.10. Nothing here raises on any input, logs, or does I/O. A value
that is not a str comes back unchanged (name_or_none() gives None for it).
"""

import json
import re
import unicodedata

NAME_MAX = 100
DESCRIPTION_MAX = 2000  # could drop to 500 once descriptions are returned only on request
ECHO_MAX = 64
ELLIPSIS = "\u2026"
LINK = "[link]"
IMAGE = "[image]"
HTML = "[html]"

# --- character data ------------------------------------------------------------------------------------

# Default_Ignorable_Code_Point, DerivedCoreProperties-18.0.0.txt.
_DI = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
       (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
       (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
       (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
# Not default-ignorable, still dropped: C0, DEL and C1, interlinear annotation, surrogates, the BMP
# noncharacter block. Every other noncharacter ((cp & 0xFFFE) == 0xFFFE) is added below.
_EXTRA = ((0x0000, 0x001F), (0x007F, 0x009F), (0xFFF9, 0xFFFB), (0xD800, 0xDFFF), (0xFDD0, 0xFDEF))
# A blank Braille cell and the Egyptian hieroglyph format controls draw nothing.
_BLANK = ((0x2800, 0x2800), (0x13430, 0x1343F))
# Each becomes one ASCII space; checked before the drop rule.
_SPACES = frozenset({0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x85, 0x2028, 0x2029})
_ZS = frozenset({0x0020, 0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000})  # all 17 Zs
# Coarse emoji test for the ZWJ and variation-selector rules (a superset of Extended_Pictographic).
_EMOJIISH = ((0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x2BFF), (0x3030, 0x3030), (0x303D, 0x303D),
             (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1FAFF), (0x1FC00, 0x1FFFD))
# Scripts whose orthography uses ZWJ or ZWNJ between letters. By range, not unicodedata, so py3.10 and
# py3.12 decide alike.
_JOINING = ((0x0600, 0x08FF), (0x0900, 0x0DFF), (0x0F00, 0x0FFF), (0x1000, 0x109F), (0x1780, 0x17FF),
            (0x1800, 0x18AF), (0xA8E0, 0xA8FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFC))
ZWNJ, ZWJ, VS15, VS16 = "\u200c", "\u200d", "\ufe0e", "\ufe0f"
_KEYCAP, _CANCEL_TAG, _BLACK_FLAG = "\u20e3", "\U000e007f", "\U0001f3f4"
_KEYCAP_BASES = frozenset("0123456789#*")
# The only RGI emoji tag sequences (UTS #51): the flags of England, Scotland and Wales.
_RGI_TAG_FLAGS = tuple("".join(chr(0xE0000 + ord(c)) for c in spec) + _CANCEL_TAG
                       for spec in ("gbeng", "gbsct", "gbwls"))
_CONTEXT = frozenset({ZWNJ, ZWJ, VS15, VS16})  # kept by phase 1, decided by phase 2


def _in(cp, ranges):
    for lo, hi in ranges:
        if lo <= cp <= hi:
            return True
    return False


def _without(ranges, points):
    """`ranges` minus the code points in `points`, as sorted (lo, hi) pairs."""
    out = []
    for lo, hi in sorted(ranges):
        start = lo
        for p in sorted(p for p in points if lo <= p <= hi):
            if start <= p - 1:
                out.append((start, p - 1))
            start = p + 1
        if start <= hi:
            out.append((start, hi))
    return out


def _char_class(ranges):
    return "".join("\\U%08x" % lo if lo == hi else "\\U%08x-\\U%08x" % (lo, hi) for lo, hi in ranges)


_NONCHARACTERS = tuple((plane | 0xFFFE, plane | 0xFFFF) for plane in range(0, 0x110000, 0x10000))
_SPACE_POINTS = (_SPACES | _ZS) - {0x20}  # a space is already a space
_DROP_RANGES = _without(_DI + _EXTRA + _BLANK + _NONCHARACTERS,
                        _SPACES | _ZS | {ord(c) for c in _CONTEXT})
_SPACE_RE = re.compile("[" + _char_class(sorted((p, p) for p in _SPACE_POINTS)) + "]")
_DROP_RE = re.compile("[" + _char_class(_DROP_RANGES) + "]")
_TAG_RE = re.compile("[\\U000e0000-\\U000e0fff]")
_RGI_RE = re.compile("(" + "|".join(re.escape(_BLACK_FLAG + seq) for seq in _RGI_TAG_FLAGS) + ")")
_CONTEXT_RE = re.compile("[\u200c\u200d\ufe0e\ufe0f]")
_MULTI_SPACE_RE = re.compile(" {2,}")


def _emojiish(ch):
    return bool(ch) and _in(ord(ch), _EMOJIISH)


def _joining(ch):
    return bool(ch) and _in(ord(ch), _JOINING)


def _phase1(text):
    """Spaces become " "; every unconditional code point is dropped. ZWJ, ZWNJ, VS15, VS16 and whole RGI tag
    flags stay for phase 2, which judges them on the string after the drops."""
    if _TAG_RE.search(text) is None:
        return _DROP_RE.sub("", _SPACE_RE.sub(" ", text))
    parts = _RGI_RE.split(text)  # odd indices: an RGI flag with its tags, kept whole
    for i in range(0, len(parts), 2):
        parts[i] = _DROP_RE.sub("", _SPACE_RE.sub(" ", parts[i]))
    return "".join(parts)


def _phase2(text):
    """The context rules, run on the phase 1 string, so "the next character" is
    always one that phase 1 kept. "Previous" is the previous character this phase kept."""
    if _CONTEXT_RE.search(text) is None:
        return text
    out = []
    pos = 0
    prev = ""
    last = len(text) - 1
    for m in _CONTEXT_RE.finditer(text):
        i = m.start()
        if i > pos:
            out.append(text[pos:i])
            prev = text[i - 1]
        ch = text[i]
        nxt = text[i + 1] if i < last else ""
        if ch == ZWJ:
            keep = prev not in ("", ZWJ, ZWNJ) and (
                ((_emojiish(prev) or prev in (VS16, _KEYCAP, _CANCEL_TAG)) and _emojiish(nxt))
                or (_joining(prev) and _joining(nxt)))
        elif ch == ZWNJ:
            keep = prev not in ("", ZWJ, ZWNJ) and _joining(prev) and _joining(nxt)
        else:  # VS15 or VS16: once, right after an emoji or a keycap base
            keep = _emojiish(prev) or prev in _KEYCAP_BASES
        if keep:
            out.append(ch)
            prev = ch
        pos = i + 1
    out.append(text[pos:])
    return "".join(out)


def _char_pass(text):
    """The per-character pass of 5.1 on one str (phase 1, then phase 2)."""
    return _phase2(_phase1(text))


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# --- public text functions ---------------------------------------------------------------------------

def clean_text(value, max_chars=None):
    """Full clean of one string: the character pass, runs of spaces collapsed, both ends trimmed, then cap()
    when `max_chars` (an int) is given and the text is longer. Anything that is not a str comes back unchanged.
    Idempotent: clean_text(clean_text(x)) == clean_text(x)."""
    if not isinstance(value, str):
        return value
    text = value if (value.isascii() and value.isprintable()) else _char_pass(value)  # printable ASCII: nothing to drop
    if "  " in text:
        text = _MULTI_SPACE_RE.sub(" ", text)
    text = text.strip(" ")
    if _is_int(max_chars) and len(text) > max_chars:
        text = cap(text, max_chars)
    return text


def strip_invisible(value):
    """The character pass only: no collapse, no trim, no cap, no links. Printable ASCII comes back unchanged
    (the same object), so ids, timestamps, enum codes and Garmin URLs never change."""
    if not isinstance(value, str) or (value.isascii() and value.isprintable()):
        return value
    text = _char_pass(value)
    return value if text == value else text


def _continues(text, k):
    """True when text[k] belongs to the same visible unit as text[k - 1]."""
    ch, prev = text[k], text[k - 1]
    cp = ord(ch)
    return (unicodedata.category(ch)[0] == "M"
            or ch in _CONTEXT
            or 0x1F3FB <= cp <= 0x1F3FF  # skin tones
            or 0xE0020 <= cp <= 0xE007F  # tags
            or prev in (ZWJ, ZWNJ))


def _safe_end(text, end):
    """The largest cut <= end that leaves no visible unit split and no half regional-indicator pair."""
    n = len(text)
    if end >= n:
        return n
    cut = max(end, 0)
    while True:
        while 0 < cut < n and _continues(text, cut):
            cut -= 1
        run = 0  # regional indicators pair up into flags; never keep half a flag
        j = cut - 1
        while j >= 0 and 0x1F1E6 <= ord(text[j]) <= 0x1F1FF:
            run += 1
            j -= 1
        if run % 2 == 0:
            return cut
        cut -= 1  # drop the half flag, then check again: it may have been joined to what came before


def cap(text, max_chars):
    """`text` cut to at most `max_chars` code points, ending with ELLIPSIS when cut. Never splits a visible
    unit (a mark, a joiner, a variation selector, a skin tone or a tag stays with what it modifies) and never
    keeps half a flag. Unchanged when it fits, when `text` is not a str or `max_chars` not an int.
    `max_chars` should be at least 2: 1 gives just the ellipsis, 0 or less an empty string."""
    if not isinstance(text, str) or not _is_int(max_chars) or len(text) <= max_chars:
        return text
    if max_chars < 1:
        return ""
    return text[:_safe_end(text, max_chars - 1)].rstrip() + ELLIPSIS


def cut(text, n):
    """The longest prefix of `text` of at most `n` code points that does not end inside a visible unit (the
    cap() rule, with no ellipsis and no trim). Unchanged when it fits or when the input is not (str, int)."""
    if not isinstance(text, str) or not _is_int(n) or len(text) <= n:
        return text
    if n <= 0:
        return ""
    return text[:_safe_end(text, n)]


# Link forms. Matching runs on a copy with U+FF01-FF5E folded to ASCII (one to
# one, so spans match the original). Details of the patterns, each for idempotence or cost:
# - images, markdown links and HTML tags also take the "(...)" groups right after them, so a replacement is never
#   followed by "(...)" and turned into a new link by the next round;
# - link text stops at "[" and a link target at "(" or "[" (a target such as "a_(b)" is left to the URL pass),
#   and the HTML tag body is found with str.find, so every pass runs in linear time, whatever the text; a tag body
#   may hold "<", so a nested tag cannot leave an outer one behind;
# - "<\s*/?\s*" is written "<\s*(?:/\s*)?" (the same strings, without the quadratic backtracking over spaces);
# - a scheme or "www." followed by the ellipsis of a cut value is not a link.
# Later changes:
# - CommonMark reads a backslash escape or a character reference inside a link destination as the character, and
#   a browser does in an attribute, so ":", "/" and "." are also matched in those forms ("https&colon;&sol;&sol;");
# - "ftp", "ws", "wss" and "file" need "//" and "data" needs a data-URI shape ("type;param,"), so workout
#   notation such as "FTP:280W", "WS:20s", "HR data:150" or "file:2 of 3" stays;
# - a protocol-relative host ends in a label that starts with a letter, or in a number (an IP address), so
#   "6x800 //2.5min jog" stays;
# - a scheme or "www" counts after "_" too (GFM autolinks "_www."), not only after a non-word character;
# - a last pass turns every "](" the other passes left (a destination with "(" or "[", link text with "[", a
#   scheme spelled with character references) into "] (", which no renderer reads as a link or an image.
_IMAGE_RE = re.compile(r"!\[[^\[\]\n]*\]\([^()\[\n]*\)(?:\([^()\[\n]*\))*")
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\([^()\[\n]*\)(?:\([^()\[\n]*\))*")
_HTML_START_RE = re.compile(r"(?i)<\s*(?:/\s*)?(?:a|img|image|svg|iframe|frame|script|style|link|meta|base"
                            r"|object|embed|video|audio|source|picture|form|input|body|html)\b")
_PARENS_RE = re.compile(r"(?:\([^()\[\n]*\))*")
_COLON = r"(?::|\\:|&(?:colon;|#0*58;?|#x0*3a;?))"
_SLASH = r"(?:/|\\/|&(?:sol;|#0*47;?|#x0*2f;?))"
_DOT = r"(?:\.|\\\.|&(?:period;|#0*46;?|#x0*2e;?))"
_START = r"(?<![^\W_])"  # not right after a letter or a digit
_URL_RE = re.compile(r"(?i)" + _START + r"(?:(?:https?|javascript|vbscript|mailto)" + _COLON
                     + r"|(?:ftp|wss?|file)" + _COLON + _SLASH + _SLASH
                     + r"|data" + _COLON + r"[\w.+/-]*(?:;[\w=.+-]+)*,)(?!\u2026)\S+"
                     + r"|" + _START + r"www" + _DOT + r"(?!\u2026)\S+")
_PROTO_REL_RE = re.compile(r"(?i)(?<![\w/\\])" + _SLASH + _SLASH + r"[\w-]+(?:" + _DOT + r"[\w-]+)*" + _DOT
                           + r"(?:[^\W\d_][\w-]*|[0-9]+(?![\w-])|0x[0-9a-f]*(?![\w-]))\S*")
_OPENER_RE = re.compile(r"\]\(")
_FULLWIDTH = {cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)}
_FULLWIDTH_RE = re.compile("[\uff01-\uff5e]")
_LINKISH_RE = re.compile("[\\[<:/&\\\\\uff01-\uff5e]|www", re.IGNORECASE)  # nothing can match without one
_LINK_ROUNDS = 4  # the passes repeat until nothing changes (one changing round is the norm), at most 4 times


def _regex_spans(rx):
    def spans(folded):
        for m in rx.finditer(folded):
            yield m.start(), m.end(), m
    return spans


def _html_spans(folded):
    """(start, end, None) for each HTML tag, leftmost first: a _HTML_START_RE match, everything up to the first
    ">" on the same line, then the _PARENS_RE groups right after it. The same spans as re.finditer() over the
    head pattern followed by `[^>\\n]*>`, in linear time: the next ">" and the next line end are looked up once
    and reused while later tag starts lie before them (tag starts only move right)."""
    pos = 0
    n = len(folded)
    next_gt = next_nl = -1
    while True:
        m = _HTML_START_RE.search(folded, pos)
        if m is None:
            return
        head = m.end()
        if next_gt < head:
            next_gt = folded.find(">", head)
            if next_gt < 0:  # no ">" left anywhere: no later tag can close either
                return
        if next_nl < head:
            next_nl = folded.find("\n", head)
            if next_nl < 0:
                next_nl = n
        if next_gt < next_nl:
            end = _PARENS_RE.match(folded, next_gt + 1).end()
            yield m.start(), end, None
            pos = end
        else:  # the line ends before its next ">"
            pos = m.start() + 1


def _markdown_link(m, original):
    text = original[m.start(1):m.end(1)].strip()
    return f"{text} {LINK}" if text else LINK


_LINK_PASSES = ((_regex_spans(_IMAGE_RE), lambda m, original: IMAGE),
                (_regex_spans(_MD_LINK_RE), _markdown_link),
                (_html_spans, lambda m, original: HTML),
                (_regex_spans(_URL_RE), lambda m, original: LINK),
                (_regex_spans(_PROTO_REL_RE), lambda m, original: LINK),
                (_regex_spans(_OPENER_RE), lambda m, original: "] ("))


def _replace(spans, text, make):
    folded = text.translate(_FULLWIDTH) if _FULLWIDTH_RE.search(text) else text
    pieces = []
    last = 0
    for start, end, m in spans(folded):
        pieces.append(text[last:start])
        pieces.append(make(m, text))
        last = end
    if not pieces:
        return text
    pieces.append(text[last:])
    return "".join(pieces)


def neutralize_links(value):
    """Markdown images -> [image]; markdown links -> "text [link]" (just [link] when the text is empty); HTML
    tags -> [html]; URLs with a scheme or "www." and protocol-relative links -> [link], also when ":", "/" or
    "." is written as a backslash escape or a character reference; then any "](" left -> "] (", so no inline
    link or image survives. In that order, repeated until nothing changes. Bare host names, other "<...>" text
    and notation such as "FTP:280W" stay ("400m.easy/200m", "HR <150", "6x800 //2.5min jog"). Linear time in
    the length of `value`."""
    if not isinstance(value, str) or _LINKISH_RE.search(value) is None:
        return value
    text = value
    for _ in range(_LINK_ROUNDS):
        before = text
        for spans, make in _LINK_PASSES:
            text = _replace(spans, text, make)
        if text is before:
            break
    return text


def clean_free_text(value, max_chars=NAME_MAX):
    """clean_text(), then neutralize_links(), then cap(max_chars). Anything that is not a str comes back
    unchanged."""
    if not isinstance(value, str):
        return value
    return cap(neutralize_links(clean_text(value)), max_chars)


def name_or_none(value, max_chars=NAME_MAX):
    """clean_free_text() of a str, or None for anything else and for an empty result."""
    if not isinstance(value, str):
        return None
    return clean_free_text(value, max_chars) or None


# --- JSON answers ------------------------------------------------------------------------------------

_FREE_TEXT_KEYS = frozenset({"name", "title", "description", "notes", "note", "label", "location", "comment",
                             "comments", "training_plan", "customMakeModel"})
_FREE_TEXT_SUFFIXES = ("_name", "Name", "_description", "Description", "_notes", "Notes")
_LONG_TEXT_KEYS = frozenset({"description", "notes", "note", "comment", "comments"})
_LONG_TEXT_SUFFIXES = ("_description", "Description", "_notes", "Notes")


def is_free_text_key(key) -> bool:
    """True for a key whose string value is typed by a person (activityName, displayName, description, ...)."""
    return isinstance(key, str) and (key in _FREE_TEXT_KEYS or key.endswith(_FREE_TEXT_SUFFIXES))


def free_text_cap(key) -> int:
    """DESCRIPTION_MAX for descriptions, notes and comments; NAME_MAX for every other free-text key."""
    if isinstance(key, str) and (key in _LONG_TEXT_KEYS or key.endswith(_LONG_TEXT_SUFFIXES)):
        return DESCRIPTION_MAX
    return NAME_MAX


_CONTAINERS = (dict, list, tuple)


def _clean_leaf(value, key):
    if not isinstance(value, str):
        return value
    if is_free_text_key(key):
        new = clean_free_text(value, free_text_cap(key))
    else:
        new = strip_invisible(value)
    return value if new == value else new


def _frame(container, key):
    # [container, item iterator, new items, key of the child being cleaned, changed, is a dict, key for items]
    # Items are read with the base class's own methods, so a subclass that overrides them cannot make this raise.
    if isinstance(container, dict):
        return [container, iter(dict.items(container)), [], None, False, True, key]
    base = list if isinstance(container, list) else tuple
    return [container, base.__iter__(container), [], None, False, False, key]


def _clean_tree(top, key):
    """clean_json() of a container, walked with an explicit stack: no recursion limit, and a container that
    is already on the current path (a cycle) is left as it is."""
    on_path = {id(top)}
    stack = [_frame(top, key)]
    result = top
    while stack:
        frame = stack[-1]
        container, items, out, _, changed, is_dict, item_key = frame
        descended = False
        for item in items:
            if is_dict:
                k, v = item
                child_key = k
            else:
                k, v = None, item
                child_key = item_key
            if isinstance(v, _CONTAINERS):
                if id(v) not in on_path:
                    frame[3], frame[4] = k, changed
                    on_path.add(id(v))
                    stack.append(_frame(v, child_key))
                    descended = True
                    break
                new_v = v
            else:
                new_v = _clean_leaf(v, child_key)
            out.append((k, new_v) if is_dict else new_v)
            if new_v is not v:
                changed = True
        if descended:
            continue
        stack.pop()
        on_path.discard(id(container))
        if not changed:
            new = container
        elif is_dict:
            new = dict(out)
        elif isinstance(container, tuple):
            new = tuple(out)
        else:
            new = list(out)
        if stack:
            parent = stack[-1]
            parent[2].append((parent[3], new) if parent[5] else new)
            if new is not container:
                parent[4] = True
        else:
            result = new
    return result


def clean_json(obj, key=None) -> tuple:
    """(cleaned object, changed). A dict cleans each value with its own key; a list or tuple cleans each item
    with the parent's key; a str under a free-text key gets clean_free_text() with that key's cap, any other
    str gets strip_invisible(); everything else is left alone. Dict keys never change. When nothing changed
    the original object comes back (identity), so the caller can skip a re-dump."""
    if isinstance(obj, _CONTAINERS):
        new = _clean_tree(obj, key)
    else:
        new = _clean_leaf(obj, key)
    return new, new is not obj


_JSON_SPACE_RE = re.compile(r"[ \t\n\r]*")
_COMPACT = {"separators": (",", ":")}


def clean_tool_answer(text):
    """An upstream tool answer with its JSON string values cleaned (clean_json), still valid JSON.

    Not a str, not a JSON object or array, or not valid JSON: returned unchanged. Nothing to clean: the same
    object comes back. Otherwise re-dumped with ensure_ascii, with indent=2 when the original had a newline
    and compact separators when it did not, as upstream dumps. When the indented dump fails (nesting deeper
    than the pure-Python encoder's recursion limit, which py3.12's json.loads accepts), the compact dump is
    used, so a cleaned tree is never dropped for the raw text."""
    if not isinstance(text, str):
        return text
    start = _JSON_SPACE_RE.match(text).end()
    if text[start:start + 1] not in ("{", "["):
        return text
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError, TypeError):
        return text
    new, changed = clean_json(obj)
    if not changed:
        return text
    styles = ({"indent": 2}, _COMPACT) if "\n" in text else (_COMPACT,)
    for style in styles:
        try:
            return json.dumps(new, ensure_ascii=True, **style)
        except (ValueError, RecursionError, TypeError):
            continue
    return text
