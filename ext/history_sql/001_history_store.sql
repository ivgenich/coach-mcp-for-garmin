-- History search store (B3): numbers only, never names, ids, notes or raw Garmin answers.
-- Applied by history_store.PgHistoryStore.migrate() in one transaction, under an advisory lock.
-- Versions 001-099 belong to the history store, 100-199 to the job queue and the shared guard state.

CREATE TABLE IF NOT EXISTS history.meta (
    key        text PRIMARY KEY CHECK (key ~ '^[a-z0-9_.:-]{1,64}$'),
    value      text NOT NULL CHECK (length(value) <= 256),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One row per local calendar day (Garmin's calendarDate) and account fingerprint.
CREATE TABLE IF NOT EXISTS history.days (
    account_fp text NOT NULL CHECK (account_fp ~ '^[0-9a-f]{16,64}$'),
    day        date NOT NULL,
    numbers    jsonb NOT NULL CHECK (jsonb_typeof(numbers) = 'object'),
    final      boolean NOT NULL DEFAULT false,
    empty      boolean NOT NULL DEFAULT false,
    rechecked  boolean NOT NULL DEFAULT false,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_fp, day)
);

-- One row per Monday-to-Sunday week, keyed by its Monday.
CREATE TABLE IF NOT EXISTS history.weeks (
    account_fp    text NOT NULL CHECK (account_fp ~ '^[0-9a-f]{16,64}$'),
    start_day     date NOT NULL,
    end_day       date NOT NULL,
    numbers       jsonb NOT NULL CHECK (jsonb_typeof(numbers) = 'object'),
    final         boolean NOT NULL DEFAULT false,
    rules_version integer NOT NULL DEFAULT 1,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_fp, start_day),
    CHECK (end_day = start_day + 6)
);

CREATE INDEX IF NOT EXISTS days_open_idx ON history.days (account_fp, day) WHERE NOT final OR empty;
