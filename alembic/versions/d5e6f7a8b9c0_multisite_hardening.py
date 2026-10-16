"""multi-site hardening

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-10-22

Review fixes for agos-backend-v2#10:
- partial unique index: at most ONE open deployment per responder
  (the spec'd invariant; services also serialize via row lock)
- composite unique groups(name, location_id): per-site groups reuse a name
  ("All Active Responders") without allowing duplicates within a site
- camera_devices.stream_url: the camera's stream/upload config field
"""

import sqlalchemy as sa

from alembic import op

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_responder_deployments_one_open",
        "responder_deployments",
        ["responder_id"],
        unique=True,
        postgresql_where=sa.text("ends_at IS NULL"),
    )
    # NULLS NOT DISTINCT (PG15+): NULL-site groups are the legacy/global kind
    # and must keep the old groups_name_key name-uniqueness among themselves.
    op.create_unique_constraint(
        "uq_groups_name_location",
        "groups",
        ["name", "location_id"],
        postgresql_nulls_not_distinct=True,
    )
    op.add_column(
        "camera_devices",
        sa.Column("stream_url", sa.String(500), nullable=True),
    )
    # Planned posting window (metadata); ends_at stays the ACTUAL close marker
    # so "open ⇔ ends_at IS NULL" — the partial index — stays exact.
    op.add_column(
        "responder_deployments",
        sa.Column("planned_ends_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("responder_deployments", "planned_ends_at")
    op.drop_column("camera_devices", "stream_url")
    op.drop_constraint("uq_groups_name_location", "groups", type_="unique")
    op.drop_index(
        "uq_responder_deployments_one_open", table_name="responder_deployments"
    )
