"""History search retrieval, composed in LangChain.

Three channels, each a LangChain retriever, fused by LangChain's EnsembleRetriever
(Reciprocal Rank Fusion):

- vector: the stored documents' embeddings (history_embed.OnnxEmbeddings) in a
  LangChain vector store: PGVectorStore (langchain-postgres v2, HNSW, cosine) on
  Railway's Postgres, or InMemoryVectorStore for tests and offline evals;
- keyword: Postgres full-text search (a generated tsvector column + GIN index)
  over the same rows, as a BaseRetriever (KeywordRetriever); an in-memory
  BM25 twin for tests and evals;
- numbers: "weeks like this one" (NumericWeekRetriever, a BaseRetriever): robust
  z-scores (median and MAD over this athlete's own weeks) of the stored weekly
  numbers; a feature whose MAD is 0 is dropped, a pair that shares fewer than
  6 features is skipped, and fewer than 8 usable weeks is NotEnoughHistory.

langchain-postgres 0.0.19 also offers hybrid search inside PGVectorStore
(HybridSearchConfig: plainto_tsquery + RRF). It is not used: it fuses only
vector and keyword (the numbers channel must join the same fusion), it hands the
raw query to plainto_tsquery (all words must match, which misses loose wording),
and the ablation needs each channel on its own. docs/history-search.md has the
details.

An optional reranker (RERANK, off by default) wraps the fused retriever in a
ContextualCompressionRetriever with a local cross-encoder; it stays off unless
the ablation shows a gain.

Safety:
- query text is cut to tokens [a-z0-9]+ (at most 16, 32 characters each); only
  those reach SQL, always as bound parameters; a query with no such token (for
  example Hebrew) skips the keyword channel;
- any database error becomes HistoryUnavailable with a fixed text; nothing logs
  the query, a document or a number;
- LangSmith tracing is forced off (langsmith_off(), and every call runs inside
  tracing_context(enabled=False)), whatever LANGSMITH_* / LANGCHAIN_* say.

Pure helpers (tokens, robust z-scores, numeric ranking, RRF) are standard
library; LangChain classes are built on first use, so Python 3.10 without the
history packages can import this module.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import logging
import math
import os
import re
import threading
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger("garmin_ext.history")

CHANNELS = ("vector", "keyword", "numbers")
DEFAULT_CHANNELS = CHANNELS
RERANK = False  # the ablation (evals/history) decides; see docs/history-search.md
RRF_K = 60
FETCH_K = 50  # candidates per channel before fusion
MAX_TOKENS = 16
MAX_TOKEN_CHARS = 32
MIN_WEEKS = 8
MIN_SHARED = 6
MIN_PAIR = 3
EXCLUDE_WEEKS = 2  # "like this week" never returns the 2 weeks around it
MAD_SCALE = 1.4826
FOCUSES = ("overall", "recovery", "training")
# The stored week numbers (history_extract.WEEK_FIELDS) the numbers channel compares. Context fields (the
# 28-day HRV average, Garmin's balanced range, last week's hours, nights synced) are not the week's state.
RECOVERY_FEATURES = ("hrv_avg_7d", "hrv_delta_ms", "sleep_avg_h", "nights_under_6h", "sleep_avg_score", "rhr_avg",
                     "stress_avg", "body_battery_high_avg")
TRAINING_FEATURES = ("total_hours", "change_pct", "sessions", "run_hours", "bike_hours", "swim_hours",
                     "other_hours", "acwr", "load_acute", "load_chronic_change")
FOCUS_FEATURES = {"overall": RECOVERY_FEATURES + TRAINING_FEATURES, "recovery": RECOVERY_FEATURES,
                  "training": TRAINING_FEATURES}

SCHEMA = "history"
DOC_TABLE = "doc_vectors"
DOC_METADATA_COLUMNS = ("doc_key", "kind", "start_date", "end_date", "account_fp", "embed_model")
# The vector table in the PGVectorStore layout plus a generated tsvector column for the keyword channel.
# Idempotent; the same DDL belongs in a history_sql migration (owned by the store).
DOC_TABLE_SQL = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.{DOC_TABLE} (
        langchain_id UUID PRIMARY KEY,
        content TEXT NOT NULL,
        embedding vector(384) NOT NULL,
        doc_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        account_fp TEXT NOT NULL,
        embed_model TEXT NOT NULL,
        langchain_metadata JSON,
        content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
    )""",
    f"CREATE INDEX IF NOT EXISTS {DOC_TABLE}_tsv ON {SCHEMA}.{DOC_TABLE} USING GIN (content_tsv)",
    f"CREATE INDEX IF NOT EXISTS {DOC_TABLE}_hnsw ON {SCHEMA}.{DOC_TABLE} USING hnsw (embedding vector_cosine_ops)",
    f"CREATE INDEX IF NOT EXISTS {DOC_TABLE}_kind_start ON {SCHEMA}.{DOC_TABLE} (account_fp, kind, start_date)",
)
_KEYWORD_SQL = (
    f"SELECT doc_key, kind, start_date, end_date, content, langchain_metadata::text AS meta, "
    "ts_rank_cd(content_tsv, q) AS score "
    f"FROM {SCHEMA}.{DOC_TABLE}, to_tsquery('english', %(q)s) AS q "
    "WHERE content_tsv @@ q AND account_fp = %(fp)s AND kind = %(kind)s "
    "AND start_date >= %(date_from)s AND start_date <= %(date_to)s "
    "ORDER BY score DESC, start_date DESC LIMIT %(k)s"
)
_DOC_ID_NS = uuid.UUID("6f1d3c2e-9b0a-5d4e-8f7a-1c2b3d4e5f60")
DB_ERROR_TEXT = "history database error"


