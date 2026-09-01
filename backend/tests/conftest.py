from argparse import Namespace
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import session_scope
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _test_database_url() -> str:
    """The configured database with ``_test`` appended, never the real one."""
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def _admin_dsn(url_str: str) -> str:
    """libpq DSN for the maintenance database, used to create/drop the test DB."""
    url = make_url(url_str).set(drivername="postgresql", database="postgres")
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def database_url() -> str:
    """A freshly created test database, migrated to head. Real Postgres, never sqlite."""
    url = _test_database_url()
    name = make_url(url).database

    with psycopg.connect(_admin_dsn(url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.cmd_opts = Namespace(x=[f"database_url={url}"])
    command.upgrade(cfg, "head")

    return url


@pytest.fixture
async def db_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """A session whose writes are discarded when the test ends.

    The session joins an outer transaction as a savepoint, so a test may commit
    and still leave no trace — including after an IntegrityError.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        maker = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The real ASGI app, talking to the test session so writes roll back."""
    app = create_app()

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[session_scope] = _session_override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://flakehound.test") as http_client:
        yield http_client
