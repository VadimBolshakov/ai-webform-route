import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Chat, Message, Usage, User


def make_user(**kw) -> User:
    base = dict(username="alice", password_hash="x", role="user", monthly_limit_usd=5)
    base.update(kw)
    return User(**base)


async def test_delete_user_cascades_chats_and_nulls_usage(db):
    user = make_user()
    db.add(user)
    await db.flush()
    chat = Chat(user_id=user.id)
    db.add(chat)
    await db.flush()
    db.add(Message(chat_id=chat.id, role="user", content="hi", attachments=[]))
    usage = Usage(
        user_id=user.id, user_label="alice", chat_id=chat.id, model="m",
        prompt_tokens=1, completion_tokens=2, latency_ms=3, status="ok",
    )
    db.add(usage)
    await db.commit()
    chat_id, usage_id = chat.id, usage.id

    await db.delete(user)
    await db.commit()
    db.expunge_all()

    assert (await db.execute(select(Chat).where(Chat.id == chat_id))).first() is None
    assert (await db.execute(select(Message).where(Message.chat_id == chat_id))).first() is None
    row = (await db.execute(select(Usage).where(Usage.id == usage_id))).scalar_one()
    assert row.user_id is None
    assert row.chat_id is None


async def test_user_defaults(db):
    user = make_user()
    db.add(user)
    await db.commit()
    await db.refresh(user)
    assert user.answer_language == "ru"
    assert user.theme == "system"
    assert user.system_prompt == ""
    assert user.default_model == "openrouter/auto"
    assert user.is_active is True
    assert user.totp_enabled is False
    assert user.failed_logins == 0
    assert user.created_at is not None


@pytest.mark.parametrize(
    "kw",
    [
        {"role": "root"},
        {"answer_language": "de"},
        {"theme": "blue"},
        {"username": "Bad Name"},
        {"username": "ab"},
        {"system_prompt": "x" * 2001},
    ],
)
async def test_user_check_constraints(db, kw):
    db.add(make_user(**kw))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_message_and_usage_check_constraints(db):
    user = make_user()
    db.add(user)
    await db.flush()
    chat = Chat(user_id=user.id)
    db.add(chat)
    await db.flush()
    async with db.begin_nested():
        db.add(Message(chat_id=chat.id, role="system", content="x", attachments=[]))
        with pytest.raises(IntegrityError):
            await db.flush()
    db.expunge_all()
    async with db.begin_nested():
        db.add(Usage(user_label="a", model="m", prompt_tokens=0, completion_tokens=0,
                     latency_ms=0, status="bogus"))
        with pytest.raises(IntegrityError):
            await db.flush()
