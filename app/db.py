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
    # hide_parameters: тексты сообщений не попадают в сообщения об ошибках и логи.
    return create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Сессия на запрос; engine создаётся в create_app и хранится в app.state."""
    async with request.app.state.sessionmaker() as session:
        yield session


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Фабрика коротких сессий для работы вне жизненного цикла запроса (после стрима, фон)."""
    return request.app.state.sessionmaker
