"""Grade a run folder, summarise it and compare two runs.

grade_run(run_dir, cases, client=None) grades every transcripts/*.json (judge items too when a client is given)
and writes grades.jsonl: one JSON object per trial, the TrialGrade fields plus group, model,
all_trials_must_pass, usage, cost_usd, duration_s, tool and Garmin call counts, judge_model and judge_usage.

summarize(run_dir) reads grades.jsonl (and config.json when present) and writes summary.json and summary.md:
the headline (case-variant pass rate and trial pass@1, each with a Wilson 95% interval), per group, per variant,
pass^k of the all-trials cases, tokens, cost, the worst cases, and the cases that flipped against a baseline
(config.json "baseline", or the `baseline_dir` argument).

compare(base_dir, new_dir) returns text: cases that flipped (pass -> fail first), cases in one run only, and the
deltas of the headline, per group, per variant, tokens and cost. Paired per case, as the noise floor of a
headline (about +-9 points at 120 trials) hides real changes.
"""

import json
from pathlib import Path

from evals import cost as cost_mod
from evals.grading import errors as errors_mod
from evals.grading.aggregate import aggregate, wilson
from evals.grading.checks import grade, tool_calls
from evals.grading.judge import DEFAULT_JUDGE_MODEL, judge

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def read_grades(run_dir) -> list:
    path = Path(run_dir) / "grades.jsonl"
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except OSError:
        return []
    return rows


