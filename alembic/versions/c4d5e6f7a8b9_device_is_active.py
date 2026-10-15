"""device is_active flags

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-10-22

Multi-site scalability (spec: agos-backend-v2#10, ticket T6/#16):
- sensor_devices.is_active / camera_devices.is_active: deactivate a device
  without deleting history — the registry's "Deactivate" action flips this
  flag, and an inactive device is rejected on the camera ingest path.
"""

import sqlalchemy as sa

from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sensor_devices",
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "camera_devices",
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("camera_devices", "is_active")
    op.drop_column("sensor_devices", "is_active")
