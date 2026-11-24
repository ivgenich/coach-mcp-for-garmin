"""Grading of eval trials: deterministic checks first, an LLM judge where meaning matters.

    grade(case, transcript) -> TrialGrade(checks=[Check(name, passed, detail, kind)], passed)
    judge(case, transcript, client, model) -> [Check(kind="judge")]
    aggregate(case, grades) -> CaseResult (pass rules)
    grade_run(run_dir, cases, client=None) -> rows written to grades.jsonl
    summarize(run_dir) -> writes summary.json + summary.md
    compare(base_dir, new_dir) -> text

Modules: numbers (no invented numbers), errors (error-text rules from ext/tool_errors.py), checks, judge,
aggregate, report. Standard library only, except that judge() takes an Anthropic client as an argument.
"""

from evals.grading.aggregate import CaseResult, aggregate, wilson
from evals.grading.checks import Check, TrialGrade, code_checks, grade
from evals.grading.judge import DEFAULT_JUDGE_MODEL, is_calibrated, judge
from evals.grading.report import build_summary, compare, flips, grade_run, summarize

__all__ = ["CaseResult", "Check", "DEFAULT_JUDGE_MODEL", "TrialGrade", "aggregate", "build_summary",
           "code_checks", "compare", "flips", "grade", "grade_run", "is_calibrated", "judge", "summarize",
           "wilson"]