class HistoryUnavailable(RuntimeError):
    """The history database or model could not answer. str() is a fixed group name, never a detail."""

    def __init__(self, group: str = "database"):
        super().__init__(group)
        self.group = group


class NotEnoughHistory(ValueError):
    """Fewer than MIN_WEEKS weeks share enough numbers for a similar-week search."""

    def __init__(self, usable: int):
        super().__init__(f"{usable} usable weeks")
        self.usable = usable


# ---------------------------------------------------------------- LangSmith

_LANGSMITH_VARS = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")


def langsmith_off() -> None:
    """Force LangSmith tracing off for this process, whatever the environment says.

    Sets the tracing switches to "false", drops langsmith's cached reading of them and,
    when langsmith is installed, turns its global tracing flag off. API keys are left alone
    (they are not read without tracing)."""
    for name in _LANGSMITH_VARS:
        os.environ[name] = "false"
    try:
        import langsmith
        import langsmith.utils as ls_utils
    except ImportError:
        return
    cache_clear = getattr(getattr(ls_utils, "get_env_var", None), "cache_clear", None)
    if cache_clear is not None:
        cache_clear()
    configure = getattr(langsmith, "configure", None)
    if configure is not None:
        configure(enabled=False)


@contextlib.contextmanager
def no_tracing():
    """Run LangChain calls with LangSmith tracing off in this context too."""
    langsmith_off()
    try:
        from langsmith import tracing_context
    except ImportError:
        yield
        return
    with tracing_context(enabled=False):
        yield


# ---------------------------------------------------------------- query text

def query_tokens(text: Any) -> List[str]:
    """The query's words as [a-z0-9]+ tokens: lower case, at most MAX_TOKENS of at most
    MAX_TOKEN_CHARS characters, duplicates dropped. Nothing else of the text survives."""
    if not isinstance(text, str):
        return []
    out: List[str] = []
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        tok = tok[:MAX_TOKEN_CHARS]
        if tok not in out:
            out.append(tok)
        if len(out) >= MAX_TOKENS:
            break
    return out


def tsquery_text(tokens: Sequence[str]) -> str:
    """'a | b | c' for to_tsquery from tokens that are already [a-z0-9]+ (checked again here)."""
    safe = [t for t in tokens if re.fullmatch(r"[a-z0-9]{1,32}", t)]
    return " | ".join(safe)


# ---------------------------------------------------------------- numbers channel (pure)

def _date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def week_features(numbers: Mapping[str, Any], focus: str = "overall") -> Dict[str, float]:
    """The numeric features of one stored week for this focus (nulls dropped: masked, never zero)."""
    out: Dict[str, float] = {}
    for key in FOCUS_FEATURES[focus]:
        v = _num((numbers or {}).get(key))
        if v is not None:
            out[key] = v
    return out


def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def robust_scales(feature_rows: Iterable[Mapping[str, float]]) -> Dict[str, Tuple[float, float]]:
    """{feature: (median, scale)} where scale = 1.4826 * MAD; a feature with MAD 0 (or one value) is dropped."""
    columns: Dict[str, List[float]] = {}
    for row in feature_rows:
        for k, v in row.items():
            columns.setdefault(k, []).append(v)
    out: Dict[str, Tuple[float, float]] = {}
    for k, vals in columns.items():
        if len(vals) < 2:
            continue
        med = _median(vals)
        mad = _median([abs(v - med) for v in vals])
        if mad > 0:
            out[k] = (med, MAD_SCALE * mad)
    return out


