from functools import lru_cache
from pathlib import Path
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

    database_url: str = "postgresql+psycopg://ci:ci@localhost:5433/flakehound"

    # RDS generated the master password and holds it in a secret it owns (D-018),
    # so production injects the credential's parts rather than a URL — nobody
    # keeps a second copy of the password to assemble one. When db_host is set
    # these parts replace database_url.
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "flakehound"
    db_user: str | None = None
    db_password: str | None = None

    # Shared with GitHub; every webhook body is HMAC-verified against it. No
    # default, so a deploy without it fails at startup rather than at the first
    # forged request.
    github_webhook_secret: str

    # Gates every read endpoint. Must be byte-identical here, in Secrets Manager,
    # and in Vercel's environment (H-004), or every read returns 401.
    internal_api_token: str

    # The App's own identity, for signing the JWT that buys installation tokens.
    # Optional because the webhook path does not need them: an ingest-only deploy
    # must still start. Whoever calls GitHub raises if they are missing.
    github_app_id: int | None = None
    github_app_private_key: str | None = None
    # Local development points at the .pem on disk (H-012) rather than pasting a
    # private key into `.env`. Production injects the key itself from Secrets
    # Manager, where there is no file to point at.
    github_app_private_key_path: str | None = None
    github_api_base_url: str = "https://api.github.com"

    # Statements slower than this are logged by the worker's slow-query hook (SPEC §9).
    slow_query_ms: int = 500

    # The two config flags SPEC §2's edge-case table asks for, at its defaults. A
    # timed-out job counts as an eligible failure; a job that completed none of its
    # steps is a dead runner rather than a flaky test and is not an opportunity.
    timed_out_is_failure: bool = True
    exclude_infra_failures: bool = True

    worker_batch_size: int = 10
    worker_poll_seconds: float = 1.0

    # Retry (SPEC §5). The delay before attempt n is base * 2^(n-1), capped, so
    # the five attempts of the default ceiling span minutes rather than one
    # second — long enough to outlive a Postgres failover or a GitHub blip.
    retry_backoff_seconds: float = 5.0
    retry_backoff_max_seconds: float = 300.0
    # How often the worker reaps stuck rows and fails spent ones.
    queue_sweep_seconds: float = 60.0
    # A row claimed for longer than this is assumed to belong to a dead worker.
    # **It must exceed maximum processing time**, or the reaper hands a second
    # worker a row the first is still holding. A live handler is a handful of
    # upserts and one detection query — single-digit milliseconds — so five
    # minutes is three orders of magnitude of headroom. Section D's backfill
    # makes one row an HTTP crawl; revisit the number then, with a measurement.
    reaper_timeout_seconds: float = 300.0

    @model_validator(mode="after")
    def _load_private_key(self) -> "Settings":
        """Resolve the key to PEM text, from whichever source this environment has."""
        if self.github_app_private_key is None and self.github_app_private_key_path:
            self.github_app_private_key = Path(self.github_app_private_key_path).read_text()
        if self.github_app_private_key:
            # A PEM that travelled through an env var often arrives with its
            # newlines escaped, and cryptography rejects that with an error that
            # says nothing about newlines.
            self.github_app_private_key = self.github_app_private_key.replace("\\n", "\n").strip()
        return self

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
