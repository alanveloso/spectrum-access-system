"""Generic-only persistence domain (admin_injected_data).

Revision ID: 20260904_generic_0001
Revises:
Create Date: 2026-09-04

Selected when PersistencePlan activates only the generic contribution.
Historical full-schema installs remain on ``20260808_0001`` and are not rewritten.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_generic_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_injected_data",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_injected_data_kind", "admin_injected_data", ["kind"]
    )


def downgrade() -> None:
    op.drop_table("admin_injected_data")