@dataclasses.dataclass
class NumericMatch:
    row: Mapping[str, Any]
    distance: float
    why: List[str]
    shared: int

    @property
    def similarity(self) -> float:
        return round(1.0 / (1.0 + self.distance), 2)


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def numeric_rank(reference: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], focus: str = "overall", *,
                 exclude_weeks: int = EXCLUDE_WEEKS, date_from: Optional[str] = None,
                 date_to: Optional[str] = None) -> List[NumericMatch]:
    """Weeks ranked by robust-z distance to `reference` (rows are week dicts with start and numbers).

    Raises NotEnoughHistory when fewer than MIN_WEEKS weeks (the reference included) have at least
    MIN_SHARED usable features. Ties go to the newer week."""
    if focus not in FOCUSES:
        raise ValueError("unknown focus")
    feats = {str(_date(r["start"])): week_features(r.get("numbers") or {}, focus) for r in rows}
    ref_start = _date(reference["start"])
    feats.setdefault(str(ref_start), week_features(reference.get("numbers") or {}, focus))
    scales = robust_scales(feats.values())
    need = min(MIN_SHARED, max(1, len(scales)))
    usable = [k for k, f in feats.items() if sum(1 for n in f if n in scales) >= need]
    if len(usable) < MIN_WEEKS or not scales:
        raise NotEnoughHistory(len(usable))
    ref = feats[str(ref_start)]
    ref_usable = sum(1 for n in ref if n in scales)
    lo = _date(date_from) if date_from else dt.date.min
    hi = _date(date_to) if date_to else dt.date.max
    out: List[NumericMatch] = []
    for r in rows:
        start = _date(r["start"])
        if abs((start - ref_start).days) <= 7 * exclude_weeks or not lo <= start <= hi:
            continue
        cand = feats[str(start)]
        shared = [n for n in scales if n in cand and n in ref]
        # A key-mode reference has no load numbers: it needs as many shared features as it has, at least 3.
        if len(shared) < max(MIN_PAIR, min(need, ref_usable)):
            continue
        diffs = {n: (cand[n] - ref[n]) / scales[n][1] for n in shared}
        dist = math.sqrt(sum(d * d for d in diffs.values()) / len(shared))
        closest = sorted(shared, key=lambda n: (abs(diffs[n]), n))[:3]
        why = [f"{n} {_fmt(cand[n])} vs {_fmt(ref[n])}" for n in closest]
        out.append(NumericMatch(r, dist, why, len(shared)))
    out.sort(key=lambda m: (m.distance, -_date(m.row["start"]).toordinal()))
    return out


def rrf(rankings: Sequence[Sequence[str]], k: int = RRF_K, weights: Optional[Sequence[float]] = None) -> List[str]:
    """Reciprocal Rank Fusion of id lists (the same formula as EnsembleRetriever; for checks and evals)."""
    weights = list(weights or [1.0] * len(rankings))
    score: Dict[str, float] = {}
    first: Dict[str, int] = {}
    n = 0
    for w, ranking in zip(weights, rankings):
        for rank, doc_id in enumerate(ranking, start=1):
            score[doc_id] = score.get(doc_id, 0.0) + w / (rank + k)
            if doc_id not in first:
                first[doc_id] = n
                n += 1
    return sorted(score, key=lambda d: (-score[d], first[d]))


# ---------------------------------------------------------------- documents

def doc_key(kind: str, start: Any) -> str:
    return f"{kind}:{_date(start).isoformat()}"


def doc_uuid(account_fp: str, key: str) -> str:
    """The stable row id of a document: re-embedding the same day or week overwrites its row."""
    return str(uuid.uuid5(_DOC_ID_NS, f"{account_fp}|{key}"))


def normalise_metadata(meta: Mapping[str, Any]) -> Dict[str, Any]:
    """Metadata as the tools read it: doc_id, kind, start, end (ISO strings) and the rest unchanged."""
    m = dict(meta or {})
    if isinstance(m.get("langchain_metadata"), str):
        try:
            m.update(json.loads(m.pop("langchain_metadata")))
        except ValueError:
            m.pop("langchain_metadata", None)
    start = m.get("start") or m.get("start_date")
    end = m.get("end") or m.get("end_date")
    if start is not None:
        m["start"] = _date(start).isoformat()
    if end is not None:
        m["end"] = _date(end).isoformat()
    if "doc_id" not in m:
        m["doc_id"] = m.get("doc_key") or (doc_key(m["kind"], m["start"]) if "kind" in m and "start" in m else None)
    return m


_STOP = frozenset("a an and are as at be by for from had has have i in is it its me my of on or the this to was "
                  "were what when where which with did do does like last time week weeks day days".split())


def _stem(tok: str) -> str:
    """A small English suffix stripper, close enough to Postgres' Snowball stemmer for the template words."""
    for suf in ("fulness", "ness", "ful", "ling", "ing", "ies", "es", "ed", "ly", "s"):
        if len(tok) > len(suf) + 2 and tok.endswith(suf):
            tok = tok[: -len(suf)] + ("y" if suf == "ies" else "")
            if suf in ("ing", "ed", "ling") and len(tok) > 3 and tok[-1] == tok[-2] and tok[-1] not in "aeiouls":
                tok = tok[:-1]
            break
    return tok


def _doc_terms(text: str) -> List[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))]


def relevance_floor(ranked: Sequence[str], contents: Mapping[str, str], tokens: Sequence[str],
                    pool: int) -> List[str]:
    """The results of a word search that are relevant enough to be re-sorted by date (order=newest).

    ranked: result keys in relevance order; contents: every searched document's text by key (the document
    frequencies are counted over all of them). Keeps the first `pool` results, then only those holding the
    rarest query term that any document holds, so a common word ("week", "bike") alone does not qualify a
    result. When no query term is in any document (a meaning-only match), the first `pool` results stay."""
    top = list(ranked[:max(0, pool)])
    terms = {_stem(t) for t in tokens if t not in _STOP}
    if not top or not terms:
        return top
    words = {key: set(_doc_terms(text or "")) for key, text in contents.items()}
    df = {t: sum(1 for w in words.values() if t in w) for t in terms}
    held = [t for t in terms if df[t] > 0]
    if not held:
        return top
    rarest = min(held, key=lambda t: (df[t], t))
    kept = [key for key in top if rarest in words.get(key, set())]
    return kept or top


