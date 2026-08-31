from argparse import Namespace
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

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
    engine = create_async_engine(database_url, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        yield session
    await engine.dispose()
