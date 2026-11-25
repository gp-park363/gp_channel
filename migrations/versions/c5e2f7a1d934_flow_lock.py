"""flow lock: content written before a confirm step sends the item back

Revision ID: c5e2f7a1d934
Revises: b1a48d786894
Create Date: 2026-11-25 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e2f7a1d934"
down_revision: str | Sequence[str] | None = "b1a48d786894"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("flow_submission", "flow_task"):
        op.add_column(
            table,
            sa.Column("locked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        )


def downgrade() -> None:
    for table in ("flow_task", "flow_submission"):
        op.drop_column(table, "locked")
