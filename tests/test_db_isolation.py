"""Regression: commit() in one test must not leak into others."""
from sqlalchemy import select

from app.models import User


async def test_a_commits_a_row(db):
    db.add(User(username="leaky", password_hash="x", role="user", monthly_limit_usd=5))
    await db.commit()
    assert (await db.execute(select(User).where(User.username == "leaky"))).scalar_one()


async def test_b_row_from_previous_test_is_gone(db):
    assert (await db.execute(select(User).where(User.username == "leaky"))).first() is None
