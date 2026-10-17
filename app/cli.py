import argparse
import asyncio
import getpass
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import audit
from app.auth.sessions import revoke_all
from app.auth.usernames import validate_username
from app.config import get_settings
from app.db import make_engine, make_sessionmaker
from app.models import User
from app.security.passwords import hash_password, validate_password


async def create_admin(db: AsyncSession, username: str, password: str) -> User:
    username = username.strip().lower()
    errors = validate_username(username) or validate_password(password, username)
    if errors:
        raise ValueError("; ".join(errors))
    existing = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if existing:
        raise ValueError(f"Пользователь {username} уже существует.")
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        monthly_limit_usd=Decimal("50"),
    )
    db.add(user)
    await db.flush()
    await audit(db, "admin_created", user, "cli")
    await db.commit()
    return user


async def _user_for_update(db: AsyncSession, username: str) -> User:
    username = username.strip().lower()
    user = (
        await db.execute(select(User).where(User.username == username).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        raise ValueError(f"Пользователь {username} не найден.")
    return user


async def reset_2fa(db: AsyncSession, username: str) -> User:
    """Восстановление доступа: 2FA выключена, все сессии отозваны (админ настроит заново)."""
    user = await _user_for_update(db, username)
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_last_step = None
    await revoke_all(db, user.id)
    await audit(db, "totp_reset", user, "cli")
    await db.commit()
    return user


async def reset_password(db: AsyncSession, username: str, password: str) -> User:
    user = await _user_for_update(db, username)
    errors = validate_password(password, user.username)
    if errors:
        await db.rollback()
        raise ValueError("; ".join(errors))
    user.password_hash = await asyncio.to_thread(hash_password, password)
    await revoke_all(db, user.id)
    await audit(db, "password_reset", user, "cli")
    await db.commit()
    return user


async def _run(fn, *args) -> None:
    engine = make_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as db:
            await fn(db, *args)
    finally:
        await engine.dispose()


def _ask_password() -> str | None:
    pw = getpass.getpass("Пароль: ")
    if pw != getpass.getpass("Повторите пароль: "):
        print("Пароли не совпадают.", file=sys.stderr)
        return None
    return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("create-admin", "reset-2fa", "reset-password"):
        sub.add_parser(name).add_argument("username")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "reset-2fa":
            asyncio.run(_run(reset_2fa, args.username))
            print(f"2FA для {args.username} сброшена, сессии отозваны.")
            return 0
        pw = _ask_password()
        if pw is None:
            return 1
        if args.cmd == "reset-password":
            asyncio.run(_run(reset_password, args.username, pw))
            print(f"Пароль {args.username} изменён, сессии отозваны.")
            return 0
        asyncio.run(_run(create_admin, args.username, pw))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Администратор {args.username} создан.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
