from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Secrets are added here by the slice that first needs them, so that a missing
    credential fails loudly at startup instead of being defaulted around.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "info"

    database_url: str = "postgresql+psycopg://ci:ci@localhost:5433/ci_insights"

    # Statements slower than this are logged by the worker's slow-query hook (SPEC §9).
    slow_query_ms: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
