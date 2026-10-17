import asyncio
import os
import sys

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.models  # noqa: F401  (регистрирует модели в Base.metadata)
from app.config import Settings
from app.db import Base, get_db
from app.main import create_app

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://aichat:aichat@127.0.0.1:5432/aichat_test"
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        database_url=TEST_DB_URL,
        fernet_key=Fernet.generate_key().decode(),
        openrouter_api_key="test-openrouter-key",
        login_path_token="test-login-token",
        base_url="https://localhost:8443",
        title_model="test/title-model",
        enable_jobs=False,
        _env_file=None,
    )


@pytest_asyncio.fixture(scope="session")
async def engine(settings):
    eng = create_async_engine(settings.database_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    """Сессия внутри внешней транзакции; commit() в тесте — savepoint, в конце откат."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture
def app(settings, db):
    application = create_app(settings)

    async def _get_db():
        yield db

    application.dependency_overrides[get_db] = _get_db
    return application


@pytest_asyncio.fixture
async def client(app, settings):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=settings.base_url,
        headers={"Origin": settings.base_url},
    ) as c:
        yield c
