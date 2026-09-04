#!/bin/bash
# Bootstrap for the one instance that runs flakehound.
#
# Passed to `ec2 run-instances --user-data`, so this file is the whole
# configuration of the box: there is no console step and nothing typed by hand,
# and the four scripts and the unit file it installs are embedded here rather
# than fetched, because a fresh box has no way to read a private repository.
#
# The instance is cattle. There is no shell on it — the build's IAM user is
# denied every SSM action — so changing how it is set up means editing this file
# and launching a replacement. That is a constraint, and it is also the property
# that keeps the box's configuration honest.
#
# Everything it needs comes over outbound connections: the image from ECR, the
# credentials from Secrets Manager, both authorized by the instance profile.
# Nothing dials in. The security group has no inbound rule, and the container
# publishes its port on loopback only; ingress is `cloudflared` inside the
# container dialling out to Cloudflare.
set -euo pipefail

REGION=us-east-1
ECR=753397111940.dkr.ecr.us-east-1.amazonaws.com/flakehound
LOG_GROUP=/flakehound/app
INIT_LOG=/var/log/flakehound-init.log

exec > >(tee -a "$INIT_LOG") 2>&1
set -x

imds() {
  local token
  token=$(curl -sf -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -sf -H "X-aws-ec2-metadata-token: ${token}" \
    "http://169.254.169.254/latest/meta-data/$1"
}

INSTANCE_ID=$(imds instance-id)

# There is no SSM access and no inbound port, so a boot that fails is only
# debuggable if its log leaves the box. This trap is the whole story of a boot:
# it runs whether the script succeeded or died on `set -e`.
ship_init_log() {
  local status=$?
  set +x
  local stream="init/${INSTANCE_ID}"
  aws logs create-log-stream --region "$REGION" \
    --log-group-name "$LOG_GROUP" --log-stream-name "$stream" 2>/dev/null || true
  python3 - "$INIT_LOG" "$status" > /tmp/init-events.json <<'PY'
import json
import sys
import time

path, status = sys.argv[1], sys.argv[2]
now = int(time.time() * 1000)
lines = open(path, errors="replace").read().splitlines()
lines.append(f"user-data exited with status {status}")
# PutLogEvents wants ascending timestamps and rejects an empty message.
events = [{"timestamp": now + i, "message": line or " "} for i, line in enumerate(lines)]
json.dump(events[-9000:], sys.stdout)
PY
  aws logs put-log-events --region "$REGION" \
    --log-group-name "$LOG_GROUP" --log-stream-name "$stream" \
    --log-events file:///tmp/init-events.json >/dev/null || true
}
trap ship_init_log EXIT

dnf install -y docker
systemctl enable --now docker

# Created up front rather than as a side effect of whichever script runs first.
# Getting that order wrong once cost six minutes of production: the self-test was
# written into this directory before the script that used to create it had run,
# `set -e` killed the bootstrap, and the box came up with no service on it.
install -d -m 0700 /run/flakehound

# ---------------------------------------------------------------------------
# flakehound-secrets — the container's environment, assembled on the box.
#
# Written to tmpfs, so no credential is ever on the disk, in the image, or in a
# process argument list. It took over from the ECS task definition's `secrets`
# block, and keeps that block's names and sources. The unit runs it on
# every start, so a rotated RDS password is picked up by a restart and nothing
# holds a stale copy.
# ---------------------------------------------------------------------------
install -m 0755 /dev/stdin /usr/local/bin/flakehound-secrets <<'SCRIPT'
#!/bin/bash
set -euo pipefail
umask 077

REGION=us-east-1
RUN_DIR=/run/flakehound
RDS_SECRET='arn:aws:secretsmanager:us-east-1:753397111940:secret:rds!db-ec283f03-6478-49af-b495-7c5aecb47d4c-7uHXmU'

secret() {
  aws secretsmanager get-secret-value --region "$REGION" \
    --secret-id "$1" --query SecretString --output text
}

install -d -m 0700 "$RUN_DIR"

# RDS generated the master password and owns it; nobody holds a second copy, so
# the parts are read out of its JSON here exactly as ECS read them by json-key.
rds=$(secret "$RDS_SECRET")
db_user=$(printf '%s' "$rds" | python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])')
db_password=$(printf '%s' "$rds" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')

# The App key is a PEM. `docker --env-file` cannot carry a value with newlines,
# so the key goes to a file and the app is pointed at it — the same shape local
# development already uses. Owned by the container's uid, not by root.
secret 'arn:aws:secretsmanager:us-east-1:753397111940:secret:flakehound/github-app-private-key-SMRJmp' \
  > "${RUN_DIR}/github-app.pem"
chown 10001:10001 "${RUN_DIR}/github-app.pem"
chmod 0400 "${RUN_DIR}/github-app.pem"

cat > "${RUN_DIR}/env" <<EOF
APP_ENV=production
LOG_LEVEL=info
PORT=8000
DB_HOST=flakehound-db.c47a6ai2o1w8.us-east-1.rds.amazonaws.com
DB_PORT=5432
DB_NAME=flakehound
DB_USER=${db_user}
DB_PASSWORD=${db_password}
GITHUB_APP_ID=4792446
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
OBSERVATION_INSTALLATION_ID=158221992
GITHUB_WEBHOOK_SECRET=$(secret 'arn:aws:secretsmanager:us-east-1:753397111940:secret:flakehound/github-webhook-secret-Iv7fNm')
INTERNAL_API_TOKEN=$(secret 'arn:aws:secretsmanager:us-east-1:753397111940:secret:flakehound/internal-api-token-EmFPqI')
TUNNEL_TOKEN=$(secret 'arn:aws:secretsmanager:us-east-1:753397111940:secret:flakehound/tunnel-token-HiW65T')
EOF
chmod 0600 "${RUN_DIR}/env"
SCRIPT

# ---------------------------------------------------------------------------
# flakehound-image — resolve the tag to a digest, pull it, and record it.
#
# The container is always started **by digest, never by tag.** A tag can move
# under a running unit, and then a restart would silently land on different
# bytes than the ones that were started and verified.
# ---------------------------------------------------------------------------
install -m 0755 /dev/stdin /usr/local/bin/flakehound-image <<'SCRIPT'
#!/bin/bash
set -euo pipefail

REGION=us-east-1
ECR=753397111940.dkr.ecr.us-east-1.amazonaws.com/flakehound
TAG="${1:-latest}"
RUN_DIR=/run/flakehound

install -d -m 0700 "$RUN_DIR"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ECR%/*}"

digest=$(aws ecr describe-images --region "$REGION" --repository-name flakehound \
  --image-ids "imageTag=${TAG}" --query 'imageDetails[0].imageDigest' --output text)

docker pull "${ECR}@${digest}"

token=$(curl -sf -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
instance=$(curl -sf -H "X-aws-ec2-metadata-token: ${token}" \
  http://169.254.169.254/latest/meta-data/instance-id)

cat > "${RUN_DIR}/runtime" <<EOF
IMAGE=${ECR}@${digest}
LOG_STREAM=app/${instance}
EOF
SCRIPT

# ---------------------------------------------------------------------------
# flakehound-run — the container, in the foreground.
#
# A wrapper rather than a long ExecStart line, because systemd reads
# EnvironmentFile *before* ExecStartPre runs: the digest that ExecStartPre had
# just resolved would not be the one ExecStart used. Reading the file here means
# there is no window in which those two disagree.
#
# `exec` makes the docker client the unit's main process, so systemd's SIGTERM
# reaches the entrypoint's trap and the three processes shut down in order.
# ---------------------------------------------------------------------------
install -m 0755 /dev/stdin /usr/local/bin/flakehound-run <<'SCRIPT'
#!/bin/bash
set -euo pipefail

REGION=us-east-1
LOG_GROUP=/flakehound/app

# shellcheck disable=SC1091
. /run/flakehound/runtime

# The port is published on loopback only. Nothing on the network can reach it —
# the security group has no inbound rule either — and it exists so that a deploy
# can gate on the container's own health check and so the load tests SPEC §10
# asks for can hit the container directly instead of measuring Cloudflare.
#
# The memory cap is deliberate on a 1 GB box: an unbounded container that leaks
# would have the kernel choose the victim, and it might choose dockerd.
exec /usr/bin/docker run --rm --name flakehound \
  --env-file /run/flakehound/env \
  -v /run/flakehound/github-app.pem:/run/secrets/github-app.pem:ro \
  -p 127.0.0.1:8000:8000 \
  --memory 768m --memory-swap 768m \
  --log-driver awslogs \
  --log-opt "awslogs-region=${REGION}" \
  --log-opt "awslogs-group=${LOG_GROUP}" \
  --log-opt "awslogs-stream=${LOG_STREAM}" \
  "$IMAGE"
SCRIPT

# ---------------------------------------------------------------------------
# flakehound-poll — the deploy.
#
# CI has no way to reach this box: the IAM user is denied every SSM action and
# the security group has no inbound rule, so there is nothing to push *to*
# (D-053). Deployment is therefore the box's own job. CI builds an image, moves
# the `latest` tag, and stops; a minute later this notices the tag points at a
# digest that is not the one running, and restarts the unit onto it.
#
# It compares digests rather than tags because the tag never changes — that is
# the whole point of a tag — and the digest is what `flakehound-image` already
# recorded for the container that is running right now.
#
# Restarting is all it does. It does not pull, does not resolve a second time,
# and does not write the runtime file: `flakehound-image` does all three on the
# way back up, so there is exactly one place that decides which bytes run.
# ---------------------------------------------------------------------------
install -m 0755 /dev/stdin /usr/local/bin/flakehound-poll <<'SCRIPT'
#!/bin/bash
set -euo pipefail

REGION=us-east-1
RUN_DIR=/run/flakehound

latest=$(aws ecr describe-images --region "$REGION" --repository-name flakehound \
  --image-ids imageTag=latest --query 'imageDetails[0].imageDigest' --output text)

# A throttled or half-broken describe prints `None`, an error, or nothing at all.
# Treating any of those as "the digest moved" would restart the service on a bad
# API call, so anything that is not a digest ends the run without acting.
if [[ "$latest" != sha256:* ]]; then
  echo "flakehound-poll: :latest did not resolve to a digest (got '${latest}')"
  exit 0
fi

running=none
if [[ -r "${RUN_DIR}/runtime" ]]; then
  running=$(sed -n 's/^IMAGE=.*@//p' "${RUN_DIR}/runtime")
fi

if [[ "$latest" == "$running" ]]; then
  exit 0
fi

echo "flakehound-poll: :latest moved ${running} -> ${latest}, restarting flakehound"
systemctl restart flakehound
SCRIPT

install -m 0644 /dev/stdin /etc/systemd/system/flakehound-poll.service <<'UNIT'
[Unit]
Description=flakehound: adopt a new image if the latest tag has moved
After=flakehound.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/flakehound-poll
UNIT

install -m 0644 /dev/stdin /etc/systemd/system/flakehound-poll.timer <<'UNIT'
[Unit]
Description=flakehound: check for a new image every minute

[Timer]
# A minute is the latency a deploy pays after CI finishes. It is dwarfed by the
# six-minute emulated arm64 build, so polling faster would buy nothing that is
# noticeable; polling slower would make the deploy's own wait look like a hang.
OnBootSec=90s
OnUnitActiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT

# ---------------------------------------------------------------------------
# The unit. This is what ECS used to do, and all that is left of what it did.
#
# `Restart=always` is the entire supervision requirement: D-013 has the
# entrypoint block on `wait -n` so the first of the three processes to die takes
# the container with it, and this is the half that starts it again.
# ---------------------------------------------------------------------------
install -m 0644 /dev/stdin /etc/systemd/system/flakehound.service <<'UNIT'
[Unit]
Description=flakehound: the API, the worker, and the Cloudflare tunnel in one container
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=exec
Restart=always
RestartSec=15s
# Rate limiting is off on purpose. The default gives up after five restarts and
# leaves the unit failed — and there is no shell on this box to start it again,
# so a service that has stopped trying is a service that is down until someone
# launches a new instance. Fifteen seconds between attempts is already not a hot
# loop, and ECS would have gone on replacing the task forever too.
StartLimitIntervalSec=0

ExecStartPre=/usr/local/bin/flakehound-secrets
ExecStartPre=/usr/local/bin/flakehound-image
ExecStartPre=-/usr/bin/docker rm -f flakehound
ExecStart=/usr/local/bin/flakehound-run
ExecStop=/usr/bin/docker stop --time 30 flakehound
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
UNIT

# ---------------------------------------------------------------------------
# Boot self-test. Proves the four things that can only be wrong on a real box —
# the image runs on this architecture, the secrets assembled into a usable
# credential, RDS admits this security group, and the schema is the expected one
# — before anything starts serving. The image declares CMD and no ENTRYPOINT, so
# naming a command here replaces the three-process startup instead of appending
# to it.
# ---------------------------------------------------------------------------
install -m 0644 /dev/stdin /run/flakehound/selftest.py <<'PY'
import asyncio
import platform

from sqlalchemy import text

from app.db import get_engine

TABLES = ("repositories", "workflow_runs", "jobs", "event_queue", "flake_events")


async def main() -> None:
    print(f"selftest.arch={platform.machine()} python={platform.python_version()}", flush=True)
    async with get_engine().connect() as conn:
        version = (await conn.execute(text("select version()"))).scalar_one()
        print(f"selftest.postgres={version.split(',')[0]}", flush=True)
        revision = (await conn.execute(text("select version_num from alembic_version"))).scalar_one()
        print(f"selftest.alembic_revision={revision}", flush=True)
        for table in TABLES:
            count = (await conn.execute(text(f"select count(*) from {table}"))).scalar_one()
            print(f"selftest.{table}={count}", flush=True)


asyncio.run(main())
PY

/usr/local/bin/flakehound-secrets
/usr/local/bin/flakehound-image
# shellcheck disable=SC1091
. /run/flakehound/runtime

docker run --rm \
  --name flakehound-selftest \
  --env-file /run/flakehound/env \
  -v /run/flakehound/github-app.pem:/run/secrets/github-app.pem:ro \
  -v /run/flakehound/selftest.py:/app/selftest.py:ro \
  --log-driver awslogs \
  --log-opt "awslogs-region=${REGION}" \
  --log-opt "awslogs-group=${LOG_GROUP}" \
  --log-opt "awslogs-stream=selftest/${INSTANCE_ID}" \
  "$IMAGE" python selftest.py

systemctl daemon-reload
systemctl enable --now flakehound

# Started after the service, not with it: the timer's first tick must find a
# runtime file to compare against, or it would read `none`, decide the digest
# moved, and restart a container that is still starting.
systemctl enable --now flakehound-poll.timer

# Boot is not finished until the container answers for itself. A unit that
# started is not a service that works, and this is the last moment anything can
# be observed from inside the box.
code=000
for _ in $(seq 1 45); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz || true)
  [[ "$code" == "200" ]] && break
  sleep 2
done

if [[ "$code" != "200" ]]; then
  echo "flakehound.service never answered /healthz on loopback (last code ${code})"
  systemctl status flakehound --no-pager --full || true
  journalctl -u flakehound --no-pager --lines 100 || true
  exit 1
fi

echo "flakehound bootstrap complete on ${INSTANCE_ID}: healthz=${code} image=${IMAGE}"
echo "flakehound serving: $(curl -s http://127.0.0.1:8000/healthz)"
systemctl list-timers flakehound-poll.timer --no-pager || true
