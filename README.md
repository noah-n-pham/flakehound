# flakehound

Finds flaky CI jobs from GitHub Actions history you already have. One-click
install as a GitHub App — no workflow file changes, no test-suite instrumentation.

Stage 1 detects flaky **jobs**, not flaky **tests**. That is the honest tradeoff
of reading the Actions API instead of asking you to upload JUnit XML.

## Layout

```
backend/    FastAPI service, worker, and Alembic migrations
frontend/   Next.js dashboard, deployed on Vercel
docker-compose.yml   Postgres for local development and tests
```

The browser never talks to FastAPI. Next.js is the backend-for-frontend: it holds
the internal bearer token server-side and is the only client of `/api/*`.

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

## The dashboard

Requires Node 20+.

```bash
cd frontend
npm install
npm run dev:local     # reads ../.env, so the internal token lives in one file
```

`dev:local` points the BFF at whatever `API_BASE_URL` says — the deployed API by
default, so the page shows production data without running anything locally.
`/styleguide` renders every primitive in `docs/DESIGN.md` that the app uses; add a
component there before using it anywhere else.
