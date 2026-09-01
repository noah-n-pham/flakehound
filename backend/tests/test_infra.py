import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import Settings, get_settings


async def test_connects_to_real_postgres(db_session):
    version = (await db_session.execute(text("SELECT version()"))).scalar_one()
    assert version.startswith("PostgreSQL")

    current_db = (await db_session.execute(text("SELECT current_database()"))).scalar_one()
    assert current_db.endswith("_test")


async def test_migrations_ran(db_session):
    exists = (
        await db_session.execute(text("SELECT to_regclass('public.alembic_version')"))
    ).scalar_one()
    assert exists == "alembic_version"


def test_settings_default_to_local_compose_postgres():
    settings = get_settings()
    url = make_url(settings.database_url)
    assert url.drivername == "postgresql+psycopg"
    assert url.database == "flakehound"


def test_settings_assemble_the_url_from_the_rds_secret_parts(monkeypatch):
    monkeypatch.setenv("DB_HOST", "flakehound-db.c47a6ai2o1w8.us-east-1.rds.amazonaws.com")
    monkeypatch.setenv("DB_USER", "ciinsights")
    # RDS generates the password, so it may contain characters a URL reads as syntax.
    monkeypatch.setenv("DB_PASSWORD", "p@ss/word:1#2")
    monkeypatch.setenv("DB_NAME", "flakehound")

    url = make_url(Settings().database_url)
    assert url.host == "flakehound-db.c47a6ai2o1w8.us-east-1.rds.amazonaws.com"
    assert url.port == 5432
    assert url.username == "ciinsights"
    assert url.password == "p@ss/word:1#2"
    assert url.database == "flakehound"


def test_settings_reject_a_db_host_without_credentials(monkeypatch):
    monkeypatch.setenv("DB_HOST", "somewhere.rds.amazonaws.com")
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    with pytest.raises(ValidationError, match="DB_USER and DB_PASSWORD"):
        Settings()


def test_settings_read_the_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("APP_ENV", "production")
    settings = Settings()
    assert settings.log_level == "debug"
    assert settings.app_env == "production"