class MemoryKeywordBackend:
    """BM25 over documents in memory: the offline twin of the Postgres full-text channel
    (English stop words dropped, a light suffix stemmer; underscores split words as Postgres does)."""

    def __init__(self) -> None:
        self.docs: Dict[str, Tuple[Any, List[str]]] = {}

    def add(self, docs: Sequence[Any]) -> None:
        for d in docs:
            m = normalise_metadata(d.metadata)
            self.docs[m["doc_id"]] = (d, _doc_terms(d.page_content))

    def search(self, tokens: Sequence[str], *, kind: str, account_fp: str, date_from: str, date_to: str,
               k: int) -> List[Tuple[Any, float]]:
        terms = {_stem(t) for t in tokens if t not in _STOP}
        if not terms or not self.docs:
            return []
        n = len(self.docs)
        avg = sum(len(t) for _, t in self.docs.values()) / n
        df = {t: sum(1 for _, terms_ in self.docs.values() if t in terms_) for t in terms}
        out = []
        for d, words in self.docs.values():
            m = normalise_metadata(d.metadata)
            if m.get("kind") != kind or m.get("account_fp", account_fp) != account_fp:
                continue
            if not date_from <= m["start"] <= date_to:
                continue
            score = 0.0
            for t in terms:
                tf = words.count(t)
                if tf:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(words) / avg))
            if score > 0:
                out.append((d, score))
        out.sort(key=lambda p: (-p[1], -_date(normalise_metadata(p[0].metadata)["start"]).toordinal()))
        return out[:k]


def _pg_dsn(dsn: str) -> str:
    """A libpq DSN (postgresql://) from DATABASE_URL."""
    return re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql://", dsn)


def _sqlalchemy_url(dsn: str) -> str:
    return re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql+psycopg://", dsn)


class PgKeywordBackend:
    """Postgres full-text search over history.doc_vectors (to_tsquery('english', 'a | b'), ts_rank_cd)."""

    def __init__(self, dsn: str, connect: Optional[Callable[[str], Any]] = None):
        self.dsn = _pg_dsn(dsn)
        self._connect = connect

    def search(self, tokens: Sequence[str], *, kind: str, account_fp: str, date_from: str, date_to: str,
               k: int) -> List[Tuple[Any, float]]:
        q = tsquery_text(tokens)
        if not q:
            return []
        from langchain_core.documents import Document

        try:
            if self._connect is None:
                import psycopg

                conn = psycopg.connect(self.dsn, connect_timeout=5)
            else:
                conn = self._connect(self.dsn)
            with conn:
                rows = conn.execute(_KEYWORD_SQL, {"q": q, "fp": account_fp, "kind": kind, "date_from": date_from,
                                                   "date_to": date_to, "k": int(k)}).fetchall()
        except Exception:  # noqa: BLE001 - never log the query or the driver's text
            log.warning("history keyword search: %s", DB_ERROR_TEXT)
            raise HistoryUnavailable("database") from None
        out = []
        for key, kind_col, start_col, end_col, content, meta, score in rows:
            # PGVectorStore keeps metadata_columns (kind, start_date, end_date) in their own columns, not in
            # langchain_metadata, so the dates come from the columns.
            m = json.loads(meta) if meta else {}
            m.update({"kind": kind_col, "start": start_col, "end": end_col})
            m["doc_id"] = key
            out.append((Document(page_content=content, metadata=normalise_metadata(m)), float(score)))
        return out


# ---------------------------------------------------------------- LangChain classes

_CLASSES: Dict[str, Any] = {}
_CLASS_LOCK = threading.Lock()


