-- The vector table history_retrieval reads and writes (langchain-postgres PGVectorStore layout plus a generated
-- tsvector column for the keyword channel). Same DDL as history_retrieval.DOC_TABLE_SQL, whose
-- ensure_doc_table() stays as the fallback; test_history_store checks the two agree.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS history;

CREATE TABLE IF NOT EXISTS history.doc_vectors (
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
    );

CREATE INDEX IF NOT EXISTS doc_vectors_tsv ON history.doc_vectors USING GIN (content_tsv);

CREATE INDEX IF NOT EXISTS doc_vectors_hnsw ON history.doc_vectors USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS doc_vectors_kind_start ON history.doc_vectors (account_fp, kind, start_date);
