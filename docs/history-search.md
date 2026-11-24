# History search: retrieval, tools and evals

With `HISTORY_SEARCH=on` the server answers questions about past weeks and days from its own stored history,
without calling Garmin: "weeks like this one", "when did I last have short nights", "a heavy bike block".
This page covers the search side: the two tools, the LangChain retrieval behind them, the local embedding
model and the retrieval evals. How the history is filled (the worker, the job queue, the Garmin call guard)
is in [history-worker.md](history-worker.md).

## The two tools

| Tool | Use it for | Main arguments |
|---|---|---|
| `find_similar_weeks` | weeks whose numbers look like a reference week, and what followed them | `end_date` (a day of the reference week; empty = the last complete week), `focus` (`overall`, `recovery`, `training`), `k` (1-10), `date_from`, `date_to` |
| `search_history` | weeks or days by words or flags; "when did I last ..." with `order=newest` | `query` (English words), `kind` (`week`, `day`), `flags` (comma-separated ids), `order` (`relevance`, `newest`), `k`, `date_from`, `date_to` |

Both read only Postgres and never call Garmin. Every answer is compact JSON:

- `index`: `from`, `to`, `weeks`, `days`, `complete` (every day from `from` to yesterday is stored, every day
  older than 3 days is final, and the history reaches a year back or the account start; see
  [How far back the history goes](#how-far-back-the-history-goes)), `stale` (the newest stored day is more than
  3 days before yesterday);
- `reference` (find_similar_weeks): the reference week's dates and stored numbers;
- `results`: for each week or day its dates, level, flags, tags, the stored `numbers`, a code-built `why`
  (find_similar_weeks: the closest numbers as "result vs reference"; search_history: each matching flag or tag
  with the stored numbers it is decided from, e.g. "has short_nights (nights_under_6h 5)", or for a word or
  meaning match without such a flag the result's own flags and tags the same way; with no flag or tag at all,
  "text match" or "meaning match" with the result's main stored numbers, e.g. "text match (total_hours 9.6,
  sleep_avg_h 7.1, hrv_delta_ms -1, sessions 6)", never words from the query), `followed_by` (the next
  1-2 weeks; for days the next 3 days), `matched_on` (`numbers`, `keywords`, `meaning`, `flags`), `missing`
  (what history never stores: plan, gear, the long session) and `similarity` (a rank aid, not a percentage).

The guidance lives in the tool descriptions (claude.ai drops server instructions): report stored numbers
with their dates, compute nothing new, say when the index is partial or stale, and say that "tired" is judged
from sleep, HRV, resting heart rate and stress because the server has no record of how the athlete felt.

"No data: ..." answers (not errors): the index is empty or still filling; the history belongs to a different
Garmin account (set `HISTORY_FORGET=yes` once); a date outside the index; fewer than 8 indexed weeks with
enough numbers for a similar-week search; the database did not answer. Bad arguments get the usual
`Error: Invalid input: ...` text. An unknown flag is never echoed back; `load_high` and `load_low` are refused
in key mode (no training load with a Personal API key).

## What is stored

Everything lives in one Postgres schema, `history`, in the database `DATABASE_URL` names:

| Table | What it holds |
|---|---|
| `history.days` | one row per day: numbers only, from a fixed field list (`history_extract.DAY_FIELDS`): sleep, HRV, resting heart rate, stress, Body Battery, sessions and time and distance per sport, Garmin status codes, and which sources were missing. No activity names, notes, GPS, or anything typed by a person |
| `history.weeks` | one row per Monday-to-Sunday week: the weekly numbers, level and flags the rules compute (`WEEK_FIELDS`) |
| `history.doc_vectors` | each day's and week's document (a fixed-template English text built from those numbers, with the numbers as bands) and its 384-number embedding, for search |
| `history.meta` | the account fingerprint (an HMAC of the Garmin profile id with a random salt; never the id), the salt, versions and the backfill floor |
| `history.jobs` | the job queue: job kinds, dates and counters, never Garmin answers |
| `history.guard_state` | the shared Garmin call guard's row (cooldowns and counters) |
| `history.garmin_token` | full mode only: the app's Garmin token file, sealed with AES-256-GCM under `HISTORY_TOKEN_KEY`, so the worker can call Garmin |

A day fetched again (the Monday recheck, or a day fetched again because it was not final) takes the new
answer of every source that answered, an empty answer too (a value Garmin no longer has becomes null), and
keeps the stored values of a source that failed this time.

## How far back the history goes

