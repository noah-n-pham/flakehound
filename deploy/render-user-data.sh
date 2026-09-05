#!/bin/bash
# Fill __PLACEHOLDER__ markers from deploy/production.env.
#
#   deploy/render-user-data.sh                 # user-data to stdout
#   deploy/render-user-data.sh --policy        # IAM policy to stdout
#   deploy/render-user-data.sh [env-file]
#   deploy/render-user-data.sh --policy [env-file]
#
# The running instance is not rebuilt by this. User-data only runs at first
# boot. A replacement launch is: render, then
# `aws ec2 run-instances --user-data file://...`.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
MODE=user-data
ENV_FILE="$HERE/production.env"

if [[ "${1:-}" == "--policy" ]]; then
  MODE=policy
  shift
fi
if [[ -n "${1:-}" ]]; then
  ENV_FILE=$1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "render-user-data: missing $ENV_FILE" >&2
  echo "copy deploy/production.env.example to deploy/production.env and fill it" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

REQUIRED=(
  AWS_ACCOUNT_ID
  DB_HOST
  RDS_SECRET_ARN
  GITHUB_APP_PRIVATE_KEY_SECRET_ARN
  GITHUB_WEBHOOK_SECRET_ARN
  INTERNAL_API_TOKEN_SECRET_ARN
  TUNNEL_TOKEN_SECRET_ARN
  GITHUB_APP_ID
  OBSERVATION_INSTALLATION_ID
)

for name in "${REQUIRED[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "render-user-data: $name is empty in $ENV_FILE" >&2
    exit 1
  fi
done

case "$MODE" in
  user-data) src="$HERE/user-data.sh" ;;
  policy)    src="$HERE/github-deploy-policy.json" ;;
  *) echo "render-user-data: unknown mode $MODE" >&2; exit 1 ;;
esac

body=$(<"$src")
for name in "${REQUIRED[@]}"; do
  placeholder="__${name}__"
  value=${!name}
  body=${body//"$placeholder"/"$value"}
done

# Policy only needs the account id; leftover markers on that file are fine
# only if they are not required. Fail on any remaining __NAME__ either way.
leftovers=$(printf '%s' "$body" | grep -oE '__[A-Z0-9_]+__' | sort -u || true)
if [[ -n "$leftovers" ]]; then
  echo "render-user-data: unreplaced placeholder(s):" >&2
  printf '%s\n' "$leftovers" >&2
  exit 1
fi

printf '%s' "$body"
[[ "$body" == *$'\n' ]] || printf '\n'