def _classes() -> Dict[str, Any]:
    """Build the LangChain retriever classes on first use (the packages are optional at import)."""
    with _CLASS_LOCK:
        if _CLASSES:
            return _CLASSES
        langsmith_off()
        from langchain_classic.retrievers import EnsembleRetriever
        from langchain_core.documents import Document
        from langchain_core.retrievers import BaseRetriever

        class NumericWeekRetriever(BaseRetriever):
            """Weeks like a reference week by robust z-score distance over the stored numbers.

            The query string is ignored: the reference week is set on the retriever."""

            rows: List[Dict[str, Any]]
            reference: Dict[str, Any]
            focus: str = "overall"
            k: int = FETCH_K
            date_from: Optional[str] = None
            date_to: Optional[str] = None
            exclude_weeks: int = EXCLUDE_WEEKS

            def _get_relevant_documents(self, query: str, *, run_manager: Any = None) -> List[Any]:
                matches = numeric_rank(self.reference, self.rows, self.focus, exclude_weeks=self.exclude_weeks,
                                       date_from=self.date_from, date_to=self.date_to)
                docs = []
                for m in matches[: self.k]:
                    start = _date(m.row["start"]).isoformat()
                    docs.append(Document(page_content=f"week {start}", metadata={
                        "doc_id": doc_key("week", start), "kind": "week", "start": start,
                        "end": _date(m.row.get("end") or (_date(start) + dt.timedelta(days=6))).isoformat(),
                        "distance": round(m.distance, 4), "similarity": m.similarity, "why": m.why,
                        "channel": "numbers"}))
                return docs

        class KeywordRetriever(BaseRetriever):
            """Full-text search as a LangChain retriever; the query is reduced to safe tokens first."""

            backend: Any
            kind: str = "week"
            account_fp: str = ""
            date_from: str = "0001-01-01"
            date_to: str = "9999-12-31"
            k: int = FETCH_K

            def _get_relevant_documents(self, query: str, *, run_manager: Any = None) -> List[Any]:
                hits = self.backend.search(query_tokens(query), kind=self.kind, account_fp=self.account_fp,
                                           date_from=self.date_from, date_to=self.date_to, k=self.k)
                docs = []
                for d, score in hits:
                    m = normalise_metadata(d.metadata)
                    m["channel"], m["keyword_score"] = "keyword", round(score, 4)
                    docs.append(Document(page_content=d.page_content, metadata=m))
                return docs

        class FilteredVectorRetriever(BaseRetriever):
            """A vector store's similarity search with the kind/date/account filter in the store's own form."""

            store: Any
            store_filter: Any = None
            k: int = FETCH_K

            def _get_relevant_documents(self, query: str, *, run_manager: Any = None) -> List[Any]:
                kwargs = {"k": self.k}
                if self.store_filter is not None:
                    kwargs["filter"] = self.store_filter
                docs = []
                for d in self.store.similarity_search(query, **kwargs):
                    m = normalise_metadata(d.metadata)
                    m["channel"] = "vector"
                    docs.append(Document(page_content=d.page_content, metadata=m))
                return docs

        class ChannelEnsembleRetriever(EnsembleRetriever):
            """EnsembleRetriever (weighted RRF) that also records which channels found each document."""

            channel_names: List[str] = []

            def rank_fusion(self, query: str, run_manager: Any, *, config: Any = None) -> List[Any]:
                lists = []
                for name, r in zip(self.channel_names, self.retrievers):
                    with _span("history.channel", channel=name) as sp:
                        docs = r.invoke(query, config)
                        _set(sp, "coach.history.result_count", len(docs))
                    lists.append(docs)
                found: Dict[str, List[str]] = {}
                extra: Dict[str, Dict[str, Any]] = {}
                for name, docs in zip(self.channel_names, lists):
                    for d in docs:
                        key = d.metadata[self.id_key]
                        found.setdefault(key, [])
                        if name not in found[key]:
                            found[key].append(name)
                        extra.setdefault(key, {}).update(
                            {k: v for k, v in d.metadata.items() if k in ("why", "distance", "similarity")})
                fused = self.weighted_reciprocal_rank(lists)
                out = []
                for d in fused:
                    m = dict(d.metadata)
                    key = m[self.id_key]
                    m.update(extra.get(key, {}))
                    m["matched_on"] = found.get(key, [])
                    m.pop("channel", None)
                    out.append(Document(page_content=d.page_content, metadata=m))
                return out

        _CLASSES.update(NumericWeekRetriever=NumericWeekRetriever, KeywordRetriever=KeywordRetriever,
                        FilteredVectorRetriever=FilteredVectorRetriever,
                        ChannelEnsembleRetriever=ChannelEnsembleRetriever, Document=Document)
        return _CLASSES


def __getattr__(name: str) -> Any:  # history_retrieval.NumericWeekRetriever etc., built lazily
    if name in ("NumericWeekRetriever", "KeywordRetriever", "FilteredVectorRetriever", "ChannelEnsembleRetriever"):
        return _classes()[name]
    raise AttributeError(name)


# ---------------------------------------------------------------- telemetry (optional)

def _telemetry() -> Any:
    try:
        import telemetry
    except Exception:  # noqa: BLE001 - telemetry is optional and never breaks a search
        return None
    return telemetry


@contextlib.contextmanager
def _span(name: str, channel: Optional[str] = None):
    """A telemetry span ("retriever <channel>" or "history.search") when ext/telemetry.py is present; else
    nothing. Attributes are channel names and counts only, never text."""
    tm = _telemetry()
    if tm is None:
        yield None
        return
    span_name = f"retriever {channel}" if channel else name
    attrs = {"coach.history.channels": channel} if channel else {}
    with tm.span(span_name, attrs) as sp:
        yield sp


def _set(sp: Any, key: str, value: Any) -> None:
    setter = getattr(sp, "set", None)
    if callable(setter):
        try:
            setter({key: value})
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- document indexes

