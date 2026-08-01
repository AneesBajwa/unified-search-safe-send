"""core schema — connections, searches, drafts, sends, jobs

Written as raw SQL rather than Alembic operations, deliberately. openspec
``data-model.md`` is the authority for this schema and it is expressed as DDL;
transcribing it through ``op.create_table`` would introduce a translation layer
where a partial index predicate or a CHECK expression can silently come out
slightly different. This way the migration can be diffed against the design
document line for line.

``users`` already exists from 0001 and is not touched — migrations are
additive-only for the life of the PoC, so a Cloud Run rollback never strands
the schema ahead of the code.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-31

"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ENUMS = """
CREATE TYPE provider_kind   AS ENUM ('gmail', 'slack');
CREATE TYPE conn_status     AS ENUM ('active', 'expired', 'errored', 'needs_reconnect');
CREATE TYPE job_kind        AS ENUM ('adapter_run', 'send');
CREATE TYPE job_state       AS ENUM ('ready', 'running', 'succeeded', 'failed', 'parked', 'cancelled');
CREATE TYPE error_class     AS ENUM ('transient', 'permanent', 'needs_reconnect', 'config');
CREATE TYPE send_state      AS ENUM ('in_flight', 'delivered', 'failed_permanent', 'failed_transient', 'uncertain');
CREATE TYPE run_status      AS ENUM ('pending', 'running', 'done', 'failed', 'needs_reconnect');
CREATE TYPE source_mode     AS ENUM ('live', 'mock', 'degraded');
"""

API_KEYS = """
CREATE TABLE api_keys (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_id         text        NOT NULL UNIQUE,   -- non-secret, indexed lookup handle
    key_hash       text        NOT NULL,          -- unsalted SHA-256 hex (256-bit CSPRNG secret)
    prefix_display text        NOT NULL,          -- 'sk_live_31KEMo7bgFR3…zvtw' for the UI
    name           text        NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_used_at   timestamptz,                   -- throttled to <=1 write/hour
    expires_at     timestamptz,
    revoked_at     timestamptz
);
CREATE INDEX api_keys_user_idx ON api_keys (user_id) WHERE revoked_at IS NULL;
"""

CONNECTIONS = """
CREATE TABLE connections (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id              bigint         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider             provider_kind  NOT NULL,
    external_account_id  text           NOT NULL,   -- Google `sub` | Slack team_id:authed_user.id
    display_name         text           NOT NULL,
    status               conn_status    NOT NULL DEFAULT 'active',
    granted_scopes       text[]         NOT NULL DEFAULT '{}',

    -- encrypted token material: magic || key_version || nonce || ct||tag
    access_token_ct      bytea,
    refresh_token_ct     bytea,
    bot_token_ct         bytea,                     -- Slack: xoxb (search uses the user token)
    key_version          smallint       NOT NULL DEFAULT 1,
    access_expires_at    timestamptz,
    refresh_expires_at   timestamptz,               -- nullable; present only for rotating providers

    last_success_at      timestamptz,
    last_error_class     error_class,
    last_error_detail    text,
    created_at           timestamptz    NOT NULL DEFAULT now(),
    updated_at           timestamptz    NOT NULL DEFAULT now(),

    -- Identity survives reconnect: this is what "preserves identity" means.
    CONSTRAINT connections_natural_key UNIQUE (user_id, provider, external_account_id)
);
CREATE INDEX connections_user_idx ON connections (user_id, provider);
"""

SEARCHES = """
CREATE TABLE searches (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    query       text        NOT NULL,
    is_seed     boolean     NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX searches_user_recent_idx ON searches (user_id, created_at DESC);

CREATE TABLE adapter_runs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    search_id      uuid        NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    source         text        NOT NULL,              -- 'gmail' | 'slack' | 'web' - registry-driven
    connection_id  bigint      REFERENCES connections(id),  -- NULL for web
    status         run_status  NOT NULL DEFAULT 'pending',
    mode           source_mode NOT NULL DEFAULT 'live',
    result_count   int         NOT NULL DEFAULT 0,
    error_class    error_class,
    error_detail   text,
    started_at     timestamptz,
    finished_at    timestamptz
);
CREATE INDEX adapter_runs_search_idx ON adapter_runs (search_id);

