#!/usr/bin/env bash
# Walk the offered dashboard page-load rate up, one k6 process per plateau.
#
#   ./loadtest/read_ladder.sh                     # 2..80 pages/s, k6 inside the compose network
#   RATES="10 20" DURATION=60s ./loadtest/read_ladder.sh
#   MODE=host ./loadtest/read_ladder.sh           # via the published port, to price the forwarder
#
# A "page" is the six calls `frontend/src/app/page.tsx` makes plus the public board,
# so the request rate is seven times the rate named here. See read_load.js.
#
# MODE=network is the number to quote: k6 talks to the container over the compose
# bridge, so no host port forwarding sits in the measurement.
set -euo pipefail

cd "$(dirname "$0")/.."

RATES=${RATES:-"2 5 10 20 40 80"}
DURATION=${DURATION:-30s}
MODE=${MODE:-network}
IMAGE=${IMAGE:-grafana/k6:latest}
REPO_ID=${REPO_ID:-999000002}

# Read from .env into the environment, never onto a command line: docker is given
# the variable's name and inherits its value.
INTERNAL_API_TOKEN=$(sed -n 's/^INTERNAL_API_TOKEN=//p' .env)
export INTERNAL_API_TOKEN
if [[ -z "${INTERNAL_API_TOKEN}" ]]; then
  echo "INTERNAL_API_TOKEN is not set in .env; every /api read answers 401 without it" >&2
  exit 1
fi

psql_q() {
  docker compose exec -T postgres psql -qtAX -U ci -d flakehound -c "$1" | tr -d ' '
}

# What the reads are scanning. A p99 without this is a number about nothing: the
# timeline and the job list walk raw job rows, so their cost is a function of it.
echo "target: repo ${REPO_ID}, $(psql_q "select count(*) from jobs where repo_id=${REPO_ID}") job rows,\
 $(psql_q "select count(*) from job_stats_daily where repo_id=${REPO_ID}") rollup rows"
echo "queue: pending=$(psql_q "select count(*) from event_queue where status='pending'")"
echo "mode=${MODE} duration=${DURATION} rates=${RATES} (pages/s; requests/s is 7x)"

run_k6() {
  local rate=$1
  if [[ "${MODE}" == "host" ]]; then
    BASE_URL=${BASE_URL:-http://localhost:8000} RATE="${rate}" DURATION="${DURATION}" \
      REPO_ID="${REPO_ID}" k6 run --quiet loadtest/read_load.js
  else
    local network
    network=$(docker network ls --format '{{.Name}}' | grep -m1 flakehound)
    docker run --rm --network "${network}" \
      -v "${PWD}/loadtest:/loadtest:ro" \
      -e INTERNAL_API_TOKEN -e BASE_URL="${BASE_URL:-http://app:8000}" \
      -e RATE="${rate}" -e DURATION="${DURATION}" -e REPO_ID="${REPO_ID}" \
      "${IMAGE}" run --quiet /loadtest/read_load.js
  fi
}

# A cold uvicorn's first requests carry connection setup and Postgres' first read of
# these pages from disk. Warm it, discard the numbers, then measure.
if [[ "${WARMUP:-1}" == "1" ]]; then
  measured_duration=${DURATION}
  DURATION=5s
  run_k6 2 >/dev/null 2>&1 || true
  DURATION=${measured_duration}
  echo "warmed up at 2 pages/s for 5s (numbers discarded)"
fi

for rate in ${RATES}; do
  # k6 exits 99 when a threshold fails, which is a result and not an error here.
  run_k6 "${rate}" || echo "  (k6 exit $? — a threshold failed, see thresholds_failed)"
done

# Reads write nothing, so unlike the webhook ladder there is no backlog to drain.
# The queue is printed again only to prove no ingest ran alongside these numbers.
echo "queue after: pending=$(psql_q "select count(*) from event_queue where status='pending'")"
