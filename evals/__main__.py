"""The eval command line: python -m evals <command>. See docs/evals.md.

    run [--smoke] [--cases ID,..] [--variants ..] [--model ..] [--trials N] [--max-usd X]
        [--judge-model ..] [--effort ..] [--run-id ..]      live: needs ANTHROPIC_API_KEY
    grade <run_dir> [--out DIR]                               offline: code checks again
    compare <base_dir> <new_dir>                              offline: flips and deltas
    label <run_dir>                                           hand labels for the judge
    list                                                      the cases

Exit status: 0 done; 1 a step failed (missing grading, an unknown case, a trial error
in grade); 2 bad usage or a refused live run; 3 the run stopped at --max-usd.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import shutil
import sys
from datetime import datetime, timezone

from evals import cost as cost_mod
from evals import runner
from evals import tracing

ROOT = runner.ROOT
CASES_DIR = os.path.join(ROOT, "evals", "cases")
RUNS_DIR = os.path.join(ROOT, "evals", "runs")
DEFAULT_JUDGE = "claude-haiku-5-5"  # yes/no rubric items only; calibration (evals/judge) shows if it is good enough
ALT_JUDGE = "claude-sonnet-5-5"  # the judge when the model under test is DEFAULT_JUDGE
DEFAULT_MAX_USD = 5.0
DEFAULT_TRIALS = 1  # --trials 3 for a release baseline
KEY_ENV = "ANTHROPIC_API_KEY"


def say(msg: str) -> None:
    print(scrub(msg), flush=True)


def scrub(text: str) -> str:
    """text with the API key (if one is in the environment) replaced: nothing written or printed may hold it."""
    key = os.environ.get(KEY_ENV) or ""
    return text.replace(key, "[redacted]") if len(key) >= 8 else text


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(scrub(json.dumps(obj, indent=2, ensure_ascii=False, default=str)) + "\n")


def write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(scrub(json.dumps(row, ensure_ascii=False, default=str)) + "\n")


def transcript_name(case_id: str, variant: str, trial: int) -> str:
    return f"{case_id}__{variant}__t{trial}.json"


def load_cases(path: str = CASES_DIR):
    from evals.cases import load_cases as _load

    return _load(path)


def _grading():
    """evals.grading, or None (with the reason printed) when it cannot be imported."""
    try:
        import evals.grading as grading
    except ImportError as e:
        say(f"grading skipped: evals.grading not importable ({e})")
        return None
    return grading


# --- run ------------------------------------------------------------------------------

def select(cases, ids: list[str] | None, smoke: bool, variants: list[str] | None, trials: int | None):
    """[(case, variants, trials)] for this run; unknown ids raise ValueError."""
    by_id = {c.id: c for c in cases}
    if ids:
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ValueError(f"unknown case id(s): {', '.join(missing)}")
        cases = [by_id[i] for i in ids]
    if smoke:
        cases = [c for c in cases if c.smoke]
    plan = []
    for c in cases:
        vs = [v for v in c.variants if not variants or v in variants]
        if vs:
            plan.append((c, vs, trials if trials is not None else (1 if smoke else c.trials)))
    return plan


def _anthropic_client():
    import anthropic

    # The SDK retries 408/409/429/5xx (529 overloaded included) with exponential backoff.
    return anthropic.Anthropic(max_retries=8, timeout=600.0)


def cmd_run(args, client_factory=None, harness=None) -> int:
    model = args.model
    judge_model = args.judge_model or (ALT_JUDGE if model == DEFAULT_JUDGE else DEFAULT_JUDGE)
    if judge_model == model:
        say(f"refused: the judge model must not be the model under test ({model})")
        return 2
    try:
        cost_mod.price(model)
        cost_mod.price(judge_model)
    except cost_mod.UnknownModel as e:
        say(f"refused: {e}")
        return 2
    if args.variants and not set(args.variants) <= set(runner.VARIANTS):
        say(f"refused: unknown variant in {','.join(args.variants)}; known: {', '.join(runner.VARIANTS)}")
        return 2
    if not os.environ.get(KEY_ENV):
        say(f"refused: a live run needs {KEY_ENV} in the environment (never as an argument). "
            "Offline commands (grade, compare, list) need no key.")
        return 2
    for problem in tracing.problems():
        say(f"refused: {problem}")
        return 2
    try:
        plan = select(load_cases(args.cases_dir), args.cases, args.smoke, args.variants, args.trials)
    except ValueError as e:
        say(f"refused: {e}")
        return 2
    if not plan:
        say("nothing to run: no case matches")
        return 2

    run_id = args.run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{model}"
    run_dir = os.path.join(args.runs_dir, run_id)
    if os.path.exists(run_dir):
        say(f"refused: {run_dir} exists")
        return 2
    budget = cost_mod.Budget(max_usd=args.max_usd)
    client = cost_mod.MeteredClient(tracing.wrap_client((client_factory or _anthropic_client)()), budget)
    if tracing.setup():
        tracing.set_run(run_id)
        say("eval traces: on (docs/tracing.md, Eval runs)")
    coach = runner.read_coach_prompt() if any("coach" in vs for _, vs, _ in plan) else None
    config = {
        "run_id": run_id, "model": model, "judge_model": judge_model, "effort": args.effort,
        "max_usd": args.max_usd, "smoke": args.smoke, "max_turns": runner.MAX_TURNS,
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plan": [{"case_id": c.id, "variants": vs, "trials": n} for c, vs, n in plan],
        "sdk": _sdk_version(), "commit": os.environ.get("GITHUB_SHA", ""), "complete": False,
    }
    write_json(os.path.join(run_dir, "config.json"), config)
    total = sum(len(vs) * n for _, vs, n in plan)
    say(f"run {run_id}: {total} trials, model {model}, judge {judge_model}, cap ${args.max_usd:.2f}")

    done, stopped = [], False
    for case, vs, n in plan:
        for variant in vs:
            for trial in range(1, n + 1):
                tr = runner.run_trial(client, case, variant, trial, model, effort=args.effort,
                                      max_turns=getattr(case, "max_turns", None) or runner.MAX_TURNS,
                                      harness=harness, coach_prompt=coach)
                write_json(os.path.join(run_dir, "transcripts", transcript_name(case.id, variant, trial)), tr)
                say(f"{case.id} {variant} t{trial}: {tr['stop_reason']}, {len(tr['tool_calls'])} tool calls, "
                    f"${tr['cost_usd']:.4f}" + (f", error: {tr['error']}" if tr["error"] else ""))
                if tr["stop_reason"] == "budget":
                    stopped = True
                    break
                done.append((case, tr))
            if stopped:
                break
        if stopped:
            break

    grading = _grading()
    if grading is not None and done:
        cases = {c.id: c for c, _, _ in plan}
        needs_judge = any(getattr(getattr(c, "expect", None), "judge", None) for c in cases.values())
        try:
            rows = grading.grade_run(run_dir, cases, client=client if needs_judge and not stopped else None,
                                     judge_model=judge_model)
        except cost_mod.BudgetExceeded as e:
            say(f"judge stopped: {e}; grading with code checks only")
            stopped = True
            rows = grading.grade_run(run_dir, cases, client=None, judge_model=judge_model)
        if any(str(r.get("judge_error") or "").startswith("BudgetExceeded") for r in rows):
            say("judge stopped at --max-usd: some trials have no judge checks (judge_error)")
            stopped = True
        grading.summarize(run_dir)

    tracing.shutdown()
    config.update(complete=not stopped, spent_usd=round(budget.spent, 6), api_calls=budget.calls,
                  finished=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    write_json(os.path.join(run_dir, "config.json"), config)
    say(f"spent ${budget.spent:.4f} in {budget.calls} API calls; results in {run_dir}")
    if stopped:
        say(f"incomplete: stopped at --max-usd {args.max_usd:.2f}")
        return 3
    errored = [tr for _, tr in done if tr.get("error")]
    if errored:
        say(f"{len(errored)} of {len(done)} trials ended in an error (see each transcript's error): "
            "those measured nothing")
        return 1
    return 0


def _sdk_version() -> str:
    try:
        import anthropic

        return anthropic.__version__
    except Exception:
        return ""


# --- offline commands ----------------------------------------------------------------

def _old_grades(path: str) -> dict:
    """The rows of an existing grades.jsonl by transcript file name ({} when there is none)."""
    rows = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    rows[row.get("transcript") or transcript_name(row["case_id"], row["variant"], row["trial"])] = row
    return rows


def cmd_grade(args) -> int:
    """Code checks again over a run's transcripts, offline. The judge is not called again: the source run's
    judge checks (and judge_* fields) are carried over into the new rows."""
    run_dir = args.run_dir
    if args.out:
        if os.path.exists(args.out):
            say(f"refused: {args.out} exists")
            return 2
        shutil.copytree(run_dir, args.out)
        run_dir = args.out
    tdir = os.path.join(run_dir, "transcripts")
    if not os.path.isdir(tdir):
        say(f"no transcripts in {run_dir}")
        return 1
    grading = _grading()
    if grading is None:
        return 1
    cases = {c.id: c for c in load_cases(args.cases_dir)}
    unknown = []
    for name in sorted(os.listdir(tdir)):
        if name.endswith(".json"):
            with open(os.path.join(tdir, name), encoding="utf-8") as f:
                case_id = json.load(f).get("case_id")
            if case_id not in cases:
                unknown.append(f"{name}: unknown case {case_id!r}")
    for line in unknown:
        say(line)
    gpath = os.path.join(run_dir, "grades.jsonl")
    old = _old_grades(gpath)
    rows = grading.grade_run(run_dir, cases, client=None, write=False)
    check_fields = {f.name for f in dataclasses.fields(grading.Check)}
    carried = 0
    for row in rows:
        prev = old.get(row.get("transcript") or transcript_name(row["case_id"], row["variant"], row["trial"]))
        if not prev:
            continue
        judged = [grading.Check(**{k: v for k, v in c.items() if k in check_fields})
                  for c in prev.get("checks", []) if c.get("kind") == "judge"]
        if judged:
            with open(os.path.join(tdir, row["transcript"]), encoding="utf-8") as f:
                tr = json.load(f)
            row.update(grading.grade(cases[row["case_id"]], tr, judged).to_dict())
            carried += 1
        row.update({k: v for k, v in prev.items() if k.startswith("judge_")})
    with open(gpath, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(scrub(json.dumps(row, ensure_ascii=False, sort_keys=True)) + "\n")
    grading.summarize(run_dir)
    passed = sum(1 for r in rows if r.get("passed"))
    say(f"graded {len(rows)} trials in {run_dir}: {passed} passed; judge checks carried over for {carried}")
    return 1 if unknown else 0


def cmd_compare(args) -> int:
    grading = _grading()
    if grading is None:
        return 1
    out = grading.compare(args.base, args.new)
    say(out if isinstance(out, str) else "\n".join(str(x) for x in out))
    return 0


def cmd_label(args) -> int:
    try:
        from evals.judge import label
    except ImportError as e:
        say(f"labelling tool not importable: {e}")
        return 1
    if hasattr(label, "main"):
        return int(label.main([args.run_dir]) or 0)
    return int(label.label(args.run_dir) or 0)


def cmd_list(args) -> int:
    for c in load_cases(args.cases_dir):
        say(f"{c.id:5} {c.group:16} {c.mode:4} {','.join(c.variants):18} {'smoke' if c.smoke else '     '} "
            f"x{c.trials}  {c.title}")
    return 0


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evals", description="Model-behaviour evals (docs/evals.md).")
    p.add_argument("--cases-dir", default=CASES_DIR, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="live run (needs ANTHROPIC_API_KEY)")
    r.add_argument("--smoke", action="store_true", help="only the smoke cases, 1 trial each")
    r.add_argument("--cases", type=_csv, help="case ids, comma separated")
    r.add_argument("--variants", type=_csv, help="bare,instr,coach (default: each case's own)")
    r.add_argument("--model", default=runner.DEFAULT_MODEL)
    r.add_argument("--judge-model", default=None,
                   help=f"default {DEFAULT_JUDGE} ({ALT_JUDGE} when the model under test is {DEFAULT_JUDGE})")
    r.add_argument("--trials", type=int, default=DEFAULT_TRIALS,
                   help=f"trials per case and variant (default {DEFAULT_TRIALS}; 3 for a release baseline)")
    r.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD, help="stop before the run costs more")
    r.add_argument("--effort", default=runner.DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    r.add_argument("--runs-dir", default=RUNS_DIR, help=argparse.SUPPRESS)
    g = sub.add_parser("grade", help="grade a run's transcripts again, offline")
    g.add_argument("run_dir")
    g.add_argument("--out", default=None, help="copy the run here first and grade the copy")
    c = sub.add_parser("compare", help="cases that flipped between two graded runs")
    c.add_argument("base")
    c.add_argument("new")
    lab = sub.add_parser("label", help="hand-label a run's trials for judge calibration")
    lab.add_argument("run_dir")
    sub.add_parser("list", help="list the cases")
    return p


def main(argv: list[str] | None = None, client_factory=None, harness=None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == "run":
        if args.trials is not None and args.trials < 1:
            say("refused: --trials must be 1 or more")
            return 2
        if args.max_usd is not None and (not math.isfinite(args.max_usd) or args.max_usd <= 0):
            say("refused: --max-usd must be more than 0")
            return 2
        return cmd_run(args, client_factory, harness)
    try:
        return {"grade": cmd_grade, "compare": cmd_compare, "label": cmd_label, "list": cmd_list}[args.cmd](args)
    except ValueError as e:  # evals.cases.CaseError: a case file that does not load
        say(f"cases: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
