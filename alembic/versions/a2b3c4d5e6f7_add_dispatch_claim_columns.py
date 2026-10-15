"""add dispatch claim columns

Revision ID: a2b3c4d5e6f7
Revises: f6a7b8c9d0e1
Create Date: 2026-10-15

"On it" claim on shared dispatches — claimed_by/claimed_at so the crew
sees who's handling a blockage alert (spec: agos-patrol-mobile#16).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_dispatches",
        sa.Column("claimed_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "notification_dispatches",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_notification_dispatches_claimed_by",
        "notification_dispatches",
        "responders",
        ["claimed_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_notification_dispatches_claimed_by",
        "notification_dispatches",
        type_="foreignkey",
    )
    op.drop_column("notification_dispatches", "claimed_at")
    op.drop_column("notification_dispatches", "claimed_by")
