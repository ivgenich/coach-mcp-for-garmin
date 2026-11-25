-- Training plans (plan_store.py): numbers, enums, dates and the server's own Garmin ids only; never names,
-- notes or free text from Garmin or the user. Every table the plan stages need (1: plans and versions;
-- 2: workouts and tokens; 3: feedback and links; 4: changes), created at once so later stages add no
-- migration. Statuses and enums are checked in plan_store (Python), not by CHECK constraints: a constraint
-- could only be widened by removing it first, which the migration test does not allow.
-- Retention: history_jobs.sweep (plan_store.retention); HISTORY_FORGET removes the schema with everything.

CREATE TABLE IF NOT EXISTS history.plans (
    plan_id         text PRIMARY KEY,                 -- 32 hex characters (128 random bits)
    account_fp      text NOT NULL,                    -- history_store's account fingerprint
    status          text NOT NULL,                    -- draft, active, cancelled, finished
    current_version integer NOT NULL DEFAULT 1,
    race_date       date NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    activated_at    timestamptz,
    ended_at        timestamptz
);

-- One active plan per account.
CREATE UNIQUE INDEX IF NOT EXISTS plans_one_active ON history.plans (account_fp) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS plans_account_updated ON history.plans (account_fp, updated_at);

-- Each preview that changed the plan: the engine's rules version, its inputs and its answer.
CREATE TABLE IF NOT EXISTS history.plan_versions (
    plan_id       text NOT NULL REFERENCES history.plans (plan_id) ON DELETE CASCADE,
    version       integer NOT NULL,
    rules_version integer NOT NULL,
    inputs        jsonb NOT NULL,
    body          jsonb NOT NULL,
    content_hash  text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, version)
);

-- The plan's sessions on the Garmin calendar (Stage 2) and their links to finished activities (Stage 3).
-- state: pending, uploading, uploaded, scheduling, scheduled, conflict, unscheduling, unscheduled, deleting,
-- deleted, unknown, failed, local.
-- local: a session tracked in the app only (Stage 3), never sent to Garmin.
CREATE TABLE IF NOT EXISTS history.plan_workouts (
    plan_id             text NOT NULL REFERENCES history.plans (plan_id) ON DELETE CASCADE,
    workout_key         text NOT NULL,
    version             integer NOT NULL,
    date_local          date NOT NULL,
    role                text NOT NULL,
    dto_hash            text NOT NULL,
    state               text NOT NULL DEFAULT 'pending',
    garmin_workout_id   bigint,
    garmin_schedule_id  bigint,
    state_changed_at    timestamptz NOT NULL DEFAULT now(),
    linked_activity_id  bigint,
    link_status         text,                         -- done, partial (each also _early, _late), replaced, missed
    link_checked_at     timestamptz,
    PRIMARY KEY (plan_id, workout_key)
);

CREATE INDEX IF NOT EXISTS plan_workouts_state ON history.plan_workouts (state);

-- Commit, apply and cancel tokens: only their SHA-256 is stored.
CREATE TABLE IF NOT EXISTS history.plan_tokens (
    token_hash    text PRIMARY KEY,
    plan_id       text NOT NULL REFERENCES history.plans (plan_id) ON DELETE CASCADE,
    version       integer NOT NULL,
    action        text NOT NULL,                      -- commit, apply, cancel
    ops           jsonb NOT NULL,
    ops_hash      text NOT NULL,
    status        text NOT NULL DEFAULT 'issued',     -- issued, running, partial, done, expired
    result        jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    first_used_at timestamptz,
    done_at       timestamptz
);

CREATE INDEX IF NOT EXISTS plan_tokens_expires ON history.plan_tokens (expires_at);

-- RPE and pain after a session (numbers and a fixed location list only).
CREATE TABLE IF NOT EXISTS history.plan_feedback (
    plan_id       text NOT NULL REFERENCES history.plans (plan_id) ON DELETE CASCADE,
    workout_key   text NOT NULL,
    rpe           smallint,
    pain          smallint,
    pain_location text,
    sharp         boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, workout_key)
);

-- Weekly adjustments: proposed, approved, applied; the reason is a server-built sentence with its number.
CREATE TABLE IF NOT EXISTS history.plan_changes (
    change_id   text PRIMARY KEY,
    plan_id     text NOT NULL REFERENCES history.plans (plan_id) ON DELETE CASCADE,
    week_start  date NOT NULL,
    kind        text NOT NULL,
    from_value  jsonb,
    to_value    jsonb,
    reason      text NOT NULL,
    evidence    jsonb,
    status      text NOT NULL DEFAULT 'proposed',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