class MemoryDocIndex:
    """Documents in a LangChain InMemoryVectorStore plus the in-memory keyword twin (tests, offline evals)."""

    def __init__(self, embeddings: Any = None, account_fp: str = "") -> None:
        self.embeddings = embeddings
        self.account_fp = account_fp
        self.keyword = MemoryKeywordBackend()
        self.row_ids: Dict[str, str] = {}  # history_docs id (uuid) -> doc_id
        self.store = None
        if embeddings is not None:
            from langchain_core.vectorstores import InMemoryVectorStore

            self.store = InMemoryVectorStore(embedding=embeddings)

    def add(self, docs: Sequence[Any]) -> int:
        docs = list(docs)
        prepared = []
        for d in docs:
            m = normalise_metadata(d.metadata)
            m.setdefault("account_fp", self.account_fp)
            if getattr(d, "id", None):
                self.row_ids[str(d.id)] = m["doc_id"]
            prepared.append(_classes()["Document"](page_content=d.page_content, metadata=m))
        self.keyword.add(prepared)
        if self.store is not None and prepared:
            with no_tracing():
                self.store.add_documents(prepared, ids=[p.metadata["doc_id"] for p in prepared])
        return len(prepared)

    def delete(self, ids: Sequence[str]) -> None:
        keys = [self.row_ids.pop(str(i), str(i)) for i in ids]
        for key in keys:
            self.keyword.docs.pop(key, None)
        if self.store is not None and keys:
            self.store.delete(ids=keys)

    def vector_retriever(self, *, kind: str, date_from: str, date_to: str, k: int = FETCH_K) -> Any:
        if self.store is None:
            return None
        fp = self.account_fp

        def keep(d: Any) -> bool:
            m = normalise_metadata(d.metadata)
            return m.get("kind") == kind and m.get("account_fp", fp) == fp and date_from <= m["start"] <= date_to

        return _classes()["FilteredVectorRetriever"](store=self.store, store_filter=keep, k=k)

    def keyword_backend(self) -> Any:
        return self.keyword


_ENGINES: Dict[str, Any] = {}
_ENGINE_LOCK = threading.Lock()


def _engine(dsn: str) -> Any:
    """One PGEngine (SQLAlchemy async engine + its loop thread) per database per process."""
    from langchain_postgres import PGEngine

    url = _sqlalchemy_url(dsn)
    with _ENGINE_LOCK:
        if url not in _ENGINES:
            _ENGINES[url] = PGEngine.from_connection_string(url=url, pool_pre_ping=True, pool_size=2,
                                                            max_overflow=2)
        return _ENGINES[url]


def ensure_doc_table(dsn: str, connect: Optional[Callable[[str], Any]] = None) -> None:
    """Create history.doc_vectors and its indexes if missing (idempotent)."""
    try:
        if connect is None:
            import psycopg

            conn = psycopg.connect(_pg_dsn(dsn), connect_timeout=5)
        else:
            conn = connect(_pg_dsn(dsn))
        with conn:
            for stmt in DOC_TABLE_SQL:
                conn.execute(stmt)
    except Exception:  # noqa: BLE001
        log.warning("history doc table: %s", DB_ERROR_TEXT)
        raise HistoryUnavailable("database") from None


class PgDocIndex:
    """Documents in Postgres: LangChain PGVectorStore (vector channel) + full-text (keyword channel)."""

    def __init__(self, dsn: str, embeddings: Any, account_fp: str, *, embed_model: str = "") -> None:
        self.dsn = dsn
        self.embeddings = embeddings
        self.account_fp = account_fp
        self.embed_model = embed_model
        self._store = None
        self._lock = threading.Lock()

    def store(self) -> Any:
        with self._lock:
            if self._store is None:
                langsmith_off()
                from langchain_postgres import PGVectorStore
                from langchain_postgres.v2.indexes import HNSWQueryOptions

                try:
                    self._store = PGVectorStore.create_sync(
                        engine=_engine(self.dsn), table_name=DOC_TABLE, schema_name=SCHEMA,
                        embedding_service=self.embeddings, metadata_columns=list(DOC_METADATA_COLUMNS),
                        index_query_options=HNSWQueryOptions(ef_search=100))
                except Exception:  # noqa: BLE001
                    log.warning("history vector store: %s", DB_ERROR_TEXT)
                    raise HistoryUnavailable("database") from None
            return self._store

    def add(self, docs: Sequence[Any]) -> int:
        Document = _classes()["Document"]
        prepared, ids = [], []
        for d in docs:
            m = normalise_metadata(d.metadata)
            row = {k: v for k, v in m.items() if k not in ("start", "end", "doc_id")}
            row.update(doc_key=m["doc_id"], kind=m["kind"], start_date=m["start"], end_date=m.get("end", m["start"]),
                       account_fp=self.account_fp, embed_model=self.embed_model)
            prepared.append(Document(page_content=d.page_content, metadata=row))
            ids.append(str(d.id) if getattr(d, "id", None) else doc_uuid(self.account_fp, m["doc_id"]))
        if not prepared:
            return 0
        try:
            with no_tracing():
                self.store().add_documents(prepared, ids=ids)
        except HistoryUnavailable:
            raise
        except Exception:  # noqa: BLE001
            log.warning("history vector add: %s", DB_ERROR_TEXT)
            raise HistoryUnavailable("database") from None
        return len(prepared)

    def delete(self, ids: Sequence[str]) -> None:
        """Delete rows by id (history_docs.doc_id values)."""
        if not ids:
            return
        try:
            self.store().delete(ids=[str(i) for i in ids])
        except HistoryUnavailable:
            raise
        except Exception:  # noqa: BLE001
            log.warning("history vector delete: %s", DB_ERROR_TEXT)
            raise HistoryUnavailable("database") from None

    def vector_retriever(self, *, kind: str, date_from: str, date_to: str, k: int = FETCH_K) -> Any:
        flt = {"$and": [{"account_fp": {"$eq": self.account_fp}}, {"kind": {"$eq": kind}},
                        {"start_date": {"$between": [date_from, date_to]}}]}
        return _classes()["FilteredVectorRetriever"](store=_GuardedStore(self), store_filter=flt, k=k)

    def keyword_backend(self) -> Any:
        return PgKeywordBackend(self.dsn)


