"""users.totp_last_step for TOTP replay protection

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17 18:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('totp_last_step', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'totp_last_step')
