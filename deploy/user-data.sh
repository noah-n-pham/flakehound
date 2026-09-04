#!/bin/bash
# Bootstrap for the one instance that runs flakehound.
#
# Passed to `ec2 run-instances --user-data`, so this file is the whole
# configuration of the box: there is no console step and nothing typed by hand.
# The instance is cattle — to change how it is set up, change this file and
# launch a replacement rather than logging in and editing.
#
# Everything it needs comes over outbound connections: the image from ECR, the
# credentials from Secrets Manager, both authorized by the instance profile.
# Nothing dials in. The security group has no inbound rule and the container
# publishes no port; ingress is `cloudflared` inside the container dialling out.
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

# ---------------------------------------------------------------------------
# The container's environment, assembled on the box from Secrets Manager.
#
# Written to tmpfs, so no credential is ever on the disk, in the image, or in a
# process argument list. This replaces the task definition's `secrets` block;
# the names and the sources are the same ones ECS injected.
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

/usr/local/bin/flakehound-secrets

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ECR%/*}"

docker pull "${ECR}:latest"

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

docker run --rm \
  --name flakehound-selftest \
  --env-file /run/flakehound/env \
  -v /run/flakehound/github-app.pem:/run/secrets/github-app.pem:ro \
  -v /run/flakehound/selftest.py:/app/selftest.py:ro \
  --log-driver awslogs \
  --log-opt "awslogs-region=${REGION}" \
  --log-opt "awslogs-group=${LOG_GROUP}" \
  --log-opt "awslogs-stream=selftest/${INSTANCE_ID}" \
  "${ECR}:latest" python selftest.py

echo "flakehound bootstrap complete on ${INSTANCE_ID}"
