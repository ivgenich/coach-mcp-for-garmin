"""Property-based tests (Hypothesis) for status_common's matching and path ids:

    python3 -m unittest discover -s ext -p "test_properties_*.py" -v

Skipped ("needs hypothesis") where hypothesis is not installed. Runs are
derandomized and keep no example database, so CI sees the same examples
every time.
"""

import datetime as dt
import itertools
import unittest
import uuid

import status_common as sc

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st
    HAVE_HYPOTHESIS = True
except ImportError:  # the classes below are skipped; the shim only lets the module load
    HAVE_HYPOTHESIS = False

    class _Absent:
        """Stands in for hypothesis.strategies: every attribute, call and | is itself."""

        def __getattr__(self, name):
            return self

        def __call__(self, *args, **kwargs):
            return self

        def __or__(self, other):
            return self

    st = HealthCheck = _Absent()

    def given(*args, **kwargs):
        def keep(fn):
            return fn
        return keep

    settings = given

NEEDS_HYPOTHESIS = "needs hypothesis"
PROPS = dict(max_examples=200, deadline=None, derandomize=True, database=None,
             suppress_health_check=[HealthCheck.too_slow])


# --- match_completion_shifts ----------------------------------------------------

BASE = dt.date(2026, 3, 18)
SPORTS = ("run", "run", "run", "bike", "swim", None)  # weighted to run so most pairs could match


def iso(offset):
    return (BASE + dt.timedelta(days=offset)).isoformat()


# A date is mostly a real day near BASE (close days, so windows overlap and matches compete); sometimes a day
# further out, None or a non-date string (the code compares those as text).
dates = st.integers(-2, 2).map(iso) | st.integers(-2, 2).map(iso) | st.integers(-5, 5).map(iso) \
    | st.sampled_from([None, "not-a-date"])
workouts = st.lists(st.fixed_dictionaries({
    "date": dates, "sport": st.sampled_from(SPORTS),
    "activity_id": st.none() | st.none() | st.integers(1, 8),
}), max_size=5)
activities = st.lists(st.fixed_dictionaries({
    "date": dates, "sport": st.sampled_from(SPORTS), "id": st.none() | st.integers(1, 8),
}), max_size=6)
windows = st.integers(0, 3)
# Crowded weeks: no Garmin links, 2-4 sessions each side over five days, so matches compete for activities and
# a greedy pass alone would sometimes complete fewer than possible.
close_days = st.integers(-2, 2).map(iso)
crowded_sports = st.sampled_from(("run", "run", "bike"))
crowded_workouts = st.lists(st.fixed_dictionaries({
    "date": close_days, "sport": crowded_sports, "activity_id": st.none()}), min_size=2, max_size=4)
crowded_activities = st.lists(st.fixed_dictionaries({
    "date": close_days, "sport": crowded_sports, "id": st.none()}), min_size=2, max_size=4)


def spec_gap(activity_day, planned_day):
    """activity - planned in days; text equality (0) for non-dates; None when not comparable."""
    try:
        return (dt.date.fromisoformat(activity_day) - dt.date.fromisoformat(planned_day)).days
    except (TypeError, ValueError):
        return 0 if activity_day is not None and activity_day == planned_day else None


def edges(scheduled, acts, early, late):
    """{(workout, activity): |gap|} the spec allows: same non-None sport, inside the window, activity not
    taken by a Garmin link, workout not Garmin-linked."""
    linked = {s["activity_id"] for s in scheduled if s["activity_id"] is not None}
    out = {}
    for n, w in enumerate(scheduled):
        if w["activity_id"] is not None:
            continue
        for i, a in enumerate(acts):
            if a["id"] is not None and a["id"] in linked:
                continue
            if a["sport"] is None or a["sport"] != w["sport"]:
                continue
            gap = spec_gap(a["date"], w["date"])
            if gap is None or gap < -early or gap > late:
                continue
            out[(n, i)] = abs(gap)
    return out


