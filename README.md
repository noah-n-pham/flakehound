# flakehound

Finds flaky CI jobs from GitHub Actions history you already have. One-click
install as a GitHub App — no workflow file changes, no test-suite instrumentation.

Stage 1 detects flaky **jobs**, not flaky **tests**. That is the honest tradeoff
of reading the Actions API instead of asking you to upload JUnit XML.

## Layout

```
backend/    FastAPI service, worker, and Alembic migrations
docker-compose.yml   Postgres for local development and tests
```

## Local development

Requires Docker, Python 3.12.

```bash
cp .env.example .env
docker compose up -d postgres

cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Tests run against the Postgres in `docker-compose.yml` — never sqlite, never a
mocked database. The suite creates and migrates a `flakehound_test` database of
its own, so the development database is left alone.

Migrations:

```bash
cd backend && alembic upgrade head
```

## The container

One image runs all three processes — the API, the worker, and `cloudflared` —
under a shell entrypoint that migrates first and exits if any of them dies.

```bash
docker compose up -d --build
curl localhost:8000/healthz
```

The tunnel starts only when `TUNNEL_TOKEN` is set. Outside production it is
skipped, since you reach the API on localhost; with `APP_ENV=production` and no
token the container exits rather than come up with no way in.
