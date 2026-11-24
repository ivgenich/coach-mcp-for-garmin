"""Human labels for the LLM judge, and its calibration.

label(run_dir) walks the graded trials of a run (grades.jsonl, written by evals.grading.grade_run with a
judge client), shows each trial's user prompt, tool results and answer, and asks the human yes / no for every
judge item of that trial (s skips, q stops). Each answer is appended to evals/judge/labels.jsonl beside the
judge's verdict, with the rubric hash and judge model; a trial item already labelled is not asked again.

stats() compares human and judge on the labels made under the current rubric file: raw agreement and
Cohen's kappa, overall and per item. write_calibration() stores them in evals/judge/calibration.json with
calibrated = true only at agreement >= 0.90 and kappa >= 0.6 on at least 40 labels (and false otherwise,
including when kappa is undefined because every label is the same). Seed the run with bad answers (an invented
number, a wrong level, an obeyed injection) so both verdicts occur.

    python -m evals.judge.label <run_dir>          label a run
    python -m evals.judge.label stats [--write]    show (and store) agreement and kappa
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from evals.grading.judge import CALIBRATION_PATH, RUBRICS_PATH, load_rubrics, rubric_hash
from evals.grading.report import read_grades

LABELS_PATH = Path(__file__).resolve().parent / "labels.jsonl"
MIN_LABELS = 40
MIN_AGREEMENT = 0.90
MIN_KAPPA = 0.6
_SHOW_RESULT = 600  # characters of each tool result shown to the labeller


def read_labels(path=LABELS_PATH) -> list:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        return []
    return rows


def _label_key(row):
    return (row.get("run"), row.get("case_id"), row.get("variant"), row.get("trial"), row.get("item"))


def _show(transcript, out):
    out("=" * 72)
    out(f"case {transcript.get('case_id')}  variant {transcript.get('variant')}  trial {transcript.get('trial')}"
        f"  model {transcript.get('model')}")
    for msg in transcript.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            texts = [content] if isinstance(content, str) else [
                b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
            for text in texts:
                out("USER: " + text)
    for call in transcript.get("tool_calls") or []:
        text = call.get("text") or ""
        more = f" ... ({len(text)} chars)" if len(text) > _SHOW_RESULT else ""
        out(f"TOOL {call.get('name')} {json.dumps(call.get('input'))}"
            f"{' [error]' if call.get('is_error') else ''}: {text[:_SHOW_RESULT]}{more}")
    out("ANSWER:\n" + (transcript.get("final_text") or "(empty)"))
    out("-" * 72)


def label(run_dir, labels_path=LABELS_PATH, rubrics_path=RUBRICS_PATH, input_fn=input, out=print) -> int:
    """Ask for human labels on the judge items of `run_dir`; returns the number of labels written."""
    run_dir = Path(run_dir)
    rows = [r for r in read_grades(run_dir)
            if any(str(c.get("name", "")).startswith("judge:") for c in r.get("checks") or [])]
    if not rows:
        out(f"No judged trials in {run_dir} (grade the run with a judge first).")
        return 0
    rubrics = load_rubrics(rubrics_path)
    current = rubric_hash(rubrics_path)
    done = {_label_key(r) for r in read_labels(labels_path) if r.get("rubric_hash") == current}
    written = 0
    with open(labels_path, "a", encoding="utf-8") as fh:
        for row in rows:
            judged = [c for c in row["checks"] if str(c.get("name", "")).startswith("judge:")]
            todo = [c for c in judged if (run_dir.name, row.get("case_id"), row.get("variant"), row.get("trial"),
                                          c["name"][6:]) not in done]
            if not todo:
                continue
            name = row.get("transcript") or f"{row.get('case_id')}__{row.get('variant')}__t{row.get('trial')}.json"
            try:
                with open(run_dir / "transcripts" / name, encoding="utf-8") as tf:
                    transcript = json.load(tf)
            except (OSError, ValueError):
                out(f"(transcript {name} not found, skipped)")
                continue
            _show(transcript, out)
            for check in todo:
                item = check["name"][6:]
                question = rubrics.get(item, {}).get("question", item)
                while True:
                    reply = input_fn(f"[{item}] {question} (y/n/s/q): ").strip().lower()
                    if reply in ("y", "n", "s", "q"):
                        break
                if reply == "q":
                    return written
                if reply == "s":
                    continue
                record = {"run": run_dir.name, "case_id": row.get("case_id"), "variant": row.get("variant"),
                          "trial": row.get("trial"), "model": row.get("model"), "item": item,
                          "human": reply == "y", "judge": bool(check.get("passed")),
                          "judge_model": row.get("judge_model"), "rubric_hash": current,
                          "labelled_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                fh.write(json.dumps(record, sort_keys=True) + "\n")
                fh.flush()
                written += 1
    return written


def cohen_kappa(pairs):
    """Cohen's kappa of (human, judge) bool pairs; None when undefined (no pairs, or chance agreement is 1)."""
    n = len(pairs)
    if n == 0:
        return None
    agree = sum(1 for h, j in pairs if h == j) / n
    ph = sum(1 for h, _ in pairs if h) / n
    pj = sum(1 for _, j in pairs if j) / n
    chance = ph * pj + (1 - ph) * (1 - pj)
    if chance >= 1:
        return None
    return (agree - chance) / (1 - chance)


