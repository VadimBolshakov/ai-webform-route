import argparse
import asyncio
import getpass
import sys
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import audit
from app.auth.sessions import revoke_all
from app.auth.usernames import validate_username
from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.models import User
from app.security.passwords import hash_password, validate_password
from app.telegram.client import TelegramClient, TelegramError


async def create_admin(db: AsyncSession, username: str, password: str) -> User:
    username = username.strip().lower()
    errors = validate_username(username) or validate_password(password, username)
    if errors:
        raise ValueError("; ".join(errors))
    existing = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if existing:
        raise ValueError(f"User {username} already exists.")
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
        raise ValueError(f"User {username} not found.")
    return user


async def reset_2fa(db: AsyncSession, username: str) -> User:
    """Access recovery: 2FA disabled, all sessions revoked (the admin will set it up again)."""
    user = await _user_for_update(db, username)
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_last_step = None
    user.failed_logins = 0
    user.locked_until = None
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
    user.failed_logins = 0
    user.locked_until = None
    await revoke_all(db, user.id)
    await audit(db, "password_reset", user, "cli")
    await db.commit()
    return user


async def telegram_whoami(settings: Settings) -> int:
    """Print chat ids of recent messages to the bot (to find TELEGRAM_OWNER_CHAT_ID)."""
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        return 1
    async with httpx.AsyncClient() as http:
        client = TelegramClient(settings.telegram_bot_token, http)
        try:
            updates = await client.get_updates(None, 0)
        except TelegramError as e:
            print(f"getUpdates failed: {e}", file=sys.stderr)
            return 1
    shown = 0
    for upd in updates:
        msg = upd.get("message")
        if not msg:
            continue
        sender = msg.get("from") or {}
        who = sender.get("username") or sender.get("first_name") or "?"
        print(f"chat_id={msg['chat']['id']} from={who} text={(msg.get('text') or '')[:30]}")
        shown += 1
    if not shown:
        print("No messages. Send any message to the bot first.")
    return 0


async def _run(fn, *args) -> None:
    engine = make_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as db:
            await fn(db, *args)
    finally:
        await engine.dispose()


def _ask_password() -> str | None:
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return None
    return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("create-admin", "reset-2fa", "reset-password"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("telegram-whoami")
    args = parser.parse_args(argv)
    if args.cmd == "telegram-whoami":
        return asyncio.run(telegram_whoami(get_settings()))
    try:
        if args.cmd == "reset-2fa":
            asyncio.run(_run(reset_2fa, args.username))
            print(f"2FA for {args.username} reset, sessions revoked.")
            return 0
        pw = _ask_password()
        if pw is None:
            return 1
        if args.cmd == "reset-password":
            asyncio.run(_run(reset_password, args.username, pw))
            print(f"Password for {args.username} changed, sessions revoked.")
            return 0
        asyncio.run(_run(create_admin, args.username, pw))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Administrator {args.username} created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
