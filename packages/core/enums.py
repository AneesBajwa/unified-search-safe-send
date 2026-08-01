"""The enumerations from openspec ``data-model.md``, and the one helper that
binds them to their PostgreSQL types.

Kept out of ``models.py`` so ``errors.py`` — which classifies provider failures
and has no business touching the ORM — can import ``ErrorClass`` without
dragging the whole schema in behind it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy.dialects.postgresql import ENUM


class ProviderKind(StrEnum):
    GMAIL = "gmail"
    SLACK = "slack"


class ConnStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    ERRORED = "errored"
    NEEDS_RECONNECT = "needs_reconnect"


class JobKind(StrEnum):
    ADAPTER_RUN = "adapter_run"
    SEND = "send"


class JobState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Terminal *transient*: the attempt ceiling was reached. Deliberately
    # distinct from `failed` so "we gave up retrying" is never confused with
    # "the provider told us no" — only one of the two is worth an operator
    # retry (openspec task 3.5).
    PARKED = "parked"
    CANCELLED = "cancelled"


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    NEEDS_RECONNECT = "needs_reconnect"
    # Our bug, not the user's. `invalid_client`, `deleted_client`, a malformed
    # request. Rendering this as "reconnect your account" sends the user round
    # in circles reconnecting a perfectly good grant (risks.md R24).
    CONFIG = "config"


class SendState(StrEnum):
    IN_FLIGHT = "in_flight"
    DELIVERED = "delivered"
    FAILED_PERMANENT = "failed_permanent"
    FAILED_TRANSIENT = "failed_transient"
    UNCERTAIN = "uncertain"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    NEEDS_RECONNECT = "needs_reconnect"


class SourceMode(StrEnum):
    LIVE = "live"
    MOCK = "mock"
    DEGRADED = "degraded"


def pg_enum(python_enum: type[StrEnum], name: str) -> ENUM:
    """Bind a Python enum to an existing PostgreSQL enum type.

    Two non-obvious arguments, both load-bearing:

    ``values_callable`` — SQLAlchemy persists a Python enum by its **member
    name** (``ADAPTER_RUN``), not its value. Our labels are the lowercase
    values, so without this every insert fails on an invalid enum label.

    ``create_type=False`` — the types are created once by the migration. Left
    default, SQLAlchemy tries to emit ``CREATE TYPE`` per table that mentions
    the type and the second one errors.
    """
    return ENUM(
        python_enum,
        name=name,
        create_type=False,
        values_callable=_values,
    )


def _values(enum_cls: Any) -> list[str]:
    return [member.value for member in enum_cls]