def _measure(pairs):
    n = len(pairs)
    return {"n": n, "agreement": (sum(1 for h, j in pairs if h == j) / n) if n else None,
            "kappa": cohen_kappa(pairs)}


def stats(labels_path=LABELS_PATH, rubrics_path=RUBRICS_PATH) -> dict:
    """Agreement and kappa, overall and per item, over the labels of the current rubric file."""
    current = rubric_hash(rubrics_path)
    labels = [r for r in read_labels(labels_path)
              if r.get("rubric_hash") == current and isinstance(r.get("human"), bool)
              and isinstance(r.get("judge"), bool)]
    pairs = [(r["human"], r["judge"]) for r in labels]
    out = _measure(pairs)
    out["per_item"] = {}
    for item in sorted({r["item"] for r in labels}):
        out["per_item"][item] = _measure([(r["human"], r["judge"]) for r in labels if r["item"] == item])
    models = sorted({r.get("judge_model") for r in labels if r.get("judge_model")})
    out["judge_model"] = models[0] if len(models) == 1 else (models or None)
    out["rubric_hash"] = current
    out["calibrated"] = bool(out["n"] >= MIN_LABELS and out["agreement"] is not None
                             and out["agreement"] >= MIN_AGREEMENT and out["kappa"] is not None
                             and out["kappa"] >= MIN_KAPPA and isinstance(out["judge_model"], str))
    return out


def write_calibration(result, path=CALIBRATION_PATH) -> dict:
    """Store `result` (from stats()) as the calibration file; returns what was written."""
    data = {"calibrated": result["calibrated"], "agreement": result["agreement"], "kappa": result["kappa"],
            "n_labels": result["n"], "per_item": result["per_item"], "judge_model": result["judge_model"],
            "rubric_hash": result["rubric_hash"],
            "thresholds": {"agreement": MIN_AGREEMENT, "kappa": MIN_KAPPA, "min_labels": MIN_LABELS},
            "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": "Written by evals/judge/label.py (stats --write). Judge checks only report until calibrated "
                    "is true for the current rubrics.toml hash."}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.write("\n")
    return data


def _fmt(x):
    return "-" if x is None else f"{x:.3f}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.judge.label", description=__doc__.split("\n\n")[0])
    parser.add_argument("target", help="a run folder to label, or 'stats'")
    parser.add_argument("--write", action="store_true", help="with stats: write evals/judge/calibration.json")
    args = parser.parse_args(argv)
    if args.target == "stats":
        result = stats()
        print(f"labels {result['n']}: agreement {_fmt(result['agreement'])}, kappa {_fmt(result['kappa'])}, "
              f"calibrated {result['calibrated']}")
        for item, m in result["per_item"].items():
            print(f"  {item}: n {m['n']}, agreement {_fmt(m['agreement'])}, kappa {_fmt(m['kappa'])}")
        if args.write:
            write_calibration(result)
            print(f"wrote {CALIBRATION_PATH}")
        return 0
    if not Path(args.target).is_dir():
        print(f"not a folder: {args.target}", file=sys.stderr)
        return 2
    n = label(args.target)
    print(f"{n} label(s) written to {LABELS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