class _GuardedStore:
    """similarity_search on the PG store with database errors mapped to HistoryUnavailable."""

    def __init__(self, index: PgDocIndex):
        self.index = index

    def similarity_search(self, query: str, **kwargs: Any) -> List[Any]:
        try:
            return self.index.store().similarity_search(query, **kwargs)
        except HistoryUnavailable:
            raise
        except Exception:  # noqa: BLE001
            log.warning("history vector search: %s", DB_ERROR_TEXT)
            raise HistoryUnavailable("database") from None


# ---------------------------------------------------------------- composition

@dataclasses.dataclass
class SearchRequest:
    kind: str = "week"
    text: str = ""  # free words (search_history) or the reference document's text (find_similar_weeks)
    reference: Optional[Dict[str, Any]] = None  # a stored week row: numbers channel
    focus: str = "overall"
    date_from: str = "0001-01-01"
    date_to: str = "9999-12-31"
    k: int = 5
    channels: Tuple[str, ...] = DEFAULT_CHANNELS
    exclude_weeks: int = EXCLUDE_WEEKS
    rerank: bool = RERANK


def build_retriever(index: Any, week_rows: Sequence[Mapping[str, Any]], req: SearchRequest,
                    cross_encoder: Any = None) -> Tuple[Any, List[str]]:
    """The LangChain retriever for this request and the names of the channels it uses.

    Channels run only when they can: vector needs text and a vector store, keyword needs at least one
    safe token, numbers needs a reference week. Returns (None, []) when nothing can run."""
    c = _classes()
    retrievers, names = [], []
    if "vector" in req.channels and req.text.strip():
        vr = index.vector_retriever(kind=req.kind, date_from=req.date_from, date_to=req.date_to)
        if vr is not None:
            retrievers.append(vr)
            names.append("vector")
    if "keyword" in req.channels and query_tokens(req.text):
        retrievers.append(c["KeywordRetriever"](backend=index.keyword_backend(), kind=req.kind,
                                                account_fp=getattr(index, "account_fp", ""),
                                                date_from=req.date_from, date_to=req.date_to))
        names.append("keyword")
    if "numbers" in req.channels and req.reference is not None and req.kind == "week":
        retrievers.append(c["NumericWeekRetriever"](rows=[dict(r) for r in week_rows], reference=dict(req.reference),
                                                    focus=req.focus, date_from=req.date_from, date_to=req.date_to,
                                                    exclude_weeks=req.exclude_weeks))
        names.append("numbers")
    if not retrievers:
        return None, []
    retriever = c["ChannelEnsembleRetriever"](retrievers=retrievers, weights=[1.0] * len(retrievers), c=RRF_K,
                                              id_key="doc_id", channel_names=names)
    if req.rerank and cross_encoder is not None:
        from langchain_classic.retrievers import ContextualCompressionRetriever
        from langchain_classic.retrievers.document_compressors import CrossEncoderReranker

        retriever = ContextualCompressionRetriever(
            base_compressor=CrossEncoderReranker(model=cross_encoder, top_n=max(req.k * 4, 20)),
            base_retriever=retriever)
    return retriever, names


def search(index: Any, week_rows: Sequence[Mapping[str, Any]], req: SearchRequest,
           cross_encoder: Any = None) -> Tuple[List[Any], List[str]]:
    """Run the fused search: (documents in rank order with metadata doc_id/start/end/matched_on/..., channels).

    Excludes the reference week and the EXCLUDE_WEEKS around it from every channel. Raises
    NotEnoughHistory (numbers channel) or HistoryUnavailable (database)."""
    retriever, names = build_retriever(index, week_rows, req, cross_encoder)
    if retriever is None:
        return [], []
    with no_tracing(), _span("history.search") as sp:
        docs = retriever.invoke(req.text or "reference week")
        _set(sp, "coach.history.result_count", len(docs))
    ref_start = _date(req.reference["start"]) if req.reference else None
    out, seen = [], set()
    for d in docs:
        m = normalise_metadata(d.metadata)
        if m["doc_id"] in seen:
            continue
        if ref_start is not None and abs((_date(m["start"]) - ref_start).days) <= 7 * req.exclude_weeks:
            continue
        seen.add(m["doc_id"])
        d.metadata = m
        out.append(d)
    return out[: req.k], names


# ---------------------------------------------------------------- process-wide index, worker job, forget

_INDEXES: Dict[Tuple[str, str], Any] = {}
_INDEX_LOCK = threading.Lock()
_TABLE_READY: set = set()