def brute_max_matching(left, edge_set):
    """Size of the largest matching, by trying every choice (inputs are tiny)."""
    best = 0

    def go(k, taken, size):
        nonlocal best
        best = max(best, size)
        if k == len(left) or size + (len(left) - k) <= best:
            return
        n = left[k]
        for (m, i) in edge_set:
            if m == n and i not in taken:
                go(k + 1, taken | {i}, size + 1)
        go(k + 1, taken, size)

    go(0, frozenset(), 0)
    return best


def realizable(shifts, allowed, workouts_left):
    """True when distinct activities can be given to the completed workouts with exactly their shifts."""
    done = [n for n in workouts_left if shifts[n] is not None]
    options = {n: [i for (m, i), g in allowed.items() if m == n and g == shifts[n]] for n in done}

    def go(k, taken):
        if k == len(done):
            return True
        return any(go(k + 1, taken | {i}) for i in options[done[k]] if i not in taken)

    return go(0, frozenset())


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class MatchCompletionShiftsProperties(unittest.TestCase):

    @settings(**PROPS)
    @given(workouts, activities, windows, windows)
    def test_shifts_are_a_maximum_matching_inside_the_rules(self, scheduled, acts, early, late):
        shifts = sc.match_completion_shifts(scheduled, acts, max_early_days=early, max_late_days=late)
        self.assertEqual(len(shifts), len(scheduled))
        free = [n for n, w in enumerate(scheduled) if w["activity_id"] is None]
        for n, w in enumerate(scheduled):
            if w["activity_id"] is not None:
                self.assertEqual(shifts[n], 0)  # linked by Garmin: done on its day
            elif shifts[n] is not None:
                self.assertGreaterEqual(shifts[n], 0)
                self.assertLessEqual(shifts[n], max(early, late))
        allowed = edges(scheduled, acts, early, late)
        # Same non-None sport, inside the window, each activity at most once (and never a Garmin-linked one).
        self.assertTrue(realizable(shifts, allowed, free), (scheduled, acts, early, late, shifts))
        # As many completed as any matching allows.
        done = sum(1 for n in free if shifts[n] is not None)
        self.assertEqual(done, brute_max_matching(free, set(allowed)), (scheduled, acts, early, late, shifts))

    @settings(**PROPS)
    @given(crowded_workouts, crowded_activities, st.integers(0, 2), st.integers(0, 2))
    def test_crowded_weeks_complete_as_many_as_possible(self, scheduled, acts, early, late):
        shifts = sc.match_completion_shifts(scheduled, acts, max_early_days=early, max_late_days=late)
        allowed = edges(scheduled, acts, early, late)
        everyone = list(range(len(scheduled)))
        self.assertTrue(realizable(shifts, allowed, everyone), (scheduled, acts, early, late, shifts))
        self.assertEqual(sum(s is not None for s in shifts), brute_max_matching(everyone, set(allowed)),
                         (scheduled, acts, early, late, shifts))

    @settings(**PROPS)
    @given(workouts, activities, windows)
    def test_symmetric_window_equals_max_shift_days(self, scheduled, acts, days):
        self.assertEqual(sc.match_completion_shifts(scheduled, acts, days),
                         sc.match_completion_shifts(scheduled, acts, max_early_days=days, max_late_days=days))

    @settings(**PROPS)
    @given(workouts, activities)
    def test_match_completions_is_same_day_shifts(self, scheduled, acts):
        self.assertEqual(sc.match_completions(scheduled, acts),
                         [s is not None for s in sc.match_completion_shifts(scheduled, acts, 0)])
        for done, w in zip(sc.match_completions(scheduled, acts), scheduled):
            if w["activity_id"] is not None:
                self.assertTrue(done)

    @settings(**PROPS)
    @given(workouts, activities, windows, windows, st.integers(0, 2), st.integers(0, 2))
    def test_a_wider_window_never_completes_fewer(self, scheduled, acts, early, late, more_early, more_late):
        def count(e, la):
            return sum(s is not None for s in sc.match_completion_shifts(scheduled, acts, max_early_days=e,
                                                                        max_late_days=la))
        self.assertGreaterEqual(count(early + more_early, late + more_late), count(early, late))

    @settings(**PROPS)
    @given(workouts, activities, windows, windows, st.randoms(use_true_random=False))
    def test_count_does_not_depend_on_activity_order(self, scheduled, acts, early, late, rnd):
        shuffled = list(acts)
        rnd.shuffle(shuffled)

        def count(a):
            return sum(s is not None for s in sc.match_completion_shifts(scheduled, a, max_early_days=early,
                                                                        max_late_days=late))
        self.assertEqual(count(shuffled), count(acts))

    @settings(**PROPS)
    @given(workouts, activities, windows, windows)
    def test_inputs_are_not_changed(self, scheduled, acts, early, late):
        before = (repr(scheduled), repr(acts))
        sc.match_completion_shifts(scheduled, acts, max_early_days=early, max_late_days=late)
        self.assertEqual((repr(scheduled), repr(acts)), before)


