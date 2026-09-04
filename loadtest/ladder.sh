#!/usr/bin/env bash
# Walk the offered webhook rate up until something gives, one k6 process per
# plateau, and report what the queue did about it.
#
#   ./loadtest/ladder.sh                      # 50..1600/s, k6 inside the compose network
#   RATES="200 400" DURATION=60s ./loadtest/ladder.sh
#   MODE=host ./loadtest/ladder.sh            # via the published port, to price the forwarder
#
# MODE=network is the number to quote: k6 talks to the container over the compose
# bridge, so no host port forwarding sits in the measurement. MODE=host crosses
# Docker Desktop's forwarder and is only useful next to a network run.
set -euo pipefail

cd "$(dirname "$0")/.."

RATES=${RATES:-"50 100 200 400 800 1600"}
DURATION=${DURATION:-30s}
MODE=${MODE:-network}
IMAGE=${IMAGE:-grafana/k6:latest}

# Read from .env into the environment, never onto a command line: docker is given
# the variable's name and inherits its value.
WEBHOOK_SECRET=$(sed -n 's/^GITHUB_WEBHOOK_SECRET=//p' .env)
export WEBHOOK_SECRET
if [[ -z "${WEBHOOK_SECRET}" ]]; then
  echo "GITHUB_WEBHOOK_SECRET is not set in .env; the handler verifies every signature" >&2
  exit 1
fi

psql_q() {
  docker compose exec -T postgres psql -qtAX -U ci -d flakehound -c "$1" | tr -d ' '
}

pending() { psql_q "select count(*) from event_queue where status = 'pending'"; }

counts() {
  psql_q "select 'deliveries=' || (select count(*) from webhook_deliveries)
            || ' queue=' || (select count(*) from event_queue)
            || ' pending=' || (select count(*) from event_queue where status='pending')
            || ' failed=' || (select count(*) from event_queue where status='failed')
            || ' jobs=' || (select count(*) from jobs)"
}

run_k6() {
  local rate=$1
  if [[ "${MODE}" == "host" ]]; then
    BASE_URL=${BASE_URL:-http://localhost:8000} RATE="${rate}" DURATION="${DURATION}" \
      k6 run --quiet loadtest/webhook_throughput.js
  else
    local network
    network=$(docker network ls --format '{{.Name}}' | grep -m1 flakehound)
    docker run --rm --network "${network}" \
      -v "${PWD}/loadtest:/loadtest:ro" \
      -e WEBHOOK_SECRET -e BASE_URL="${BASE_URL:-http://app:8000}" \
      -e RATE="${rate}" -e DURATION="${DURATION}" \
      "${IMAGE}" run --quiet /loadtest/webhook_throughput.js
  fi
}

echo "mode=${MODE} duration=${DURATION} rates=${RATES}"
echo "before: $(counts)"

# A cold uvicorn's first few hundred requests carry connection setup and first-use
# costs that belong to no plateau. Warm it, discard the numbers, then measure.
if [[ "${WARMUP:-1}" == "1" ]]; then
  measured_duration=${DURATION}
  DURATION=5s
  run_k6 50 >/dev/null 2>&1 || true
  DURATION=${measured_duration}
  echo "warmed up at 50/s for 5s (numbers discarded)"
fi

for rate in ${RATES}; do
  # k6 exits 99 when a threshold fails, which is a result and not an error here.
  run_k6 "${rate}" || echo "  (k6 exit $?: a threshold failed, see thresholds_failed)"
  echo "  backlog after ${rate}/s: pending=$(pending)"
done

echo "draining the queue, sampling every second"
start=$(date +%s)
depth=$(pending)
peak=${depth}
while [[ "${depth}" != "0" ]]; do
  sleep 1
  depth=$(pending)
  now=$(date +%s)
  if (( now - start > 600 )); then
    echo "  gave up after 10 minutes with pending=${depth}"
    break
  fi
done
elapsed=$(( $(date +%s) - start ))
echo "drained ${peak} rows in ${elapsed}s"
if (( elapsed > 0 )); then
  echo "worker drain rate: $(( peak / elapsed ))/s"
fi
echo "after: $(counts)"