CREATE TABLE search_results (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    search_id       uuid        NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    adapter_run_id  uuid        NOT NULL REFERENCES adapter_runs(id) ON DELETE CASCADE,

    -- the closed common shape, stored verbatim
    source          text        NOT NULL,
    external_id     text        NOT NULL,
    title           text        NOT NULL,
    snippet         text        NOT NULL,
    author          text,
    occurred_at     timestamptz,
    url             text        NOT NULL,

    -- ranking inputs live OUTSIDE the Result shape
    source_rank     int         NOT NULL,
    blended_score   double precision NOT NULL,

    -- Slack ToS: message bodies are cached, not archived
    body_expires_at timestamptz,

    CONSTRAINT search_results_shape_complete CHECK (
        length(source) > 0 AND length(external_id) > 0 AND
        length(title)  > 0 AND length(snippet)     > 0 AND length(url) > 0
    )
);
CREATE INDEX search_results_rank_idx ON search_results (search_id, blended_score DESC);
CREATE INDEX search_results_expiry_idx ON search_results (body_expires_at) WHERE body_expires_at IS NOT NULL;
"""

SENDS = """
CREATE TABLE drafts (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               bigint        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connection_id         bigint        NOT NULL REFERENCES connections(id),
    channel               provider_kind NOT NULL,
    recipient             text          NOT NULL,   -- email address | Slack channel id
    recipient_display     text          NOT NULL,   -- '#general' | the address, echoed verbatim
    subject               text,                     -- gmail only
    body                  text          NOT NULL,
    idempotency_key       text          NOT NULL,
    in_reply_to_result_id bigint        REFERENCES search_results(id),
    in_reply_to_external  text,                     -- provider message id, for threading
    created_at            timestamptz   NOT NULL DEFAULT now(),
    updated_at            timestamptz   NOT NULL DEFAULT now(),

    CONSTRAINT drafts_key_bounded  CHECK (char_length(idempotency_key) BETWEEN 16 AND 255),
    CONSTRAINT drafts_subject_only_email CHECK (channel = 'gmail' OR subject IS NULL)
);
CREATE UNIQUE INDEX drafts_user_key_idx ON drafts (user_id, idempotency_key);

CREATE TABLE sends (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    draft_id              uuid        NOT NULL REFERENCES drafts(id),
    connection_id         bigint      NOT NULL REFERENCES connections(id),
    provider              provider_kind NOT NULL,
    idempotency_key       text        NOT NULL,
    state                 send_state  NOT NULL DEFAULT 'in_flight',

    -- confirmation proof AND idempotency fingerprint, over channel||recipient||subject||body
    confirmed_sha256      char(64)    NOT NULL,

    provider_draft_id     text,       -- Gmail: the stable draft id - our reconciliation token
    provider_message_id   text,
    attempts              int         NOT NULL DEFAULT 0,
    reconcile_attempts    int         NOT NULL DEFAULT 0,
    dispatched_at         timestamptz,   -- COMMITTED IN ITS OWN TRANSACTION before the provider call
    delivered_at          timestamptz,
    last_error_class      error_class,
    last_error_detail     text,
    is_seed               boolean     NOT NULL DEFAULT false,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),

    -- The claim. One row per key, decided by the database, not by application logic.
    CONSTRAINT sends_user_key_uniq UNIQUE (user_id, idempotency_key),

    -- Delivered without provider evidence is unrepresentable.
    CONSTRAINT sends_delivered_has_evidence CHECK (
        state <> 'delivered' OR (provider_message_id IS NOT NULL AND delivered_at IS NOT NULL)
    ),
    -- Uncertain is only reachable after a dispatch was actually attempted.
    CONSTRAINT sends_uncertain_was_dispatched CHECK (
        state <> 'uncertain' OR dispatched_at IS NOT NULL
    )
);

CREATE INDEX sends_stale_idx     ON sends (dispatched_at)
    WHERE state = 'in_flight' AND dispatched_at IS NOT NULL;
