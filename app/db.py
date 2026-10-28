from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> AsyncEngine:
    # hide_parameters: message texts do not end up in error messages and logs.
    return create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Per-request session; the engine is created in create_app and stored in app.state."""
    async with request.app.state.sessionmaker() as session:
        yield session


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Factory of short sessions for work outside the request lifecycle (after a stream, background)."""
    return request.app.state.sessionmaker
