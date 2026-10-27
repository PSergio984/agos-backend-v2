"""Seed escalation settings (#35)

Revision ID: d7e8f9a0b1c2
Revises: c5d6e7f8a9b0
Create Date: 2026-10-28 09:00:00.000000

escalation_check_job reads `escalation_timeout_minutes` and
`max_escalation_count` from system_settings — both were only ever set in
test fixtures, so the job raised 404 every run and no unacknowledged
alarm ever re-pinged in production.

Values are the #25 decision: ~5 min to ack, ONE re-push, then the
un-acked responders are the dispatcher's problem (human escalation —
radio/phone — not another automated channel).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SETTINGS = [
    ("escalation_timeout_minutes", "5"),
    ("max_escalation_count", "1"),
]


def upgrade() -> None:
    for key, value in SETTINGS:
        # Values are module constants, not user input — literal interpolation.
        op.execute(
            sa.text(
                f"INSERT INTO system_settings (key, json_value) "
                f"VALUES (:key, '{value}'::jsonb) "
                f"ON CONFLICT (key) DO NOTHING"
            ).bindparams(key=key)
        )


def downgrade() -> None:
    for key, _ in SETTINGS:
        op.execute(
            sa.text("DELETE FROM system_settings WHERE key = :key").bindparams(
                key=key
            )
        )
