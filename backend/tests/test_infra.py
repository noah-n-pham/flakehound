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
    assert url.database == "ci_insights"


def test_settings_read_the_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("APP_ENV", "production")
    settings = Settings()
    assert settings.log_level == "debug"
    assert settings.app_env == "production"
