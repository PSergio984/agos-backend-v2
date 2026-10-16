"""Add ALARM value to notificationtype enum (T8)

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-10-24

Alarm tier on notification_dispatches.type — a dispatch carrying
location_id (NULL = barangay-wide), never a parallel channel (#21).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG12+ allows ADD VALUE inside a transaction; the new value simply cannot
    # be used within the same transaction — this migration writes no rows.
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'ALARM'")


def downgrade() -> None:
    # Postgres cannot drop a single enum value — removing 'ALARM' would mean
    # recreating notificationtype and rewriting every dependent column.
    pass
