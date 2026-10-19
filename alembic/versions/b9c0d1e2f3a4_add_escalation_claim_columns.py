"""Add escalation claim/marker columns (#26)

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-10-28

- notification_dispatches.is_escalation: marks dispatches spawned by the
  escalation job so their deliveries never re-qualify — terminates the
  unbounded re-escalation chain.
- notification_deliveries.last_escalated_at: claim timestamp written under
  the FOR UPDATE lock before pings go out; the overdue predicate uses
  COALESCE(last_escalated_at, sent_at) so claimed rows can't be re-selected
  within the timeout window.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notification_dispatches",
        sa.Column("is_escalation", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("last_escalated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_deliveries", "last_escalated_at")
    op.drop_column("notification_dispatches", "is_escalation")
