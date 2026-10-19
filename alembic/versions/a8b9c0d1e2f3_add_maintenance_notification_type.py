"""Add MAINTENANCE value to notificationtype enum (#28)

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-10-28

NotificationType.MAINTENANCE existed in the model but had no PG label —
the first dispatch/template typed maintenance would fail at insert.
Also aligns the notif_preferences server default: the API accepts a
'maintenance' preference key that the persisted JSON previously lacked
(existing rows hydrate via the Python-side field default).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Same pattern as f7a8b9c0d1e2's ALARM add: PG12+ allows ADD VALUE in a
    # transaction; the new value can't be used in this transaction — none is.
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'MAINTENANCE'")
    op.execute(
        "ALTER TABLE responders ALTER COLUMN notif_preferences SET DEFAULT "
        "'{\"warning\": true, \"critical\": true, \"blockage\": true, "
        "\"announcement\": true, \"maintenance\": true}'::json"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE responders ALTER COLUMN notif_preferences SET DEFAULT "
        "'{\"warning\": true, \"critical\": true, \"blockage\": true, "
        "\"announcement\": true}'::json"
    )
    # Postgres cannot drop a single enum value — removing 'MAINTENANCE' would
    # mean recreating notificationtype and rewriting every dependent column.
