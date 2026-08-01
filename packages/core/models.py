"""ORM models — a faithful transcription of openspec ``data-model.md``.

`data-model.md` is the authority; this file is the Python view of it. Where a
constraint looks like defensive paranoia it is encoding an invariant the code
would otherwise have to be *trusted* to maintain — `sends_delivered_has_evidence`
is the clearest example: a row claiming delivery without provider evidence is
not merely discouraged, it is unrepresentable.

**Indexes, views, `fillfactor` and the per-table autovacuum settings live in
the migration, not here.** Several are partial or expression-based, Alembic
cannot round-trip them faithfully, and this project hand-writes migrations
rather than autogenerating them — so one honest source (the migration) beats
two that drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlmodel import Field, SQLModel

from core.enums import (
    ConnStatus,
    ErrorClass,
    JobKind,
    JobState,
    ProviderKind,
    RunStatus,
    SendState,
    SourceMode,
    pg_enum,
)


def _uuid_pk() -> Column[uuid.UUID]:
    return Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )


def _bigint_pk() -> Column[int]:
    return Column(BigInteger, Identity(always=True), primary_key=True, nullable=False)


def _created_at() -> Column[datetime]:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Identity and access
# ---------------------------------------------------------------------------


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, sa_column=_bigint_pk())
    # citext, not lower(email): case-insensitive uniqueness belongs in the
    # type so no query can accidentally bypass it.
    email: str = Field(sa_column=Column(CITEXT, nullable=False, unique=True))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class ApiKey(SQLModel, table=True):
    """No plaintext column and no reveal endpoint — by construction, not by policy.

    ``key_hash`` is an *unsalted* SHA-256. That is the right tool here and
    bcrypt would be the wrong one: the secret is 256 bits of CSPRNG output, so
    there is no dictionary to defend against, while bcrypt would spend ~100 ms
    of billed CPU per request and truncates at 72 bytes (design D8, task 9.1).
    """

    __tablename__ = "api_keys"

    id: int | None = Field(default=None, sa_column=_bigint_pk())
    user_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        )
    )
    # Non-secret lookup handle, so authentication is an indexed hit on a
    # column that is safe to log.
    key_id: str = Field(sa_column=Column(Text, nullable=False, unique=True))
    key_hash: str = Field(sa_column=Column(Text, nullable=False))
    prefix_display: str = Field(sa_column=Column(Text, nullable=False))
    name: str = Field(sa_column=Column(Text, nullable=False, server_default=text("''")))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    # Throttled to at most one write an hour: an unthrottled "last used"
    # column turns every authenticated request into a write.
    last_used_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


class Connection(SQLModel, table=True):
    """The natural key is what makes reconnect an UPDATE rather than an INSERT.

    That is the whole of what "reconnect preserves identity" means: every
    historical row pointing at this connection survives, because the row it
    points at is the same row.
    """

    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", "external_account_id", name="connections_natural_key"
        ),
    )

    id: int | None = Field(default=None, sa_column=_bigint_pk())
    user_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        )
    )
    provider: ProviderKind = Field(
        sa_column=Column(pg_enum(ProviderKind, "provider_kind"), nullable=False)
    )
    # Google `sub` | Slack `team_id:authed_user.id`
    external_account_id: str = Field(sa_column=Column(Text, nullable=False))
    display_name: str = Field(sa_column=Column(Text, nullable=False))
    status: ConnStatus = Field(
        default=ConnStatus.ACTIVE,
        sa_column=Column(
            pg_enum(ConnStatus, "conn_status"),
            nullable=False,
            server_default=text("'active'"),
        ),
    )
    granted_scopes: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
        ),
    )

    # Encrypted token material: magic ‖ key_version ‖ nonce ‖ ct‖tag.
    access_token_ct: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary)
    )
    refresh_token_ct: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary)
    )
    # Slack ships two tokens per install. Search needs the *user* token; every
    # other call takes the bot token (risks.md R4).
    bot_token_ct: bytes | None = Field(default=None, sa_column=Column(LargeBinary))
    key_version: int = Field(
        default=1,
        sa_column=Column(SmallInteger, nullable=False, server_default=text("1")),
    )
    access_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # Nullable and unused today, present so enabling Slack token rotation
    # later is a config change rather than a migration.
    refresh_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )

    last_success_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    last_error_class: ErrorClass | None = Field(
        default=None, sa_column=Column(pg_enum(ErrorClass, "error_class"))
    )
    last_error_detail: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_created_at())


# ---------------------------------------------------------------------------
# Searches and adapter runs
# ---------------------------------------------------------------------------


class Search(SQLModel, table=True):
    __tablename__ = "searches"

    id: uuid.UUID | None = Field(default=None, sa_column=_uuid_pk())
    user_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        )
    )
    query: str = Field(sa_column=Column(Text, nullable=False))
    # Seed-ness is a property of the row, so no view can accidentally blur
    # demo data into real history (design D12).
    is_seed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class AdapterRun(SQLModel, table=True):
    """``source`` is ``text``, not an enum, and that is deliberate.

    An enum would make adding a fourth adapter a migration — contradicting the
    "one file plus one registry line" guarantee the source-agnosticism test
    defends (design D6).
    """

    __tablename__ = "adapter_runs"

    id: uuid.UUID | None = Field(default=None, sa_column=_uuid_pk())
    search_id: uuid.UUID = Field(
        sa_column=Column(
            UUID(as_uuid=True),
            ForeignKey("searches.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    source: str = Field(sa_column=Column(Text, nullable=False))
    # NULL for web, which needs no connection at all — the zero-connections
    # case has to be representable.
    connection_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, ForeignKey("connections.id"))
    )
    status: RunStatus = Field(
        default=RunStatus.PENDING,
        sa_column=Column(
            pg_enum(RunStatus, "run_status"),
            nullable=False,
            server_default=text("'pending'"),
        ),
    )
    mode: SourceMode = Field(
        default=SourceMode.LIVE,
        sa_column=Column(
            pg_enum(SourceMode, "source_mode"),
            nullable=False,
            server_default=text("'live'"),
        ),
    )
    result_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    error_class: ErrorClass | None = Field(
        default=None, sa_column=Column(pg_enum(ErrorClass, "error_class"))
    )
    error_detail: str | None = Field(default=None, sa_column=Column(Text))
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class SearchResult(SQLModel, table=True):
    """``search_results_shape_complete`` makes normalization a database invariant.

    A half-populated ``Result`` cannot be stored, so the API cannot serve one.
    The spec's "every result has source/id/title/snippet/url populated" is
    enforced rather than asserted.
    """

    __tablename__ = "search_results"
    __table_args__ = (
        CheckConstraint(
            "length(source) > 0 AND length(external_id) > 0 AND "
            "length(title) > 0 AND length(snippet) > 0 AND length(url) > 0",
            name="search_results_shape_complete",
        ),
    )

    id: int | None = Field(default=None, sa_column=_bigint_pk())
    search_id: uuid.UUID = Field(
        sa_column=Column(
            UUID(as_uuid=True),
            ForeignKey("searches.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    adapter_run_id: uuid.UUID = Field(
        sa_column=Column(
            UUID(as_uuid=True),
            ForeignKey("adapter_runs.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    # The closed common shape, stored verbatim.
    source: str = Field(sa_column=Column(Text, nullable=False))
    external_id: str = Field(sa_column=Column(Text, nullable=False))
    title: str = Field(sa_column=Column(Text, nullable=False))
    snippet: str = Field(sa_column=Column(Text, nullable=False))
    author: str | None = Field(default=None, sa_column=Column(Text))
    occurred_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    url: str = Field(sa_column=Column(Text, nullable=False))

    # Ranking inputs live OUTSIDE the Result shape (design D7) — they are
    # columns here and envelope fields in the API, never keys on the result.
    source_rank: int = Field(sa_column=Column(Integer, nullable=False))
    blended_score: float = Field(sa_column=Column(Float, nullable=False))

    # Slack ToS: message bodies are cached, not archived. Identifiers and
    # permalinks persist; the body expires (risks.md R19).
    body_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


# ---------------------------------------------------------------------------
# Drafts and sends
# ---------------------------------------------------------------------------


class Draft(SQLModel, table=True):
    __tablename__ = "drafts"
    __table_args__ = (
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 16 AND 255",
            name="drafts_key_bounded",
        ),
        # Encodes the Draft interface's `subject?: string // email only`
        # comment as something the database will actually refuse.
        CheckConstraint(
            "channel = 'gmail' OR subject IS NULL", name="drafts_subject_only_email"
        ),
    )

    id: uuid.UUID | None = Field(default=None, sa_column=_uuid_pk())
    user_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        )
    )
    connection_id: int = Field(
        sa_column=Column(BigInteger, ForeignKey("connections.id"), nullable=False)
    )
    channel: ProviderKind = Field(
        sa_column=Column(pg_enum(ProviderKind, "provider_kind"), nullable=False)
    )
    # email address | Slack channel id
    recipient: str = Field(sa_column=Column(Text, nullable=False))
    # '#general' | the address, echoed verbatim — what the confirm step shows.
    recipient_display: str = Field(sa_column=Column(Text, nullable=False))
    subject: str | None = Field(default=None, sa_column=Column(Text))
    body: str = Field(sa_column=Column(Text, nullable=False))
    idempotency_key: str = Field(sa_column=Column(Text, nullable=False))
    in_reply_to_result_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, ForeignKey("search_results.id"))
    )
    in_reply_to_external: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_created_at())


class Send(SQLModel, table=True):
    """The exactly-once row.

    ``sends_user_key_uniq`` *is* the claim: one row per key, decided by the
    database rather than by application logic. Everything else in the send
    gate is bookkeeping around that one constraint.
    """

    __tablename__ = "sends"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="sends_user_key_uniq"),
        # Delivered without provider evidence is unrepresentable.
        CheckConstraint(
            "state <> 'delivered' OR "
            "(provider_message_id IS NOT NULL AND delivered_at IS NOT NULL)",
            name="sends_delivered_has_evidence",
        ),
        # Uncertain is only reachable after a dispatch was actually attempted.
        CheckConstraint(
            "state <> 'uncertain' OR dispatched_at IS NOT NULL",
            name="sends_uncertain_was_dispatched",
        ),
    )

    id: uuid.UUID | None = Field(default=None, sa_column=_uuid_pk())
    user_id: int = Field(
        sa_column=Column(
            BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        )
    )
    draft_id: uuid.UUID = Field(
        sa_column=Column(
            UUID(as_uuid=True), ForeignKey("drafts.id"), nullable=False
        )
    )
    connection_id: int = Field(
        sa_column=Column(BigInteger, ForeignKey("connections.id"), nullable=False)
    )
    provider: ProviderKind = Field(
        sa_column=Column(pg_enum(ProviderKind, "provider_kind"), nullable=False)
    )
    idempotency_key: str = Field(sa_column=Column(Text, nullable=False))
    state: SendState = Field(
        default=SendState.IN_FLIGHT,
        sa_column=Column(
            pg_enum(SendState, "send_state"),
            nullable=False,
            server_default=text("'in_flight'"),
        ),
    )

    # Confirmation proof AND idempotency fingerprint, over
    # channel ‖ recipient ‖ subject ‖ body — never the body alone, or editing
    # the recipient after confirming would slip through unnoticed.
    confirmed_sha256: str = Field(sa_column=Column(String(64), nullable=False))

    # Gmail's stable draft id — our reconciliation token (risks.md R3).
    provider_draft_id: str | None = Field(default=None, sa_column=Column(Text))
    provider_message_id: str | None = Field(default=None, sa_column=Column(Text))
    attempts: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    reconcile_attempts: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    # COMMITTED IN ITS OWN TRANSACTION before the provider call. That commit is
    # the crash evidence the whole reconciliation design rests on (risks.md R28).
    dispatched_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    delivered_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    last_error_class: ErrorClass | None = Field(
        default=None, sa_column=Column(pg_enum(ErrorClass, "error_class"))
    )
    last_error_detail: str | None = Field(default=None, sa_column=Column(Text))
    is_seed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_created_at())


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class Job(SQLModel, table=True):
    """The queue. One row per adapter run and per send.

    ``jobs_lease_consistent`` is the interesting constraint: it makes
    "running" and "holds a lease" the same fact, so a claim that forgot to set
    a lease — or a completion that forgot to clear one — is rejected by the
    database instead of quietly producing a job nothing will ever reclaim.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts", name="jobs_attempts_bounded"
        ),
        CheckConstraint(
            "(state = 'running') = "
            "(claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="jobs_lease_consistent",
        ),
        # NB: `WITH (fillfactor = 70)` and the per-table autovacuum settings are
        # applied by the migration. SQLAlchemy's PostgreSQL dialect has no
        # table-level storage-parameter option, so there is nowhere to say it
        # here — `postgresql_with` is an Index kwarg and raises on a Table.
    )

    id: int | None = Field(default=None, sa_column=_bigint_pk())
    kind: JobKind = Field(
        sa_column=Column(pg_enum(JobKind, "job_kind"), nullable=False)
    )
    # adapter_runs.id | sends.id
    ref_id: uuid.UUID = Field(
        sa_column=Column(UUID(as_uuid=True), nullable=False)
    )
    dedupe_key: str | None = Field(default=None, sa_column=Column(Text))
    # 'gmail:<connection_id>' on sends; NULL on adapter runs, so fan-out stays
    # fully parallel while sends serialize per connection.
    partition_key: str | None = Field(default=None, sa_column=Column(Text))

    state: JobState = Field(
        default=JobState.READY,
        sa_column=Column(
            pg_enum(JobState, "job_state"),
            nullable=False,
            server_default=text("'ready'"),
        ),
    )
    priority: int = Field(
        default=0,
        sa_column=Column(SmallInteger, nullable=False, server_default=text("0")),
    )
    run_at: datetime | None = Field(default=None, sa_column=_created_at())

    attempts: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    max_attempts: int = Field(
        default=6, sa_column=Column(Integer, nullable=False, server_default=text("6"))
    )
    # Persisted solely so the UI can render a real countdown instead of an
    # indeterminate spinner. Never used to compute the schedule (task 3.5c).
    backoff_seconds: float | None = Field(default=None, sa_column=Column(Float))

    claimed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # host:pid — which worker had it. Names the process to look at in logs.
    claimed_by: str | None = Field(default=None, sa_column=Column(Text))
    lease_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )

    # COALESCE(started_at, now()) on claim, so it survives retries and yields
    # two distinct latencies: end-to-end including backoff, and last-attempt
    # duration (task 2.7d).
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )

    last_error_class: ErrorClass | None = Field(
        default=None, sa_column=Column(pg_enum(ErrorClass, "error_class"))
    )
    # Capped at 16 KiB in application code (core.jobs.queue): the spec forbids
    # generic messages, but a provider HTML error page per row would
    # TOAST-churn the hottest table in the system.
    last_error_detail: str | None = Field(default=None, sa_column=Column(Text))
    last_error_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )

    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_created_at())


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class SendResolution(SQLModel, table=True):
    """Who resolved an in-doubt send, and how. An `uncertain` send is resolved
    by a human decision, so the decision itself is a record."""

    __tablename__ = "send_resolutions"
    __table_args__ = (
        CheckConstraint(
            "resolution IN ('marked_delivered','forced_resend')",
            name="send_resolutions_resolution_check",
        ),
    )

    id: int | None = Field(default=None, sa_column=_bigint_pk())
    send_id: uuid.UUID = Field(
        sa_column=Column(
            UUID(as_uuid=True),
            ForeignKey("sends.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    user_id: int = Field(
        sa_column=Column(BigInteger, ForeignKey("users.id"), nullable=False)
    )
    resolution: str = Field(sa_column=Column(Text, nullable=False))
    note: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
