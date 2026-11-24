"""The history retrieval set: the synthetic athlete, the labelled queries, the metrics and the ablation.

    python -m evals.history                       # every configuration, in memory, the local model if it loads
    python -m evals.history --embeddings none     # no model: keyword and numbers only (what CI gates)
    python -m evals.history --backend postgres    # the same against COACH_TEST_DATABASE_URL (pgvector)
    python -m evals.history --gate                # exit 1 when the gated configuration is under its floors
    python -m evals.history --json out.json       # the numbers per query and per configuration

It runs the real tool logic (history_tools.HistoryService over history_retrieval's LangChain retrievers) on
a MemoryHistoryStore filled by evals/history/athlete.py, and scores the dates each tool returns against
evals/history/labels.toml: recall@5, MRR and nDCG@5. No Anthropic API call; the only network use is the
one-time download of the pinned embedding (and reranker) model, which --embeddings none avoids.
Needs the history packages (config/requirements-history.txt), e.g. ~/.cache/coach-mcp-ci/venv-history.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import sys
import tomllib
from typing import Any, Dict, List, Optional, Sequence

from evals.history import athlete as A
from evals.history import metrics as M

LABELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "labels.toml")
K = 5


@dataclasses.dataclass(frozen=True)
class Config:
    name: str
    similar: tuple
    search: tuple
    rerank: bool = False
    needs_model: bool = False


CONFIGS = (
    Config("vector", ("vector",), ("vector",), needs_model=True),
    Config("vector+keyword", ("vector", "keyword"), ("vector", "keyword"), needs_model=True),
    Config("vector+keyword+numbers", ("vector", "keyword", "numbers"), ("vector", "keyword"), needs_model=True),
    Config("+reranker", ("vector", "keyword", "numbers"), ("vector", "keyword"), rerank=True, needs_model=True),
    Config("keyword", ("keyword",), ("keyword",)),
    Config("numbers", ("numbers",), ("keyword",)),
    Config("keyword+numbers", ("keyword", "numbers"), ("keyword",)),
    Config("numbers+vector", ("numbers", "vector"), ("vector",), needs_model=True),
    Config("shipped", ("numbers",), ("vector",), needs_model=True),
)
# Floors per configuration, set from the first runs (docs/history-search.md) a little under the measured
# values. CI has no model, so it gates "numbers" (similar weeks by numbers, words by keyword); the manual
# workflow with the model also gates "shipped" (the tools' defaults).
GATES = {"numbers": {"recall@5": 0.75, "mrr": 0.78}, "shipped": {"recall@5": 0.80, "mrr": 0.85}}


def load_labels(path: str = LABELS) -> List[Dict[str, Any]]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    queries = data.get("query", [])
    ids = [q["id"] for q in queries]
    if len(ids) != len(set(ids)):
        raise ValueError("labels: duplicate query id")
    for q in queries:
        if q.get("tool") not in ("similar", "search"):
            raise ValueError(f"labels {q['id']}: tool must be similar or search")
        if not q.get("relevant"):
            raise ValueError(f"labels {q['id']}: no relevant results")
    return queries


def resolve(athlete: A.Athlete, name: str) -> List[str]:
    """The starts (weeks) or dates (days) an episode name stands for."""
    if name == "short_sleep_days":
        return list(athlete.short_sleep_days)
    if name == "sleep_debt_days":
        out = []
        for w in athlete.episodes["sleep_debt_1"] + athlete.episodes["sleep_debt_2"]:
            out += [(dt.date.fromisoformat(w) + dt.timedelta(days=i)).isoformat() for i in range(5)]
        return out
    if name not in athlete.episodes:
        raise ValueError(f"labels: unknown episode {name}")
    return list(athlete.episodes[name])


def relevant_of(athlete: A.Athlete, q: Dict[str, Any]) -> Dict[str, int]:
    rel: Dict[str, int] = {}
    for name, grade in q["relevant"].items():
        for start in resolve(athlete, name):
            rel[start] = max(rel.get(start, 0), int(grade))
    return rel


def reference_day(athlete: A.Athlete, ref: str) -> str:
    name, _, n = ref.partition("#")
    return resolve(athlete, name)[int(n or 0)]


def make_index(athlete: A.Athlete, backend: str, embeddings: Any, dsn: Optional[str] = None) -> Any:
    import history_retrieval as hr

    docs = A.documents(athlete)
    if backend == "memory":
        index = hr.MemoryDocIndex(embeddings, athlete.account_fp)
    else:
        if embeddings is None:
            import history_embed

            embeddings = history_embed.hash_embeddings()  # fills the table only; the vector channel stays off
        hr.ensure_doc_table(dsn)
        index = hr.PgDocIndex(dsn, embeddings, athlete.account_fp, embed_model="eval")
    index.add(docs)
    return index


def run_config(cfg: Config, athlete: A.Athlete, index: Any, queries: Sequence[Dict[str, Any]],
               cross_encoder: Any = None) -> Dict[str, Any]:
    import history_tools as ht

    svc = ht.HistoryService(athlete.store, lambda fp: index, today=lambda: athlete.today,
                            cross_encoder=cross_encoder, similar_channels=cfg.similar, search_channels=cfg.search,
                            rerank=cfg.rerank)
    rows = []
    for q in queries:
        if q["tool"] == "similar":
            text = svc.find_similar_weeks(reference_day(athlete, q["reference"]), q.get("focus", "overall"), K)
        else:
            text = svc.search_history(q.get("query", ""), q.get("kind", "week"), q.get("flags", ""), "", "",
                                      q.get("order", "relevance"), K)
        try:
            ranked = [r["start"] for r in json.loads(text)["results"]]
        except (ValueError, KeyError):
            ranked = []  # a "No data" answer
        rel = relevant_of(athlete, q)
        rows.append({"id": q["id"], "info": bool(q.get("info")), "ranked": ranked,
                     "recall@5": M.recall_at(ranked, rel, K), "mrr": M.reciprocal_rank(ranked, rel),
                     "ndcg@5": M.ndcg_at(ranked, rel, K)})
    scored = [r for r in rows if not r["info"]]

    def agg(prefix: str) -> Dict[str, float]:
        part = [r for r in scored if r["id"].startswith(prefix)]
        return {m: round(M.mean([r[m] for r in part]), 3) for m in ("recall@5", "mrr", "ndcg@5")}

    return {"config": cfg.name, "all": agg(""), "similar": agg("S"), "words": agg("T"), "flags": agg("F"),
            "days": agg("D"), "queries": rows}


def table(results: Sequence[Dict[str, Any]]) -> str:
    lines = ["| configuration | recall@5 | MRR | nDCG@5 | similar R@5 | words R@5 | flags R@5 | days R@5 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        a = r["all"]
        lines.append(f"| {r['config']} | {a['recall@5']:.2f} | {a['mrr']:.2f} | {a['ndcg@5']:.2f} | "
                     f"{r['similar']['recall@5']:.2f} | {r['words']['recall@5']:.2f} | "
                     f"{r['flags']['recall@5']:.2f} | {r['days']['recall@5']:.2f} |")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.history", description=__doc__.split("\n\n")[0])
    p.add_argument("--backend", choices=("memory", "postgres"), default="memory")
    p.add_argument("--embeddings", choices=("onnx", "hash", "none"), default="onnx")
    p.add_argument("--model-root", default=None, help="model folder (default: history_embed.default_root())")
    p.add_argument("--configs", default="", help="comma-separated configuration names (default: all that run)")
    p.add_argument("--gate", action="store_true", help=f"exit 1 when a configuration is under its floor {GATES}")
    p.add_argument("--json", default=None)
    args = p.parse_args(argv)

    try:
        import history_embed
        import history_retrieval as hr
    except ImportError as exc:
        print(f"evals.history: needs the history packages (config/requirements-history.txt): {exc.name}",
              file=sys.stderr)
        return 2
    hr.langsmith_off()
    dsn = os.environ.get("COACH_TEST_DATABASE_URL") if args.backend == "postgres" else None
    if args.backend == "postgres" and not dsn:
        print("evals.history: --backend postgres needs COACH_TEST_DATABASE_URL", file=sys.stderr)
        return 2
    root = args.model_root or history_embed.default_root()
    embeddings = cross = None
    if args.embeddings == "onnx":
        embeddings = history_embed.embeddings(root)
        try:
            embeddings.load()
            cross = history_embed.cross_encoder(root)
        except history_embed.ModelUnavailable as exc:
            print(f"evals.history: the embedding model is unavailable ({exc.group}); vector configurations "
                  "skipped", file=sys.stderr)
            embeddings = None
    elif args.embeddings == "hash":
        embeddings = history_embed.hash_embeddings()

    athlete = A.build()
    queries = load_labels()
    index = make_index(athlete, args.backend, embeddings, dsn)
    if args.backend == "postgres" and args.embeddings == "none":
        embeddings = None
    wanted = [c for c in args.configs.split(",") if c]
    results = []
    for cfg in CONFIGS:
        if wanted and cfg.name not in wanted:
            continue
        if cfg.needs_model and embeddings is None:
            continue
        rr = None
        if cfg.rerank:
            if cross is None:
                continue
            try:
                cross.score([("warm up", "warm up")])
            except history_embed.ModelUnavailable:
                continue
            rr = cross
        if not cfg.needs_model and embeddings is not None:
            res = run_config(cfg, athlete, _NoVector(index), queries)
        else:
            res = run_config(cfg, athlete, index, queries, rr)
        results.append(res)
    print(table(results))
    print(f"\n{len([q for q in queries if not q.get('info')])} scored queries (+ "
          f"{len([q for q in queries if q.get('info')])} informational), k={K}, backend={args.backend}, "
          f"embeddings={args.embeddings if embeddings is not None else 'none'}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)
    if args.backend == "postgres":
        index.delete([d.id for d in A.documents(athlete)])
    if args.gate:
        return gate(results)
    return 0


def gate(results: Sequence[Dict[str, Any]]) -> int:
    """0 when every gated configuration that ran is at or over its floors (and at least one ran)."""
    ran = [r for r in results if r["config"] in GATES]
    low = [f"{r['config']} {m} {r['all'][m]:.2f} < {floor:.2f}" for r in ran
           for m, floor in GATES[r["config"]].items() if r["all"][m] < floor]
    if not ran:
        low = ["no gated configuration ran"]
    names = ", ".join(r["config"] for r in ran) or "none"
    print(f"evals.history: gate {'FAIL: ' + '; '.join(low) if low else 'PASS'} ({names})")
    return 1 if low else 0


class _NoVector:
    """An index without its vector channel (the keyword/numbers configurations when a model is loaded)."""

    def __init__(self, index: Any):
        self._index = index
        self.account_fp = index.account_fp
        self.embeddings = None

    def vector_retriever(self, **_kw: Any) -> None:
        return None

    def keyword_backend(self) -> Any:
        return self._index.keyword_backend()
