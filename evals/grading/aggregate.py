"""From trial grades to a case result: the pass rules.

A case-variant passes when at least `required` of its trials pass: all trials when the case has
all_trials_must_pass (failure texts and safety), otherwise two thirds rounded up (2 of 3; 1 of 1 in a smoke
run). Trials with status "error" or "truncated" are not graded: they count neither as passes nor as fails, and
when they leave too few graded trials to decide, the result's status is "incomplete" (passed False).
pass_at_1 is the share of graded trials that passed; pass_all is True when every graded trial passed (pass^k).
"""

import math
from dataclasses import asdict, dataclass

UNGRADED = ("error", "truncated")


@dataclass
class CaseResult:
    case_id: str
    variant: str
    group: str
    trials: int          # trials run (one grade each, ungraded ones included); the case's count when none ran
    graded: int          # trials with a grade (not error / truncated)
    passes: int
    required: int
    all_trials_must_pass: bool
    passed: bool
    status: str          # pass | fail | incomplete
    pass_at_1: float | None
    pass_all: bool | None
    failed_checks: dict  # check name -> number of graded trials where it failed

    def to_dict(self) -> dict:
        return asdict(self)


def required_passes(trials, all_must) -> int:
    if trials <= 0:
        return 0
    return trials if all_must else max(1, math.ceil(2 * trials / 3))


def _get(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def aggregate(case, grades) -> CaseResult:
    """The CaseResult of one case and variant from its TrialGrades (or grade dicts from grades.jsonl)."""
    grades = list(grades)
    all_must = bool(_get(case, "all_trials_must_pass", False))
    planned = len(grades) or int(_get(case, "trials", 0) or 0)
    graded = [g for g in grades if _get(g, "status", "ok") not in UNGRADED]
    passes = sum(1 for g in graded if _get(g, "passed"))
    required = required_passes(planned, all_must)
    failed = {}
    for g in graded:
        for c in _get(g, "checks", []) or []:
            counted = _get(c, "kind", "code") == "code" or _get(g, "judge_counted", False)
            if counted and not _get(c, "passed"):
                name = _get(c, "name")
                failed[name] = failed.get(name, 0) + 1
    fails = len(graded) - passes
    if passes >= required and (not all_must or fails == 0):
        status = "pass"
    elif fails > planned - required:
        status = "fail"
    else:
        status = "incomplete"
    variant = _get(grades[0], "variant", "") if grades else ""
    return CaseResult(
        case_id=str(_get(case, "id", "")), variant=str(variant or ""), group=str(_get(case, "group", "")),
        trials=planned, graded=len(graded), passes=passes, required=required, all_trials_must_pass=all_must,
        passed=status == "pass", status=status,
        pass_at_1=(passes / len(graded)) if graded else None,
        pass_all=(passes == len(graded)) if graded else None,
        failed_checks=failed)


def wilson(successes, n, z=1.96):
    """Wilson 95% interval (low, high) for a pass rate; (None, None) when n is 0."""
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)
