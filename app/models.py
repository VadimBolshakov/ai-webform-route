from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _now() -> dict:
    return {"server_default": func.now()}


def _ts(**kw) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("username ~ '^[a-z0-9_.-]{3,32}$'", name="ck_users_username"),
        CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
        CheckConstraint("answer_language IN ('ru', 'en')", name="ck_users_answer_language"),
        CheckConstraint("theme IN ('light', 'dark', 'system')", name="ck_users_theme"),
        CheckConstraint("char_length(system_prompt) <= 2000", name="ck_users_system_prompt_len"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(10))
    totp_secret_enc: Mapped[str | None] = mapped_column(String(255))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    monthly_limit_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    answer_language: Mapped[str] = mapped_column(String(2), default="ru", server_default="ru")
    theme: Mapped[str] = mapped_column(String(6), default="system", server_default="system")
    system_prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    default_model: Mapped[str] = mapped_column(
        String(200), default="openrouter/auto", server_default="openrouter/auto"
    )
    policy_accepted_at: Mapped[datetime | None] = _ts(nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(**_now())


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = _ts()
    used_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = _ts(nullable=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    id_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = _ts()
    ip: Mapped[str] = mapped_column(String(45))
    user_agent: Mapped[str] = mapped_column(String(512), default="", server_default="")
    created_at: Mapped[datetime] = _ts(**_now())
    last_seen: Mapped[datetime] = _ts(**_now())
    totp_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (Index("ix_chats_user_updated", "user_id", desc("updated_at")),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200), default="Новый чат", server_default="Новый чат")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = _ts(**_now())
    updated_at: Mapped[datetime] = _ts(**_now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        Index("ix_messages_chat_id", "chat_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    model: Mapped[str | None] = mapped_column(String(200))
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = _ts(**_now())


class Usage(Base):
    __tablename__ = "usage"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'error', 'interrupted')", name="ck_usage_status"),
        Index("ix_usage_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    user_label: Mapped[str] = mapped_column(String(64))
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("chats.id", ondelete="SET NULL"))
    model: Mapped[str] = mapped_column(String(200))
    generation_id: Mapped[str | None] = mapped_column(String(100))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(12))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(**_now())


class AllowedModel(Base):
    __tablename__ = "allowed_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    supports_images: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    user_label: Mapped[str] = mapped_column(String(64), default="", server_default="")
    ip: Mapped[str] = mapped_column(String(45), default="", server_default="")
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _ts(**_now())