CREATE INDEX sends_uncertain_idx ON sends (created_at DESC) WHERE state = 'uncertain';
CREATE INDEX sends_user_recent_idx ON sends (user_id, created_at DESC);
"""

JOBS = """
CREATE TABLE jobs (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind              job_kind    NOT NULL,
    ref_id            uuid        NOT NULL,     -- adapter_runs.id | sends.id
    dedupe_key        text,
    partition_key     text,                     -- 'gmail:<connection_id>' on sends; NULL on adapter runs

    state             job_state   NOT NULL DEFAULT 'ready',
    priority          smallint    NOT NULL DEFAULT 0,
    run_at            timestamptz NOT NULL DEFAULT now(),

    attempts          int         NOT NULL DEFAULT 0,
    max_attempts      int         NOT NULL DEFAULT 6,
    backoff_seconds   double precision,         -- so the UI renders a countdown, not a spinner

    claimed_at        timestamptz,
    claimed_by        text,                     -- host:pid - which worker had it
    lease_expires_at  timestamptz,

    started_at        timestamptz,              -- COALESCE(started_at, now()) - survives retries
    finished_at       timestamptz,

    last_error_class  error_class,
    last_error_detail text,                     -- capped at 16 KiB in application code
    last_error_at     timestamptz,

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT jobs_attempts_bounded CHECK (attempts >= 0 AND attempts <= max_attempts),
    CONSTRAINT jobs_lease_consistent CHECK (
        (state = 'running') = (claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
) WITH (fillfactor = 70);

-- Partial indexes: size tracks the BACKLOG, not the table.
-- No INCLUDE columns - locking forces heap access, so an index-only scan is impossible.
CREATE INDEX jobs_claim_idx ON jobs (priority DESC, run_at, id) WHERE state = 'ready';
CREATE INDEX jobs_lease_idx ON jobs (lease_expires_at)           WHERE state = 'running';
CREATE INDEX jobs_running_partition_idx ON jobs (partition_key)  WHERE state = 'running';
CREATE UNIQUE INDEX jobs_running_partition_uniq ON jobs (partition_key)
    WHERE state = 'running' AND partition_key IS NOT NULL;
CREATE UNIQUE INDEX jobs_dedupe_idx ON jobs (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND state IN ('ready','running');
CREATE INDEX jobs_ref_idx ON jobs (kind, ref_id);

ALTER TABLE jobs SET (
    autovacuum_vacuum_scale_factor  = 0.01,
    autovacuum_vacuum_threshold     = 1000,
    autovacuum_analyze_scale_factor = 0.02,
    autovacuum_vacuum_cost_limit    = 10000,
    autovacuum_vacuum_cost_delay    = 0
);
"""

AUDIT_AND_VIEWS = """
CREATE TABLE send_resolutions (          -- who resolved an in-doubt send, and how
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    send_id     uuid        NOT NULL REFERENCES sends(id) ON DELETE CASCADE,
    user_id     bigint      NOT NULL REFERENCES users(id),
    resolution  text        NOT NULL CHECK (resolution IN ('marked_delivered','forced_resend')),
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- `ready_now` excludes future-dated rows on purpose: a large backoff backlog is
-- not a stall, and conflating them is the classic ops-dashboard false alarm.
CREATE VIEW jobs_health AS
SELECT kind,
    count(*) FILTER (WHERE state='ready'   AND run_at <= now())          AS ready_now,
    count(*) FILTER (WHERE state='ready'   AND run_at >  now())          AS deferred,
    count(*) FILTER (WHERE state='running')                              AS running,
    count(*) FILTER (WHERE state='running' AND lease_expires_at < now()) AS lease_expired,
    count(*) FILTER (WHERE state='parked')                               AS parked,
    max(now() - run_at) FILTER (WHERE state='ready' AND run_at <= now()) AS oldest_ready_age
FROM jobs GROUP BY kind;
"""


def _statements(block: str) -> Iterator[str]:
    """Split a DDL block into single statements.

    asyncpg sends everything as a prepared statement and a prepared statement
    holds exactly one command, so a block handed over whole fails with
    ``cannot insert multiple commands into a prepared statement`` — which reads
    like a syntax error in the SQL rather than a driver constraint.

    Comments are stripped *before* splitting, because they contain semicolons
    ("-- nullable; present only for rotating providers") and splitting first cuts
    a ``CREATE TABLE`` in half. No ``--`` appears inside a string literal in this
    file, so line-stripping is sound here; anything cleverer would be a SQL
    parser nobody asked for.
    """
    code = "\n".join(line.partition("--")[0] for line in block.splitlines())
    for raw in code.split(";"):
        if stmt := raw.strip():
            yield stmt


def upgrade() -> None:
    for block in (ENUMS, API_KEYS, CONNECTIONS, SEARCHES, SENDS, JOBS, AUDIT_AND_VIEWS):
        for statement in _statements(block):
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS jobs_health")
    for table in (
        "send_resolutions",
        "jobs",
        "sends",
        "drafts",
        "search_results",
        "adapter_runs",
        "searches",
        "connections",
        "api_keys",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum in (
        "source_mode",
        "run_status",
        "send_state",
        "error_class",
        "job_state",
        "job_kind",
        "conn_status",
        "provider_kind",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum}")
