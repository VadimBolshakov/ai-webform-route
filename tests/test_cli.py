"""Admin recovery commands (python -m app.cli reset-2fa / reset-password)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import cli
from app.auth.sessions import create_session
from app.models import AuditLog, AuthSession, User
from app.security.passwords import verify_password
from tests.test_auth import PASSWORD, make_user

NEW_PW = "Brand-New-Secret-91"


async def _sessions(db, user_id) -> int:
    return await db.scalar(
        select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user_id)
    )


async def test_reset_2fa(db):
    admin = await make_user(
        db, "root", "admin", totp_enabled=True, totp_secret_enc="enc", totp_last_step=123
    )
    admin.failed_logins = 7
    admin.locked_until = datetime.now(UTC) + timedelta(minutes=30)
    other = await make_user(db, "bob")
    await create_session(db, admin, "1.1.1.1", "ua")
    await create_session(db, admin, "1.1.1.2", "ua", totp_pending=True)
    await create_session(db, other, "2.2.2.2", "ua")
    await db.commit()

    await cli.reset_2fa(db, " Root ")
    await db.refresh(admin)
    assert (admin.totp_enabled, admin.totp_secret_enc, admin.totp_last_step) == (False, None, None)
    assert (admin.failed_logins, admin.locked_until) == (0, None)
    assert await _sessions(db, admin.id) == 0
    assert await _sessions(db, other.id) == 1
    row = (await db.execute(select(AuditLog).where(AuditLog.event == "totp_reset"))).scalar_one()
    assert row.user_id == admin.id and row.ip == "cli"

    with pytest.raises(ValueError):
        await cli.reset_2fa(db, "nobody")


async def test_reset_password(db):
    admin = await make_user(db, "root", "admin")
    admin.failed_logins = 6
    admin.locked_until = datetime.now(UTC) + timedelta(minutes=30)
    await create_session(db, admin, "1.1.1.1", "ua")
    await db.commit()

    with pytest.raises(ValueError):
        await cli.reset_password(db, "root", "short")
    with pytest.raises(ValueError):
        await cli.reset_password(db, "nobody", NEW_PW)
    await db.refresh(admin)
    assert verify_password(admin.password_hash, PASSWORD)
    assert await _sessions(db, admin.id) == 1

    await cli.reset_password(db, "root", NEW_PW)
    await db.refresh(admin)
    assert verify_password(admin.password_hash, NEW_PW)
    assert (admin.failed_logins, admin.locked_until) == (0, None)
    assert await _sessions(db, admin.id) == 0
    row = (
        await db.execute(select(AuditLog).where(AuditLog.event == "password_reset"))
    ).scalar_one()
    assert row.user_id == admin.id and row.ip == "cli"
    assert (await db.execute(select(User.username))).scalars().all() == ["root"]


def test_main_reset_password_prompts_twice(monkeypatch):
    answers = iter([NEW_PW, "different-pass-12"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    called = []

    async def fake_run(fn, *args):
        called.append(args)

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["reset-password", "root"]) == 1  # пароли не совпали
    assert called == []
    answers = iter([NEW_PW, NEW_PW])
    assert cli.main(["reset-password", "root"]) == 0
    assert called == [("root", NEW_PW)]


def test_main_reset_2fa_no_prompt(monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": pytest.fail("no prompt"))
    called = []

    async def fake_run(fn, *args):
        called.append((fn, args))

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["reset-2fa", "root"]) == 0
    assert called == [(cli.reset_2fa, ("root",))]
