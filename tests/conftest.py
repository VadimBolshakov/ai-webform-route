import asyncio
import os
import sys
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.models  # noqa: F401  (registers the models in Base.metadata)
from app.config import Settings
from app.db import Base, get_db, get_session_factory
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
    db_name = make_url(settings.database_url).database or ""
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"Refusing: the tests run drop_all, and the database {db_name!r} does not end with '_test'"
        )
    eng = create_async_engine(settings.database_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    """Session inside an outer transaction; commit() in a test is a savepoint, rolled back at the end."""
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

    @asynccontextmanager
    async def _shared_session():
        # The post-stream write goes through the session factory; in tests it is the same session,
        # so that rolling back the outer transaction isolates it too.
        yield db

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_session_factory] = lambda: _shared_session
    return application


@pytest_asyncio.fixture
async def client(app, settings):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=settings.base_url,
        headers={"Origin": settings.base_url},
    ) as c:
        yield c
