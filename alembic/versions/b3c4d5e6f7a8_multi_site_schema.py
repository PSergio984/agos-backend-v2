"""multi-site schema

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-10-22

Multi-site scalability (spec: agos-backend-v2#10):
- responder_deployments: temporary/additional site postings
- responders.active_location_id: which eligible site a responder is on
- notification_dispatches.location_id: the site a dispatch belongs to
  (NULL = global announcement); existing rows backfill to the first location
- groups.location_id: per-site responder groups (backfill to first location)
- drop UNIQUE(location_id) on sensor_devices/camera_devices: hybrid density
- drop responders.location_id server_default: site must be chosen explicitly
- locations.is_active: deactivate a site without deleting history
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "responder_deployments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "responder_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("responders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id"),
            nullable=False,
        ),
        sa.Column(
            "starts_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.timezone("UTC", sa.func.now()),
            nullable=False,
        ),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id"),
            nullable=False,
        ),
    )

    op.add_column(
        "responders",
        sa.Column(
            "active_location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
    )
    op.alter_column("responders", "location_id", server_default=None)

    op.add_column(
        "notification_dispatches",
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "groups",
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
    )
    # Historical dispatches/groups were all site-1 data. Backfill to the first
    # location when one exists; on an empty DB they stay NULL (announcement).
    op.execute(
        "UPDATE notification_dispatches SET location_id = "
        "(SELECT id FROM locations ORDER BY id LIMIT 1) "
        "WHERE location_id IS NULL"
    )
    op.execute(
        "UPDATE groups SET location_id = "
        "(SELECT id FROM locations ORDER BY id LIMIT 1) "
        "WHERE location_id IS NULL"
    )

    op.drop_constraint(
        "sensor_devices_location_id_key", "sensor_devices", type_="unique"
    )
    op.drop_constraint(
        "camera_devices_location_id_key", "camera_devices", type_="unique"
    )
    # Per-site groups reuse the same name ("All Active Responders" per site).
    op.drop_constraint("groups_name_key", "groups", type_="unique")

    op.add_column(
        "locations",
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("locations", "is_active")
    op.create_unique_constraint("groups_name_key", "groups", ["name"])
    op.create_unique_constraint(
        "camera_devices_location_id_key", "camera_devices", ["location_id"]
    )
    op.create_unique_constraint(
        "sensor_devices_location_id_key", "sensor_devices", ["location_id"]
    )
    op.drop_column("groups", "location_id")
    op.drop_column("notification_dispatches", "location_id")
    op.alter_column("responders", "location_id", server_default="1")
    op.drop_column("responders", "active_location_id")
    op.drop_table("responder_deployments")
