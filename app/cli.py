import argparse
import asyncio
import getpass
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import audit
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


async def _run(username: str, password: str) -> None:
    engine = make_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as db:
            await create_admin(db, username, password)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("create-admin")
    ca.add_argument("username")
    args = parser.parse_args(argv)
    pw = getpass.getpass("Пароль: ")
    if pw != getpass.getpass("Повторите пароль: "):
        print("Пароли не совпадают.", file=sys.stderr)
        return 1
    try:
        asyncio.run(_run(args.username, pw))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Администратор {args.username} создан.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