class MatchCompletionShiftsOracle(unittest.TestCase):
    """The brute-force oracle above agrees with hand-checked cases (so the properties test something)."""

    def test_docstring_case(self):
        scheduled = [{"date": iso(-1), "sport": "run", "activity_id": None},   # Tue
                     {"date": iso(0), "sport": "run", "activity_id": None}]    # Wed
        acts = [{"date": iso(-2), "sport": "run", "id": 1}, {"date": iso(-1), "sport": "run", "id": 2}]  # Mon, Tue
        allowed = edges(scheduled, acts, 1, 2)
        self.assertEqual(brute_max_matching([0, 1], set(allowed)), 2)
        self.assertEqual(sc.match_completion_shifts(scheduled, acts, max_early_days=1, max_late_days=2), [1, 1])

    def test_every_small_case_exhaustively(self):
        # All 2-workout x 2-activity layouts over 3 days and 2 sports, windows 0..1: the code is maximum.
        days = [iso(d) for d in (-1, 0, 1)]
        for wd, ad, ws_, as_ in itertools.product(itertools.product(days, repeat=2), itertools.product(days, repeat=2),
                                                  itertools.product(("run", "bike"), repeat=2),
                                                  itertools.product(("run", "bike"), repeat=2)):
            scheduled = [{"date": d, "sport": s, "activity_id": None} for d, s in zip(wd, ws_)]
            acts = [{"date": d, "sport": s, "id": k + 1} for k, (d, s) in enumerate(zip(ad, as_))]
            for early, late in ((0, 0), (1, 0), (0, 1), (1, 1)):
                shifts = sc.match_completion_shifts(scheduled, acts, max_early_days=early, max_late_days=late)
                best = brute_max_matching([0, 1], set(edges(scheduled, acts, early, late)))
                self.assertEqual(sum(s is not None for s in shifts), best, (scheduled, acts, early, late))


# --- path_int_id / path_uuid ---------------------------------------------------

ASCII_DIGITS = "0123456789"
HEX = set("0123456789abcdefABCDEF")
# Characters that look like digits or hex but are not ASCII: superscripts, full-width, Arabic-Indic, Devanagari,
# a fraction, circled digits, and full-width Latin a-f.
LOOKALIKES = "²³¹⁰⁴₀０１９٠١۱०½①ａｆ"


def spec_int_id(text):
    return int(text) if 1 <= len(text) <= 19 and all(c in ASCII_DIGITS for c in text) and int(text) > 0 else None


def spec_uuid(text, dashless):
    if len(text) == 36 and all((c == "-") if k in (8, 13, 18, 23) else (c in HEX) for k, c in enumerate(text)):
        return text
    if dashless and len(text) == 32 and all(c in HEX for c in text):
        return text
    return None


ascii_ids = st.text(alphabet=ASCII_DIGITS, min_size=1, max_size=21)
near_ids = st.text(alphabet=ASCII_DIGITS + " +-_.,\n\t" + LOOKALIKES, max_size=21)
uuids = st.uuids().map(str)


