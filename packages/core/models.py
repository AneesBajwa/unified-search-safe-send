"""ORM models.

Phase 0 defines only ``users`` — the walking skeleton needs exactly one table
to prove ``alembic upgrade head`` works end to end. The remaining nine tables
land in phase 1 from the authoritative DDL in openspec data-model.md.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Identity, func
from sqlalchemy.dialects.postgresql import CITEXT
from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, Identity(always=True), primary_key=True),
    )
    # citext, not lower(email): case-insensitive uniqueness belongs in the
    # type so no query can accidentally bypass it.
    email: str = Field(sa_column=Column(CITEXT, nullable=False, unique=True))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now()
        ),
    )
