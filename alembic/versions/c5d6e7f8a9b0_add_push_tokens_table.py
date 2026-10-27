"""add push_tokens table (#33)

Revision ID: c5d6e7f8a9b0
Revises: b9c0d1e2f3a4
Create Date: 2026-10-27 14:11:59.063228

Expo push tokens for the native responder app — a separate table from
push_subscriptions so VAPID subs and Expo tokens coexist per responder.

Autogen also detected indexes present in the DB but not in model metadata
(pre-existing drift — those indexes were created by migration DDL and are
intentional); those unrelated drop_index/create_index ops were trimmed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, Sequence[str], None] = 'b9c0d1e2f3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('push_tokens',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('responder_id', sa.UUID(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('platform', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('UTC', now())"), nullable=False),
    sa.ForeignKeyConstraint(['responder_id'], ['responders.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token', name='uq_push_token_token')
    )


def downgrade() -> None:
    op.drop_table('push_tokens')
