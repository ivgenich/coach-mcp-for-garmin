"""What an answer must say after each error group, read from ext/tool_errors.py at import time.

The anchor words of every rule are taken out of the server's own texts (tool_errors.client_text(),
GROUP_PHRASE, NO_SLEEP_TEXT, DATE_DETAIL) by pattern, so a change to those texts changes the rules, and a text
that no longer holds an anchor makes this module fail at import (a test catches the drift). A few plain
paraphrases are added to each rule, because a good answer explains the error in its own words.

RULES[(group, mode)] is a Rule: `any_of` (the answer must match at least one) and `none_of` (it must match
none; key mode must not name the full-mode sign-in command). INTERNAL is special: the answer must contain the
ref the tool result carried (ref_in()).

result_group(text, is_error) names the group of a tool result text, or None for a normal result.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

_EXT = Path(__file__).resolve().parents[2] / "ext"
if str(_EXT) not in sys.path:
    sys.path.insert(0, str(_EXT))

import tool_errors as te  # noqa: E402  (ext/ is not a package; its modules import each other by name)

_I = re.IGNORECASE

LOGIN, RATE_LIMITED, BAD_INPUT, NO_DATA, UNAVAILABLE, INTERNAL = (
    te.LOGIN, te.RATE_LIMITED, te.BAD_INPUT, te.NO_DATA, te.UNAVAILABLE, te.INTERNAL)
GROUPS = te.GROUPS
# Groups after which no further Garmin tool call is allowed in the trial (the texts say retrying will not help /
# stop calling Garmin tools).
STOP_GROUPS = (LOGIN, RATE_LIMITED)
# Groups whose text allows one retry of the same call ("Try again in a few minutes", "Retrying once later").
ONE_RETRY_GROUPS = (UNAVAILABLE, INTERNAL)


def _need(rx, text, what):
    m = re.search(rx, text, _I)
    if m is None:
        raise ImportError(f"evals.grading.errors: tool_errors text changed, cannot find {what!r} in {text!r}")
    return m


def _words(phrase):
    """A regex for `phrase` that allows any run of spaces or a hyphen between its words."""
    return r"\b" + r"[\s-]+".join(re.escape(w) for w in phrase.split()) + r"\b"


@dataclass(frozen=True)
class Rule:
    any_of: tuple
    none_of: tuple = ()
    anchors: tuple = ()  # the words taken from the server's texts (for reports and tests)


def _build():
    rules = {}
    full_login = te.client_text(LOGIN)
    key_login = te.client_text(LOGIN, key_mode=True)
    cmd = _need(r"run (\S+) on", full_login, "run <command> on").group(1)
    sign_m = _need(r"\b(sign)s? in\b", full_login, "signs in")
    sign = sign_m.group(1)
    restart = _need(r"\b(restart)\b", full_login, "restart").group(1)
    key_remedy = _need(r"new (Personal API key)", key_login, "new Personal API key").group(1)
    full_only = tuple(t for t in (cmd,) if t.lower() not in key_login.lower())
    # A remedy, not the noun "login": sign / log in again or back in, someone must or needs to sign in,
    # re-authenticate, the sign-in command, or restart.
    verb = rf"(?:{sign}|log)(?:s|ing|ed|ged|ging)?[\s-]?(?:in|on)"
    full_any = (re.escape(cmd),
                rf"\b{verb}\b(?:\s+(?:to|into)\s+(?:\w+\s+){{0,2}}?)?\s*(?:again|back)\b",
                rf"\b(?:{sign}|log)(?:s|ged|ed)?\s+back\s+in\b",
                rf"\b(?:must|needs?\s+to|has\s+to|have\s+to|should|will\s+need\s+to|ask\w*\s+\w+\s+to)\s+"
                rf"(?:\w+\s+){{0,2}}?{verb}\b",
                r"\bre-?auth\w*", r"\bre-?(?:sign|log)[\s-]?in\b", rf"\b{restart}")
    rules[(LOGIN, "full")] = Rule(any_of=full_any, anchors=(cmd, sign_m.group(0), restart))
    key_words = _words(key_remedy)[2:]  # "Personal API key" without the leading \b
    key_any = (rf"\bnew\s+(?:garmin\s+)?{key_words}", r"\bnew\s+(?:garmin\s+)?api[\s-]?key\b", r"\bnew key\b",
               r"\b(?:replace|regenerate|renew|create|make|generate|issue|set up)\w*\s+(?:\w+\s+){0,4}?"
               r"(?:api[\s-]?)?key\b",
               r"\b(?:api[\s-]?)?key\b[^.\n]{0,40}\b(?:replaced|regenerated|renewed|recreated)\b")
    rules[(LOGIN, "key")] = Rule(any_of=key_any, none_of=tuple(re.escape(t) for t in full_only),
                                 anchors=(key_remedy,))

    minutes = _need(r"(\d+) minutes", te.client_text(RATE_LIMITED), "<n> minutes").group(1)
    rl = (rf"\b{minutes}[\s-]*(?:min|mins|minutes?)\b",)  # "15 minutes", "a 15-minute wait"
    if minutes == "15":
        rl += (r"\bquarter of an hour\b",)
    rules[(RATE_LIMITED, "full")] = rules[(RATE_LIMITED, "key")] = Rule(any_of=rl, anchors=(minutes + " minutes",))

    invalid = _need(r"^(\w+)", te.GROUP_PHRASE[BAD_INPUT], "Invalid").group(1)
    real = _need(r"\b(real) calendar dates?\b", te.DATE_DETAIL, "real calendar dates").group(1)
    bad = (rf"\b{invalid}\b", rf"\bnot an? {real}\b", rf"\bisn't an? {real}\b", rf"\b{real} (?:calendar )?date",
           r"\bnot (?:a )?valid\b", r"\bisn'?t (?:a )?valid\b", r"\bdoes(?:n'?t| not) exist\b", r"\bno such date\b",
           r"\bonly (?:has|had) \d+ days\b")
    rules[(BAD_INPUT, "full")] = rules[(BAD_INPUT, "key")] = Rule(any_of=bad, anchors=(invalid, real))

    no_data = te.GROUP_PHRASE[NO_DATA].rstrip(":").strip()
    sync = _need(r"\b(sync)\b", te.NO_SLEEP_TEXT, "sync").group(1)
    nd = (_words(no_data), rf"\b{sync}(?:s|ed|ing|hronis\w*|hroniz\w*)?\b", r"\bnothing (?:was )?recorded\b",
          r"\bnot (?:been )?recorded\b", r"\bno sleep (?:data|recorded|record)\b", r"\bhas nothing\b",
          r"\bno (?:record|entry|value)s?\b", r"\bnot available yet\b")
    rules[(NO_DATA, "full")] = rules[(NO_DATA, "key")] = Rule(any_of=nd, anchors=(no_data, sync))

    m = _need(r"(Try again) in (a few minutes)", te.client_text(UNAVAILABLE), "Try again in a few minutes")
    un = (_words(m.group(1)), _words(m.group(2).replace("a ", "", 1)), r"\blater\b", r"\bretry\b")
    rules[(UNAVAILABLE, "full")] = rules[(UNAVAILABLE, "key")] = Rule(any_of=un, anchors=(m.group(1), m.group(2)))

    probe = te.client_text(INTERNAL, ref="0badc0de")
    _need(r"\(ref 0badc0de\)", probe, "(ref <ref>)")
    rules[(INTERNAL, "full")] = rules[(INTERNAL, "key")] = Rule(any_of=(), anchors=("ref",))
    return rules


RULES = _build()
_REF_RE = re.compile(r"\bref ([0-9a-f]{8})\b")
_ERROR_HEADS = {g: "Error: " + te.GROUP_PHRASE[g] for g in GROUPS if g != NO_DATA}
_NO_DATA_HEAD = te.GROUP_PHRASE[NO_DATA]


def result_group(text, is_error=None):
    """The error group of a tool result text, or None for a normal answer. Reads the fixed first words the
    server writes (tool_errors.GROUP_PHRASE): "Error: <phrase>" for the five error groups, "No data:" for
    no_data (which is not an error result)."""
    if not isinstance(text, str):
        return None
    head = text.lstrip()
    for group, start in _ERROR_HEADS.items():
        if head.startswith(start):
            return group
    if head.startswith(_NO_DATA_HEAD):
        return NO_DATA
    if is_error and te.is_failure_text(head):
        return te.classify_text(head)[0]
    return None


def ref_in(texts):
    """The first internal-error ref (8 hex characters after "ref ") in `texts`, or None."""
    for text in texts:
        if isinstance(text, str):
            m = _REF_RE.search(text)
            if m:
                return m.group(1)
    return None


def follows(group, mode, answer, tool_texts=()):
    """(passed, detail): does `answer` follow the instruction of `group`'s text for `mode` ("full"|"key")?"""
    answer = answer if isinstance(answer, str) else ""
    if group == INTERNAL:
        ref = ref_in(tool_texts)
        if ref is None:
            return False, "no internal-error ref in the tool results"
        if ref in answer.lower():
            return True, f"answer gives the ref {ref}"
        return False, f"answer does not give the ref {ref}"
    rule = RULES.get((group, "key" if mode == "key" else "full"))
    if rule is None:
        return False, f"unknown error group {group!r}"
    bad = [rx for rx in rule.none_of if re.search(rx, answer, _I)]
    if bad:
        return False, f"answer gives the other mode's remedy ({', '.join(bad)})"
    hit = next((rx for rx in rule.any_of if re.search(rx, answer, _I)), None)
    if hit is None:
        return False, f"answer does not say what the {group} text asks (anchors: {', '.join(rule.anchors)})"
    return True, f"answer follows the {group} text (matched {hit})"
