#!/bin/bash
# Three processes, one container (SPEC §11): the API, the worker, and the tunnel.
# If any of them dies the container dies, so ECS replaces the task rather than
# leaving a half-running service that still answers its health check.
set -euo pipefail

log() {
  printf '{"event":"%s","level":"info","logger":"entrypoint","detail":"%s"}\n' "$1" "${2:-}"
}

# One task, so no two migrators can race here.
log "migrate.start"
alembic upgrade head
log "migrate.done"

pids=()

shutdown() {
  trap - TERM INT
  log "shutdown.start"
  kill "${pids[@]}" 2>/dev/null || true
  wait || true
  exit 0
}
trap shutdown TERM INT

uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
pids+=("$!")
log "api.started"

python -m app.worker &
pids+=("$!")
log "worker.started"

# cloudflared reads TUNNEL_TOKEN from the environment; passing it as an argument
# would expose it in the process list.
if [[ -n "${TUNNEL_TOKEN:-}" ]]; then
  cloudflared --no-autoupdate tunnel run &
  pids+=("$!")
  log "tunnel.started"
elif [[ "${APP_ENV:-local}" == "production" ]]; then
  # Without the tunnel nothing can reach this task, and there is no second way in.
  echo '{"event":"tunnel.token_missing","level":"error","logger":"entrypoint"}' >&2
  exit 1
else
  log "tunnel.skipped" "no TUNNEL_TOKEN outside production"
fi

# First process to exit takes the container down with it.
wait -n
status=$?
log "process.exited" "status=${status}"
kill "${pids[@]}" 2>/dev/null || true
exit "${status}"