@st.composite
def damaged_uuid(draw):
    """A canonical UUID with one character replaced, one inserted, or wrapping added."""
    text = draw(uuids)
    if draw(st.booleans()):
        text = text.upper()
    how = draw(st.sampled_from(("replace", "insert", "wrap")))
    k = draw(st.integers(0, len(text) - 1))
    bad = draw(st.sampled_from(list(LOOKALIKES) + list(" \n\t{}-+gGzZ/.%")))
    if how == "replace":
        return text[:k] + bad + text[k + 1:]
    if how == "insert":
        return text[:k] + bad + text[k:]
    return draw(st.sampled_from(("{%s}", "urn:uuid:%s", " %s", "%s\n", "%s "))) % text


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class PathIdProperties(unittest.TestCase):

    @settings(**PROPS)
    @given(ascii_ids | near_ids | st.text(max_size=24))
    def test_int_id_takes_only_ascii_digit_strings(self, text):
        self.assertEqual(sc.path_int_id(text), spec_int_id(text), repr(text))

    @settings(**PROPS)
    @given(st.integers(-(2 ** 70), 2 ** 70))
    def test_int_id_takes_only_positive_ints(self, value):
        self.assertEqual(sc.path_int_id(value), value if value > 0 else None)

    @settings(**PROPS)
    @given(st.none() | st.booleans() | st.floats(allow_nan=True) | st.binary(max_size=8)
           | st.lists(st.integers(1, 9), max_size=2) | st.decimals(allow_nan=False, allow_infinity=False))
    def test_int_id_refuses_other_types(self, value):
        self.assertIsNone(sc.path_int_id(value))

    @settings(**PROPS)
    @given(st.text(alphabet=LOOKALIKES, min_size=1, max_size=6), ascii_ids)
    def test_int_id_refuses_any_lookalike_digit(self, odd, digits):
        for text in (odd, digits + odd, odd + digits, digits[:1] + odd + digits[1:]):
            self.assertIsNone(sc.path_int_id(text), repr(text))

    @settings(**PROPS)
    @given(uuids, st.booleans(), st.booleans())
    def test_uuid_takes_canonical_uuids_unchanged(self, text, upper, dashless):
        text = text.upper() if upper else text
        self.assertIs(sc.path_uuid(text, dashless=dashless), text)
        bare = text.replace("-", "")
        self.assertEqual(sc.path_uuid(bare, dashless=dashless), bare if dashless else None)

    @settings(**PROPS)
    @given(damaged_uuid(), st.booleans())
    def test_uuid_refuses_a_damaged_uuid(self, text, dashless):
        self.assertEqual(sc.path_uuid(text, dashless=dashless), spec_uuid(text, dashless), repr(text))

    @settings(**PROPS)
    @given(st.text(alphabet="0123456789abcdefABCDEF-" + LOOKALIKES + " \n", max_size=40) | st.text(max_size=40),
           st.booleans())
    def test_uuid_matches_the_spec_on_any_text(self, text, dashless):
        self.assertEqual(sc.path_uuid(text, dashless=dashless), spec_uuid(text, dashless), repr(text))

    @settings(**PROPS)
    @given(st.uuids() | st.none() | st.integers() | st.binary(min_size=16, max_size=16))
    def test_uuid_refuses_non_strings(self, value):
        self.assertIsNone(sc.path_uuid(value))
        self.assertIsNone(sc.path_uuid(value, dashless=True))


class PathIdExamples(unittest.TestCase):
    """Fixed cases from the brief, run with or without hypothesis."""

    def test_int_id_lookalikes_and_signs(self):
        for text in ("²", "１２", "١٢", "+5", "-5", " 5", "5 ", "5\n", "1_000", "0", "00",
                     "", "1" * 20, "5.0", "0x1f"):
            with self.subTest(text=text):
                self.assertIsNone(sc.path_int_id(text))
        self.assertEqual(sc.path_int_id("0007"), 7)
        self.assertEqual(sc.path_int_id("9" * 19), int("9" * 19))

    def test_uuid_lookalikes(self):
        good = str(uuid.UUID(int=0x1234_5678_9ABC_DEF0_1234_5678_9ABC_DEF0))
        self.assertEqual(sc.path_uuid(good), good)
        for text in (good + "\n", "{" + good + "}", good.replace("a", "ａ", 1), good[:-1] + "²",
                     good.replace("-", "‐", 1), " " + good):
            with self.subTest(text=text):
                self.assertIsNone(sc.path_uuid(text))
                self.assertIsNone(sc.path_uuid(text, dashless=True))


if __name__ == "__main__":
    unittest.main()
