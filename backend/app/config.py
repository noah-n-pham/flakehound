from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import model_validator
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

    # RDS generated the master password and holds it in a secret it owns (D-018),
    # so production injects the credential's parts rather than a URL — nobody
    # keeps a second copy of the password to assemble one. When db_host is set
    # these parts replace database_url.
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "ci_insights"
    db_user: str | None = None
    db_password: str | None = None

    # Shared with GitHub; every webhook body is HMAC-verified against it. No
    # default, so a deploy without it fails at startup rather than at the first
    # forged request.
    github_webhook_secret: str

    # Gates every read endpoint. Must be byte-identical here, in Secrets Manager,
    # and in Vercel's environment (H-004), or every read returns 401.
    internal_api_token: str

    # Statements slower than this are logged by the worker's slow-query hook (SPEC §9).
    slow_query_ms: int = 500

    worker_batch_size: int = 10
    worker_poll_seconds: float = 1.0

    @model_validator(mode="after")
    def _assemble_database_url(self) -> "Settings":
        if self.db_host is None:
            return self
        if not self.db_user or not self.db_password:
            missing = [
                name
                for name, value in (("DB_USER", self.db_user), ("DB_PASSWORD", self.db_password))
                if not value
            ]
            raise ValueError(f"DB_HOST is set but {' and '.join(missing)} is not")
        # An RDS-generated password may contain characters that mean something in
        # a URL, so both halves of the userinfo are escaped.
        user = quote(self.db_user, safe="")
        password = quote(self.db_password, safe="")
        self.database_url = (
            f"postgresql+psycopg://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"
        )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
