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

    # The commit this image was built from, stamped in by the Dockerfile's build
    # arg. Deployment is pull-based: CI pushes a tag and walks away, so the only
    # way it can know its image is the one now serving is to ask the running
    # process what it is. `unknown` is what a local or hand-built image reports.
    git_sha: str = "unknown"

    database_url: str = "postgresql+psycopg://ci:ci@localhost:5433/flakehound"

    # RDS generated the master password and holds it in a secret it owns,
    # so production injects the credential's parts rather than a URL. Nobody
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
    # and in the frontend's environment, or every read returns 401.
    internal_api_token: str

    # The App's own identity, for signing the JWT that buys installation tokens.
    # Optional because the webhook path does not need them: an ingest-only deploy
    # must still start. Whoever calls GitHub raises if they are missing.
    github_app_id: int | None = None
    github_app_private_key: str | None = None
    # Local development points at the .pem on disk rather than pasting a private
    # key into `.env`. Production injects the key itself from Secrets
    # Manager, where there is no file to point at.
    github_app_private_key_path: str | None = None
    github_api_base_url: str = "https://api.github.com"
    # How long a rate-limited request may sleep before the wait is handed back
    # to the queue instead. It must stay well under `reaper_timeout_seconds`, or
    # a worker sleeping out a limit looks dead and its row is given away.
    github_rate_limit_max_wait_seconds: float = 60.0

    # Whose installation token reads *public* repositories for the observational
    # board. Verified rather than assumed: an installation token reads any public
    # repo's Actions history at the full 5,000/hour, so the board needs no new
    # credential (D-046). Optional because ingest does not need it; the crawl raises
    # by name if it is missing, rather than falling back to anonymous requests at
    # 60/hour and looking merely slow.
    observation_installation_id: int | None = None
    # How far back the observational crawl walks. Shorter than `backfill_days` on
    # purpose: an installed repo's owner asked us to look, so it gets the full 90 days,
    # while history older than the public board's own window would be spending a shared
    # rate limit on rows the page can never show.
    observation_backfill_days: int = 30

    # Statements slower than this are logged by the worker's slow-query hook.
    slow_query_ms: int = 500

    # Metrics. One sample a minute. The window is what
    # the ingest-lag percentiles are measured over: trailing rather than lifetime,
    # or no amount of current slowness could move the number. Samples are pruned
    # after a month: at ~20 series a minute they would otherwise out-grow the facts.
    metrics_interval_seconds: float = 60.0
    metrics_window_seconds: float = 3600.0
    metrics_retention_days: int = 30
    # How many request latencies the API process keeps per endpoint per minute. The
    # reservoir is what bounds memory under load; the percentile stays unbiased
    # however far the traffic overshoots it.
    metrics_sample_limit: int = 512

    # The two detection flags, at their defaults. A
    # timed-out job counts as an eligible failure; a job that completed none of its
    # steps is a dead runner rather than a flaky test and is not an opportunity.
    timed_out_is_failure: bool = True
    exclude_infra_failures: bool = True

    worker_batch_size: int = 10
    worker_poll_seconds: float = 1.0

    # Backfill. The runs listing caps near 1000 results however you
    # page it, so history is walked in `created` windows rather than by page
    # alone, and a window that overflows the cap is halved until it fits.
    backfill_days: int = 90
    backfill_window_days: int = 7
    backfill_page_size: int = 100
    backfill_result_cap: int = 1000

    # The rollup's trailing window, recomputed whole (see app/rollup.py). It matches
    # `backfill_days` on purpose: history we asked GitHub for is history the
    # leaderboard should be able to read back.
    rollup_days: int = 90

    # Retry. The delay before attempt n is base * 2^(n-1), capped, so
    # the five attempts of the default ceiling span minutes rather than one
    # second, long enough to outlive a Postgres failover or a GitHub blip.
    retry_backoff_seconds: float = 5.0
    retry_backoff_max_seconds: float = 300.0
    # How often the worker reaps stuck rows and fails spent ones.
    queue_sweep_seconds: float = 60.0
    # A row claimed for longer than this is assumed to belong to a dead worker.
    # **It must exceed maximum processing time**, or the reaper hands a second
    # worker a row the first is still holding. A live handler is a handful of
    # upserts and one detection query (single-digit milliseconds), so five
    # minutes is three orders of magnitude of headroom. Backfill makes one row an
    # HTTP crawl, so the number is worth revisiting there, with a measurement.
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
