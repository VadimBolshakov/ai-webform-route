"""initial

Revision ID: 0001
Revises:
Create Date: 2026-10-17 14:53:39.179070
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('allowed_models',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('model_id', sa.String(length=200), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('supports_images', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('model_id')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=32), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=10), nullable=False),
    sa.Column('totp_secret_enc', sa.String(length=255), nullable=True),
    sa.Column('totp_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('monthly_limit_usd', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('answer_language', sa.String(length=2), server_default='ru', nullable=False),
    sa.Column('theme', sa.String(length=6), server_default='system', nullable=False),
    sa.Column('system_prompt', sa.Text(), server_default='', nullable=False),
    sa.Column('default_model', sa.String(length=200), server_default='openrouter/auto', nullable=False),
    sa.Column('policy_accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('failed_logins', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("answer_language IN ('ru', 'en')", name='ck_users_answer_language'),
    sa.CheckConstraint("role IN ('admin', 'user')", name='ck_users_role'),
    sa.CheckConstraint("theme IN ('light', 'dark', 'system')", name='ck_users_theme'),
    sa.CheckConstraint("username ~ '^[a-z0-9_.-]{3,32}$'", name='ck_users_username'),
    sa.CheckConstraint('char_length(system_prompt) <= 2000', name='ck_users_system_prompt_len'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('username')
    )
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('user_label', sa.String(length=64), server_default='', nullable=False),
    sa.Column('ip', sa.String(length=45), server_default='', nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('auth_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('id_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('csrf_token', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ip', sa.String(length=45), nullable=False),
    sa.Column('user_agent', sa.String(length=512), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('totp_pending', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id_hash')
    )
    op.create_index(op.f('ix_auth_sessions_user_id'), 'auth_sessions', ['user_id'], unique=False)
    op.create_table('chats',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), server_default='Новый чат', nullable=False),
    sa.Column('pinned', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_chats_user_updated', 'chats', ['user_id', sa.text('updated_at DESC')], unique=False)
    op.create_table('invites',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_by_id', sa.Integer(), nullable=True),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['used_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_table('messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=10), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('model', sa.String(length=200), nullable=True),
    sa.Column('interrupted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('user', 'assistant')", name='ck_messages_role'),
    sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_messages_chat_id', 'messages', ['chat_id', 'id'], unique=False)
    op.create_table('usage',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('user_label', sa.String(length=64), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=True),
    sa.Column('model', sa.String(length=200), nullable=False),
    sa.Column('generation_id', sa.String(length=100), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('latency_ms', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ok', 'error', 'interrupted')", name='ck_usage_status'),
    sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_usage_user_created', 'usage', ['user_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_usage_user_created', table_name='usage')
    op.drop_table('usage')
    op.drop_index('ix_messages_chat_id', table_name='messages')
    op.drop_table('messages')
    op.drop_table('invites')
    op.drop_index('ix_chats_user_updated', table_name='chats')
    op.drop_table('chats')
    op.drop_index(op.f('ix_auth_sessions_user_id'), table_name='auth_sessions')
    op.drop_table('auth_sessions')
    op.drop_table('audit_log')
    op.drop_table('users')
    op.drop_table('allowed_models')