def _attr(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def scrub(text) -> str:
    """One line of at most 300 characters, with keys, bearer tokens, addresses and long tokens hidden
    (tool_errors.redact)."""
    return errors_mod.te.redact(text, 300)


def judge_cost(model, usage):
    """US dollars of one judge call (evals.cost prices), or None without usage or a price for the model."""
    if not model or not usage:
        return None
    try:
        return round(cost_mod.usage_cost(model, usage), 6)
    except cost_mod.UnknownModel:
        return None


def grade_run(run_dir, cases, client=None, judge_model=DEFAULT_JUDGE_MODEL, write=True) -> list:
    """Grade every transcript of `run_dir` against `cases` (a list of Case or {id: Case}); write grades.jsonl
    when `write`. With `client`, cases that list judge items get a judge call per graded trial. Raises
    ValueError when the judge model is the run's model under test."""
    run_dir = Path(run_dir)
    by_id = cases if isinstance(cases, dict) else {_attr(c, "id"): c for c in cases}
    config = _read_json(run_dir / "config.json", {}) or {}
    if client is not None and judge_model and judge_model == config.get("model"):
        raise ValueError(f"the judge model {judge_model!r} is the model under test; pick another judge model")
    rows = []
    for path in sorted((run_dir / "transcripts").glob("*.json")):
        transcript = _read_json(path)
        if not isinstance(transcript, dict):
            continue
        case = by_id.get(transcript.get("case_id"))
        if case is None:
            continue
        judge_checks, judge_usage, judge_error = [], {}, None
        if client is not None and not transcript.get("error") and transcript.get("stop_reason") != "max_tokens":
            try:
                judge_checks = judge(case, transcript, client, judge_model, usage_out=judge_usage)
            except cost_mod.BudgetExceeded as e:  # keep the verdicts already paid for; no judge after this
                judge_error = scrub(f"{type(e).__name__}: {e}")
                client = None
            except Exception as e:  # one failed judge call must not lose the run's grades
                judge_error = scrub(f"{type(e).__name__}: {e}")
        tg = grade(case, transcript, judge_checks)
        row = tg.to_dict()
        row.update({
            "group": _attr(case, "group", ""), "model": transcript.get("model"),
            "all_trials_must_pass": bool(_attr(case, "all_trials_must_pass", False)),
            "trials_planned": _attr(case, "trials", None),
            "usage": transcript.get("usage") or {}, "cost_usd": transcript.get("cost_usd"),
            "duration_s": transcript.get("duration_s"), "stop_reason": transcript.get("stop_reason"),
            "tool_call_count": len(tool_calls(transcript)),
            "garmin_call_count": len(transcript.get("garmin_calls") or []),
            "judge_model": judge_usage.get("model"), "judge_usage": judge_usage.get("usage"),
            "judge_cost_usd": judge_cost(judge_usage.get("model"), judge_usage.get("usage")),
            "judge_error": judge_error, "transcript": path.name,
        })
        rows.append(row)
    if write:
        with open(run_dir / "grades.jsonl", "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return rows


# --- results ----------------------------------------------------------------------------------------------

def case_results(rows) -> list:
    """CaseResult dicts per (case_id, variant) from grade rows, sorted by case and variant."""
    groups = {}
    for row in rows:
        groups.setdefault((row.get("case_id"), row.get("variant")), []).append(row)
    out = []
    for (case_id, _variant), trials in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        first = trials[0]
        case = {"id": case_id, "group": first.get("group", ""),
                "all_trials_must_pass": first.get("all_trials_must_pass", False)}
        out.append(aggregate(case, trials).to_dict())
    return out


def _rate(results, trials_rows):
    n = len(results)
    passed = sum(1 for r in results if r["passed"])
    graded = [t for t in trials_rows if t.get("status") not in ("error", "truncated")]
    tp = sum(1 for t in graded if t.get("passed"))
    low, high = wilson(passed, n)
    tl, th = wilson(tp, len(graded))
    must = [r for r in results if r["all_trials_must_pass"] and r["pass_all"] is not None]
    return {
        "case_variants": n, "passed": passed, "pass_rate": (passed / n) if n else None, "pass_rate_ci95": [low, high],
        "trials_graded": len(graded), "trial_passes": tp,
        "pass_at_1": (tp / len(graded)) if graded else None, "pass_at_1_ci95": [tl, th],
        "all_trials_cases": len(must),
        "pass_all_rate": (sum(1 for r in must if r["pass_all"]) / len(must)) if must else None,
        "incomplete": sum(1 for r in results if r["status"] == "incomplete"),
    }


def _sum_usage(rows, key="usage"):
    total = {k: 0 for k in TOKEN_KEYS}
    for row in rows:
        usage = row.get(key) or {}
        for k in TOKEN_KEYS:
            total[k] += int(usage.get(k) or 0)
    return total


def build_summary(run_dir) -> dict:
    """The summary dict of a run folder (nothing is written)."""
    run_dir = Path(run_dir)
    rows = read_grades(run_dir)
    config = _read_json(run_dir / "config.json", {}) or {}
    results = case_results(rows)
    summary = {
        "run": run_dir.name, "model": config.get("model") or next((r.get("model") for r in rows if r.get("model")),
                                                                   None),
        "judge_model": config.get("judge_model") or next((r.get("judge_model") for r in rows
                                                          if r.get("judge_model")), None),
        "judge_counted": any(r.get("judge_counted") for r in rows),
        "trials": len(rows),
        "errors": sum(1 for r in rows if r.get("status") == "error"),
        "truncated": sum(1 for r in rows if r.get("status") == "truncated"),
        "refusals": sum(1 for r in rows if r.get("status") == "refusal"),
        "headline": _rate(results, rows),
        "per_group": {}, "per_variant": {},
        "tokens": _sum_usage(rows), "judge_tokens": _sum_usage(rows, "judge_usage"),
        "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows), 4),
        "judge_cost_usd": round(sum(float(r.get("judge_cost_usd") or 0) for r in rows), 4),
        "judge_errors": sum(1 for r in rows if r.get("judge_error")),
        "tool_calls": sum(int(r.get("tool_call_count") or 0) for r in rows),
        "cases": results,
    }
    summary["total_cost_usd"] = round(summary["cost_usd"] + summary["judge_cost_usd"], 4)
    for key, out in (("group", summary["per_group"]), ("variant", summary["per_variant"])):
        for value in sorted({str(r[key]) for r in results}):
            sel = [r for r in results if str(r[key]) == value]
            sel_rows = [row for row in rows if str(row.get(key)) == value]
            entry = _rate(sel, sel_rows)
            entry["tokens"] = _sum_usage(sel_rows)
            entry["cost_usd"] = round(sum(float(r.get("cost_usd") or 0) for r in sel_rows), 4)
            out[value] = entry
    worst = [r for r in results if not r["passed"]]
    worst.sort(key=lambda r: (r["pass_at_1"] if r["pass_at_1"] is not None else -1, r["case_id"], r["variant"]))
    summary["worst"] = [{"case_id": r["case_id"], "variant": r["variant"], "status": r["status"],
                         "pass_at_1": r["pass_at_1"], "failed_checks": r["failed_checks"]} for r in worst[:10]]
    return summary


def _key(r):
    return f"{r['case_id']}/{r['variant']}"


def flips(base_results, new_results) -> dict:
    """{"pass_to_fail": [...], "fail_to_pass": [...], "only_base": [...], "only_new": [...]} by case/variant;
    an incomplete result is neither pass nor fail and never flips."""
    base = {_key(r): r for r in base_results}
    new = {_key(r): r for r in new_results}
    out = {"pass_to_fail": [], "fail_to_pass": [], "only_base": sorted(set(base) - set(new)),
           "only_new": sorted(set(new) - set(base))}
    for k in sorted(set(base) & set(new)):
        b, n = base[k]["status"], new[k]["status"]
        if b == "pass" and n == "fail":
            out["pass_to_fail"].append(k)
        elif b == "fail" and n == "pass":
            out["fail_to_pass"].append(k)
    return out


def _pct(x):
    return "-" if x is None else f"{100 * x:.0f}%"


def _ci(pair):
    low, high = pair
    return "" if low is None else f" ({100 * low:.0f}-{100 * high:.0f}%)"


def render_md(summary) -> str:
    h = summary["headline"]
    lines = [f"# Eval run {summary['run']}", "",
             f"Model: {summary.get('model') or '-'}; judge: {summary.get('judge_model') or '-'}"
             f" ({'counted' if summary.get('judge_counted') else 'report only'}).",
             f"Trials: {summary['trials']} ({summary['errors']} errors, {summary['truncated']} truncated, "
             f"{summary['refusals']} refusals). Cost: ${summary['total_cost_usd']:.2f} (model "
             f"${summary['cost_usd']:.2f}, judge ${summary['judge_cost_usd']:.2f}). Judge errors: "
             f"{summary['judge_errors']}. Tool calls: {summary['tool_calls']}.",
             "",
             f"**Cases passed: {h['passed']}/{h['case_variants']} = {_pct(h['pass_rate'])}{_ci(h['pass_rate_ci95'])}"
             f"; pass@1 {_pct(h['pass_at_1'])}{_ci(h['pass_at_1_ci95'])}; pass^k of all-trials cases "
             f"{_pct(h['pass_all_rate'])}.**", ""]
    for title, table in (("Per group", summary["per_group"]), ("Per variant", summary["per_variant"])):
        lines += [f"## {title}", "", "| | cases | passed | pass rate | pass@1 | pass^k (all-trials) | input tok | "
                  "output tok | cost |", "|---|---|---|---|---|---|---|---|---|"]
        for name, e in table.items():
            lines.append(f"| {name} | {e['case_variants']} | {e['passed']} | {_pct(e['pass_rate'])} | "
                         f"{_pct(e['pass_at_1'])} | {_pct(e['pass_all_rate'])} | {e['tokens']['input_tokens']} | "
                         f"{e['tokens']['output_tokens']} | ${e['cost_usd']:.2f} |")
        lines.append("")
    t, j = summary["tokens"], summary["judge_tokens"]
    lines += ["## Tokens", "", f"Model: {t['input_tokens']} in, {t['output_tokens']} out, "
              f"{t['cache_read_input_tokens']} cache read, {t['cache_creation_input_tokens']} cache write. "
              f"Judge: {j['input_tokens']} in, {j['output_tokens']} out.", ""]
    if summary["worst"]:
        lines += ["## Worst cases", "", "| case | variant | status | pass@1 | failed checks |", "|---|---|---|---|---|"]
        for w in summary["worst"]:
            checks = ", ".join(f"{k} x{v}" for k, v in sorted(w["failed_checks"].items()))
            lines.append(f"| {w['case_id']} | {w['variant']} | {w['status']} | {_pct(w['pass_at_1'])} | {checks} |")
        lines.append("")
    if summary.get("flipped") is not None:
        f = summary["flipped"]
        lines += [f"## Flipped against {summary.get('baseline')}", "",
                  "pass -> fail: " + (", ".join(f["pass_to_fail"]) or "none"),
                  "fail -> pass: " + (", ".join(f["fail_to_pass"]) or "none"), ""]
    return "\n".join(lines)


def _baseline_results(path):
    path = Path(path)
    data = _read_json(path / "summary.json")
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return data["cases"]
    return case_results(read_grades(path))


def summarize(run_dir, baseline_dir=None) -> dict:
    """Write summary.json and summary.md into `run_dir`; returns the summary dict."""
    run_dir = Path(run_dir)
    summary = build_summary(run_dir)
    config = _read_json(run_dir / "config.json", {}) or {}
    base = baseline_dir or config.get("baseline")
    summary["baseline"] = str(base) if base else None
    summary["flipped"] = flips(_baseline_results(base), summary["cases"]) if base and Path(base).is_dir() else None
    with open(run_dir / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1, sort_keys=True)
        fh.write("\n")
    with open(run_dir / "summary.md", "w", encoding="utf-8") as fh:
        fh.write(render_md(summary) + "\n")
    return summary


def _load(path):
    data = _read_json(Path(path) / "summary.json")
    if isinstance(data, dict) and "headline" in data:
        return data
    return build_summary(path)


def _delta(a, b, pct=True):
    if a is None or b is None:
        return "-"
    d = b - a
    return f"{100 * d:+.0f} pts" if pct else f"{d:+,.0f}" if isinstance(d, int) else f"{d:+.2f}"


def compare(base_dir, new_dir) -> str:
    """A text report of what changed from run `base_dir` to run `new_dir`."""
    base, new = _load(base_dir), _load(new_dir)
    f = flips(base["cases"], new["cases"])
    lines = [f"Compare {Path(base_dir).name} -> {Path(new_dir).name}",
             f"models: {base.get('model')} -> {new.get('model')}", "",
             "Flipped pass -> fail: " + (", ".join(f["pass_to_fail"]) or "none"),
             "Flipped fail -> pass: " + (", ".join(f["fail_to_pass"]) or "none")]
    if f["only_base"]:
        lines.append("Only in base: " + ", ".join(f["only_base"]))
    if f["only_new"]:
        lines.append("Only in new: " + ", ".join(f["only_new"]))
    hb, hn = base["headline"], new["headline"]
    for label, key in (("Pass rate", "pass_rate"), ("pass@1", "pass_at_1"),
                       ("pass^k (all-trials cases)", "pass_all_rate")):
        lines.append(f"{label}: {_pct(hb[key])} -> {_pct(hn[key])} ({_delta(hb[key], hn[key])})")
    lines.insert(len(lines) - 3, "")
    for title, key in (("group", "per_group"), ("variant", "per_variant")):
        for name in sorted(set(base[key]) | set(new[key])):
            b, n = base[key].get(name, {}), new[key].get(name, {})
            lines.append(f"{title} {name}: pass rate {_pct(b.get('pass_rate'))} -> {_pct(n.get('pass_rate'))} "
                         f"({_delta(b.get('pass_rate'), n.get('pass_rate'))})")
    for k in ("input_tokens", "output_tokens"):
        b, n = base["tokens"][k], new["tokens"][k]
        lines.append(f"{k}: {b} -> {n} ({_delta(b, n, False)})")
    for k in ("cost_usd", "judge_cost_usd", "total_cost_usd"):
        b, n = float(base.get(k) or 0), float(new.get(k) or 0)
        lines.append(f"{k}: {b:.2f} -> {n:.2f} ({_delta(b, n, False)})")
    lines.append(f"tool calls: {base.get('tool_calls', 0)} -> {new.get('tool_calls', 0)}")
    return "\n".join(lines)
