"""users

The walking skeleton's one table. Deliberately real rather than a throwaway
probe: this is openspec task 2.1's ``users`` shape from data-model.md, so
phase 1 extends it rather than replacing it.

Revision ID: 0001
Revises:
Create Date: 2026-07-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # citext gives case-insensitive uniqueness in the type rather than in
    # every query. Available on Neon; IF NOT EXISTS keeps re-runs clean.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("email", CITEXT(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("email", name="users_email_key"),
    )


def downgrade() -> None:
    op.drop_table("users")
    # The extension is left in place: dropping it would break any other
    # citext column a concurrent branch may have added.