The worker fills the history newest day first, one day at a time, up to a year back (plus 3 weeks for the
oldest week's HRV average). It stops earlier at the account start: after 14 days in a row on which every
Garmin call answered and none had data. The day it stops at is kept as the backfill floor (`history.meta`).

- A day on which a call failed never counts toward those 14 days, whatever the error. Garmin unavailable (a
  5xx answer, a network error, a timeout) is retried with backoff (about 1 and 5 minutes later); after the
  third try the day is stored with that source missing and stays not final. The daily sweep fetches up to 5
  such older days again each day until they are final; a day that already failed again is retried on the
  Monday sweep only, so a day Garmin keeps failing costs a few calls a week.
- A source Garmin refuses (HTTP 403, for example a metric your device does not record) is not retried: the day
  is stored final with that source missing. Garmin's "no data" is an answer, not a missing source.
- The stop is skipped while an older stored day has data (a long break without the watch, not the start).
- The floor is provisional: each daily sweep checks a few days before it once (7, 21, 49 and 98 days
  earlier). When one of them has data, the floor is removed and the backfill goes on from there.
- `index.complete` is true only when no day from the start of the index to yesterday is missing, no day
  older than 3 days is still not final, and the history reaches a year back or the floor. Days stored before
  the floor by those checks are not part of the index range.

A day is "empty" when no source has a value. Garmin's 7-day averages (resting heart rate, HRV) can make the
first days of a break count as days with data; that only delays the stop, so it is left as it is.

## Forgetting

- `HISTORY_FORGET=yes` on the app (and on the worker and the scheduler) drops the whole schema `history` at the next start: the
  days, weeks, documents and vectors, the job queue, the guard's row and the sealed token, then runs
  `VACUUM`. The history tools stay off while the flag is set; remove it afterwards
  ([history-worker.md](history-worker.md#stopping-redeploying-forgetting)).
- Turning history search off (`HISTORY_SEARCH=off`) keeps the stored history, but the app deletes the
  sealed Garmin token row at its next start while `DATABASE_URL` is still set (an image built without
  `HISTORY=1` cannot reach the database and logs one line saying so). With history search on in key mode
  (`GARMIN_AUTH=key`) the app deletes that row at each start too, since key mode stores no login.
- Postgres backups are not touched by either: copies taken before can remain for as long as your backup
  settings keep them. Check your Railway backup settings, and delete the backups (or the Postgres
  service) too if you want every copy gone.

## Retrieval (LangChain)

Every stored day and week becomes a LangChain `Document` (`history_docs.py`): a fixed-template English text
with numbers only as bands ("sleep short", "training heavy, up a lot"), tags from fixed rules, and the exact
numbers in metadata. No model writes any of it.

Three channels, each a LangChain retriever, fused with Reciprocal Rank Fusion (k = 60) by `EnsembleRetriever`:

| Channel | Class | Over |
|---|---|---|
| vector ("meaning") | `PGVectorStore` (langchain-postgres v2) via `FilteredVectorRetriever` | the documents' embeddings in `history.doc_vectors`, HNSW index, cosine |
| keyword | `KeywordRetriever` (a `BaseRetriever`) | Postgres full-text: a generated `tsvector` column + GIN index, `to_tsquery('english', 'a | b')`, `ts_rank_cd` |
| numbers | `NumericWeekRetriever` (a `BaseRetriever`) | the stored weekly numbers: robust z-scores (median and 1.4826 x MAD over this athlete's own weeks) |

The numbers channel compares 8 recovery features (HRV 7-day average and its change, sleep hours, nights under
6 h, sleep score, resting HR, stress, Body Battery) and 10 training features (hours, change, sessions, hours
per sport, ACWR, acute load, chronic load change); `focus` picks one set or both. A feature whose MAD is 0 is
dropped (never divided by), a missing value is masked (never zero, so key-mode weeks compare on what they
have), and a pair must share at least as many features as the reference has (at least 3). Fewer than 8 usable
weeks is a clear "No data". The reference week and the 2 weeks on each side are never returned.

An optional reranker (`history_retrieval.RERANK`, off) wraps the fused retriever in a
`ContextualCompressionRetriever` with `CrossEncoderReranker` over a local int8 ms-marco MiniLM cross-encoder.

The channels each tool uses come from the ablation below: `find_similar_weeks` runs the numbers channel,
`search_history` the vector channel; without the model (download back-off) `search_history` falls back to
the keyword channel. Flags filter both tools' results, and `order=newest` sorts by date. With query words,
`order=newest` first keeps only the `k` most relevant results that hold the rarest query word (a common word
such as "week" or "bike" is in nearly every document), then sorts those by date.

### LangChain 1.x layout (checked 24 Nov 2026)

| Package (pinned) | What we use |
|---|---|
| `langchain-core` 1.7.6 | `Embeddings`, `BaseRetriever`, `Document`, `BaseCrossEncoder`, `InMemoryVectorStore` |
| `langchain-classic` 1.0.8 | `EnsembleRetriever`, `ContextualCompressionRetriever`, `CrossEncoderReranker` (in 1.x these left the `langchain` package; the slim `langchain` 1.x has agents only and pulls langgraph, so it is not installed) |
| `langchain-postgres` 0.0.19 | `PGVectorStore` + `PGEngine` (v2: one table per store, metadata columns, HNSW). The older `PGVector` class (collection tables `langchain_pg_*`) is deprecated since 0.0.14 and not used |

langchain-postgres also offers hybrid search inside `PGVectorStore` (`HybridSearchConfig`: a tsvector
column, `plainto_tsquery`, RRF or weighted fusion). It is not used: it fuses only vector and keyword (the
numbers channel must join the same fusion), `plainto_tsquery` needs every word to match (loose wording finds
nothing), and the ablation needs each channel on its own. The table still has the tsvector column and GIN
index, so switching is a small change if the evals ever favour it.

### The table

`history.doc_vectors` (in the history schema, so forget's `DROP SCHEMA history CASCADE` removes it):
`langchain_id` (UUID = `history_docs.doc_id`, so re-embedding replaces the row), `content`, `embedding
vector(384)`, `doc_key`, `kind`, `start_date`, `end_date`, `account_fp`, `embed_model`, `langchain_metadata`
(JSON) and the generated `content_tsv`. `history_retrieval.ensure_doc_table()` creates it idempotently.

### Safety

- Query text is reduced to tokens `[a-z0-9]+` (at most 16, 32 characters each) before any SQL, always as a
  bound parameter; a query with no such token (Hebrew) skips the keyword channel and the vector channel still
  runs. The model writes queries in English (the description says so).
- Database errors become a fixed "history database error" log line and a fixed answer; the query, documents
  and numbers are never logged, and the driver's exception is not chained into the log.
- LangSmith tracing is forced off: `LANGSMITH_TRACING`, `LANGCHAIN_TRACING_V2` and friends are set to
  `false`, langsmith's cached settings dropped, `langsmith.configure(enabled=False)` called, and every call runs
  in `tracing_context(enabled=False)`. A test sets the variables to `true` with an API key and checks that no
  LangSmith client is created.
- Answers go through `text_hygiene.clean_tool_answer` (launch.py leaves this repo's own tools' answers alone).

## The embedding model

`history_embed.OnnxEmbeddings` is a LangChain `Embeddings` running BAAI's bge-small-en-v1.5 (MIT, 384
dimensions) in Xenova's int8 ONNX export with onnxruntime and tokenizers, CLS pooling and L2 normalisation;
queries get bge's search instruction prefix. Health text never leaves the server.

| | |
|---|---|
| Files | `onnx/model_quantized.onnx` (34,014,426 bytes) and `tokenizer.json` from `Xenova/bge-small-en-v1.5` at revision `ea104dacec62c0de699686887e3f920caeb4f3e3`, each pinned by SHA-256 in `history_embed.BGE_SMALL` |
| Where | `/data/models` (the volume); a service without `/data` uses the temp directory and downloads again after a deploy |
| Checks | every file hashed on every load; a mismatch is deleted and fetched again |
| Failure | a failed download or hash starts a 24 h back-off (a marker file plus an in-process copy): no download is tried until it ends; the worker defers `embed_doc` jobs (6 h) and `search_history` falls back to keywords |
| Measured (macOS arm64, 1 thread) | imports +82 MB RSS, model +96 MB; one query 3.5 ms after load; 32 documents 0.25 s. Linux numbers are not listed here |

The reranker model (`MINILM_RERANKER`: `Xenova/ms-marco-MiniLM-L-6-v2` int8, 23 MB) follows the same rules
and loads only with `RERANK = True`.

`history_embed.forget_caches()` (called on `HISTORY_FORGET=yes`) has nothing on disk to delete: vectors live in
Postgres, and the model files are public.

## Worker job: embed_doc

`history_retrieval.register_jobs()` registers `embed_doc {"kind": "day"|"week", "start": "YYYY-MM-DD"}` with
the worker (no Garmin calls). It builds the document from the stored numbers (`history_docs.build_document`),
embeds it and upserts its row under `history_docs.doc_id`; a day that became empty deletes its row. A bad
payload or a different account fails the job at once; a missing model defers it. The vector write uses the
vector store's own connection, not the job's transaction: if the worker dies between them the job simply
runs again, and the upsert makes that harmless. `history_retrieval.worker_vectors(dsn)` is the worker's
`Context.vectors` (deletes rows by id for retention).

## Retrieval evals

`evals/history/` holds a seeded synthetic athlete (78 weeks, full mode then key mode from week 60) with planted
episodes (3 HRV drops each followed by a low week, 2 sleep-debt weeks, 2 load spikes, 2 bike blocks, a swim
block, 2 tapers, 3 rest weeks, a travel gap, 2 high-stress weeks, single short nights), 31 labelled queries
(13 "like this week", 13 word queries of which one Hebrew is informational only, 3 flag queries, 2 day
queries) in `labels.toml`, and recall@5, MRR and nDCG@5. The numbers go through the real code
(`history_extract.week_numbers`, `MemoryHistoryStore`, `history_docs`) and the tools' own logic
(`HistoryService`), so the evals score what the tools return. No API call.

```
~/.cache/coach-mcp-ci/venv-history/bin/python -m evals.history                    # all configurations
~/.cache/coach-mcp-ci/venv-history/bin/python -m evals.history --embeddings none --gate   # what CI gates
COACH_TEST_DATABASE_URL=... python -m evals.history --backend postgres            # real pgvector + FTS
```

### Ablation (in memory, int8 bge-small, 24 Nov 2026)

| configuration | recall@5 | MRR | nDCG@5 | similar R@5 | words R@5 | flags R@5 | days R@5 |
|---|---|---|---|---|---|---|---|
| vector | 0.87 | 0.97 | 0.84 | 0.91 | 0.81 | 1.00 | 0.80 |
| vector+keyword | 0.70 | 0.75 | 0.68 | 0.63 | 0.69 | 1.00 | 0.80 |
| vector+keyword+numbers | 0.70 | 0.75 | 0.67 | 0.62 | 0.69 | 1.00 | 0.80 |
| +reranker | 0.69 | 0.77 | 0.64 | 0.55 | 0.73 | 1.00 | 0.80 |
| keyword | 0.62 | 0.61 | 0.57 | 0.51 | 0.60 | 1.00 | 0.80 |
| numbers | 0.80 | 0.84 | 0.78 | 0.95 | 0.60 | 1.00 | 0.80 |
| keyword+numbers | 0.65 | 0.69 | 0.63 | 0.60 | 0.60 | 1.00 | 0.80 |
| numbers+vector | 0.86 | 0.98 | 0.85 | 0.89 | 0.81 | 1.00 | 0.80 |
| shipped (similar: numbers; words: vector) | 0.89 | 0.97 | 0.87 | 0.95 | 0.81 | 1.00 | 0.80 |

("numbers" and "keyword+numbers" run word queries on the keyword channel, since numbers needs a reference.)

What it says:
- For "weeks like this one" the numbers channel is best (0.95); adding text channels through RRF makes it worse.
- For word queries the vector channel is best (0.81); the keyword channel adds noise in the fusion (0.69),
  because the template texts share most of their words.
- The reranker does not help (0.69 vs 0.70 overall) and costs 23 MB more: it stays off.
- So each part stays on only where it helps: the tools ship "numbers" for similar weeks and "vector" for words,
  with keyword as the fallback when the model is missing.
- The in-memory keyword channel is a BM25 twin of Postgres full-text; the Postgres run (manual workflow with
  the pgvector service) checks the same table on the real stemmer.

### Gate

`--gate` fails when a gated configuration is under its floor: `numbers` (recall@5 >= 0.75, MRR >= 0.78; runs
without the model, so CI gates it) and `shipped` (recall@5 >= 0.80, MRR >= 0.85; with the model, in the manual
workflow). The gate takes about 2 s.

## Live cases

`evals/cases/history.toml` (group `history`) has 9 live LLM cases: similar weeks cite real dates, "when did I
last" newest first, a partial index, key mode, the feature off, a two-week user, a Hebrew prompt, a different
account, "tired" from numbers. Each case names a `history` fixture (`evals/harness/history.py`): `athlete` (78
weeks, complete), `partial` (the newest 20 weeks, `complete` false), `two_weeks` (a new user), `mismatch`
(another Garmin account) or `off` (the tools are not served). The harness then serves the two tools through
launch's own registration over `HistoryService` on `MemoryHistoryStore` + `MemoryDocIndex` without the model,
so search_history takes the keyword fallback. These cases need the history packages; without them the harness
raises a clear error, and the offline eval tests skip them. None is a smoke case.
`evals/tests/test_history_eval.py` checks, without a model, that each fixture gives the answer its case is
graded on and that every result carries its dates and the numbers behind its match.

## Dependencies

`config/requirements-history.txt` is the whole hash-pinned closure (33 packages beyond `constraints.txt`),
resolved on Python 3.12 against the image's pins from releases at least two weeks old, installed only with
the build arg `HISTORY=1`. `tokenizers` is installed without its declared `huggingface-hub` dependency:
importing it never loads the hub, and this code downloads the model itself. The local test venv is
`~/.cache/coach-mcp-ci/venv-history` (venv-full's packages plus this file).