def doc_index(dsn: str, account_fp: str, embeddings: Any = None) -> PgDocIndex:
    """The process's PgDocIndex for this database and account (the table is created once if missing)."""
    import history_embed

    with _INDEX_LOCK:
        key = (_pg_dsn(dsn), account_fp)
        if key not in _INDEXES:
            if key[0] not in _TABLE_READY:
                ensure_doc_table(dsn)
                _TABLE_READY.add(key[0])
            emb = embeddings if embeddings is not None else history_embed.embeddings(history_embed.default_root())
            _INDEXES[key] = PgDocIndex(dsn, emb, account_fp, embed_model=history_embed.BGE_SMALL.name)
        return _INDEXES[key]


MODEL_WAIT_S = 6 * 3600


def embed_doc(job: Any, ctx: Any) -> None:
    """Worker job embed_doc {"kind": "day"|"week", "start": "YYYY-MM-DD"}: build the stored day's or week's
    document (history_docs.build_document, no Garmin call), embed it with the local model and upsert it into
    history.doc_vectors under its stable id. An empty or missing day deletes its row. Idempotent: running it
    twice writes the same row. The vector write uses the vector store's own connection (not the job's
    transaction); a crash between the two only means the job runs again.

    ctx.doc_index (optional) replaces the Postgres index (tests)."""
    import history_docs
    import history_embed
    import history_store as hs
    import jobqueue
    import worker

    payload = getattr(job, "payload", None) or {}
    kind = payload.get("kind")
    if kind not in ("day", "week"):
        raise worker.Permanent("bad_input")
    try:
        start = _date(payload.get("start"))
    except (TypeError, ValueError):
        raise worker.Permanent("bad_input") from None
    conn = getattr(ctx, "conn", None)
    if ctx.store.meta_get(hs.META_MISMATCH, conn=conn) == "yes":
        raise worker.Permanent("account_mismatch")
    fp = ctx.store.meta_get(hs.META_ACCOUNT, conn=conn)
    if not fp:
        raise jobqueue.Defer(3600, "no_account")
    index = getattr(ctx, "doc_index", None) or doc_index(os.environ.get("DATABASE_URL", ""), fp)
    doc = history_docs.build_document(ctx.store, fp, kind, start, conn=conn)
    try:
        if doc is None:
            index.delete([history_docs.doc_id(kind, start)])
            return
        with no_tracing():
            index.add([history_docs.to_langchain(doc)])
    except history_embed.ModelUnavailable:
        raise jobqueue.Defer(MODEL_WAIT_S, "model_unavailable") from None


def register_jobs(register: Callable[..., Any]) -> None:
    """worker.load_handlers() calls this: embed_doc never calls Garmin."""
    register("embed_doc", embed_doc, garmin=False)


def forget(features: Any = None, connect: Optional[Callable[[str], Any]] = None) -> str:
    """HISTORY_FORGET=yes (launch.py, after history_store.forget_all()): drop history.doc_vectors if it is
    still there and forget this process's engines and indexes. Returns what it removed (a fixed text)."""
    dsn = getattr(features, "database_url", None) or os.environ.get("DATABASE_URL", "")
    with _INDEX_LOCK:
        _INDEXES.clear()
        _TABLE_READY.clear()
    with _ENGINE_LOCK:
        _ENGINES.clear()
    if not dsn:
        return "no database configured"
    try:
        if connect is None:
            import psycopg

            conn = psycopg.connect(_pg_dsn(dsn), connect_timeout=5)
        else:
            conn = connect(_pg_dsn(dsn))
        with conn:
            conn.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{DOC_TABLE}")
    except Exception:  # noqa: BLE001
        log.warning("history forget: %s", DB_ERROR_TEXT)
        raise HistoryUnavailable("database") from None
    return f"{SCHEMA}.{DOC_TABLE} (vectors and keyword index)"


class VectorRows:
    """The worker's Context.vectors: deletes document rows by id (history_docs.doc_id values) from
    history.doc_vectors with plain SQL. Needs no model and no account fingerprint (the ids are unique)."""

    def __init__(self, dsn: str, connect: Optional[Callable[[str], Any]] = None):
        self.dsn = _pg_dsn(dsn)
        self._connect = connect

    def __repr__(self) -> str:  # never the URL
        return "VectorRows()"

    def delete(self, ids: Sequence[str]) -> int:
        clean = [str(uuid.UUID(str(i))) for i in ids or ()]
        if not clean:
            return 0
        try:
            if self._connect is None:
                import psycopg

                conn = psycopg.connect(self.dsn, connect_timeout=5)
            else:
                conn = self._connect(self.dsn)
            with conn:
                if conn.execute("SELECT to_regclass(%s)", (f"{SCHEMA}.{DOC_TABLE}",)).fetchone()[0] is None:
                    return 0
                cur = conn.execute(f"DELETE FROM {SCHEMA}.{DOC_TABLE} WHERE langchain_id = ANY(%s::uuid[])",
                                   (clean,))
                return cur.rowcount or 0
        except Exception:  # noqa: BLE001
            log.warning("history vector delete: %s", DB_ERROR_TEXT)
            raise HistoryUnavailable("database") from None


def worker_vectors(dsn: str) -> VectorRows:
    """For worker.main(): Worker(vectors=history_retrieval.worker_vectors(DATABASE_URL))."""
    return VectorRows(dsn)
