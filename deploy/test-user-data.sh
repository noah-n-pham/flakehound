#!/bin/bash
# Dry-run `user-data.sh` in the distribution the instance actually runs, with
# every call that leaves the machine stubbed.
#
#   docker run --rm -v "$PWD/deploy:/deploy:ro" amazonlinux:2023 \
#     bash /deploy/test-user-data.sh
#
# This exists because there is no shell on the real box. A bootstrap that dies
# halfway is invisible until after it has taken production down, which is
# precisely what happened the first time this file did not exist: the self-test
# was installed into a directory nothing had created yet, `set -e` ended the
# script, and the instance came up with no service on it.
#
# What is real here: every filesystem effect. The four scripts get installed and
# then executed, the unit file gets written, the heredocs get expanded, the
# python that parses the RDS secret really runs. What is faked: `dnf`, `docker`,
# `systemctl`, `journalctl`, the AWS CLI, and the instance metadata service. So
# this proves the shape of the bootstrap and the order it does things in — not
# that the credentials are right or that RDS is reachable, which only the real
# boot self-test can say.
set -euo pipefail

STUBS=/tmp/stubs
mkdir -p "$STUBS"

cat > "$STUBS/dnf" <<'EOF'
#!/bin/bash
echo "[stub] dnf $*"
EOF

cat > "$STUBS/systemctl" <<'EOF'
#!/bin/bash
echo "[stub] systemctl $*"
EOF

cat > "$STUBS/journalctl" <<'EOF'
#!/bin/bash
echo "[stub] journalctl $*"
EOF

cat > "$STUBS/docker" <<'EOF'
#!/bin/bash
echo "[stub] docker $*"
EOF

# Answers the three metadata and health calls by looking at the argument list,
# the same way the real ones are distinguished.
cat > "$STUBS/curl" <<'EOF'
#!/bin/bash
args="$*"
case "$args" in
  *latest/api/token*)       echo "stub-imds-token" ;;
  *meta-data/instance-id*)  echo "i-0stubbed0stubbed0" ;;
  *healthz*)                echo -n "200" ;;
  *)                        echo "[stub] curl $args" >&2; exit 1 ;;
esac
EOF

# The RDS secret has to be real JSON with the two keys the script reads by name,
# or the python that pulls them out is not being tested at all.
cat > "$STUBS/aws" <<'EOF'
#!/bin/bash
args="$*"
case "$args" in
  *"secretsmanager get-secret-value"*)
    case "$args" in
      *"rds!db"*) echo '{"username":"ciinsights","password":"stub/pass@word#1"}' ;;
      *github-app-private-key*)
        printf '%s\n' "-----BEGIN RSA PRIVATE KEY-----" "c3R1Yg==" "-----END RSA PRIVATE KEY-----" ;;
      *) echo "stub-secret-value" ;;
    esac ;;
  *"ecr get-login-password"*)  echo "stub-ecr-password" ;;
  *"ecr describe-images"*)     echo "sha256:0000000000000000000000000000000000000000000000000000000000000000" ;;
  *"logs create-log-stream"*)  ;;
  *"logs put-log-events"*)     echo "[stub] shipped $(python3 -c 'import json,sys; print(len(json.load(open("/tmp/init-events.json"))))' 2>/dev/null || echo '?') log events" >&2 ;;
  *) echo "[stub] aws $args" ;;
esac
EOF

chmod +x "$STUBS"/*
export PATH="$STUBS:$PATH"

# The container image has no systemd, so the directory a real instance already
# has is part of what this harness has to fake.
mkdir -p /etc/systemd/system

# The container's uid does not exist here, and `chown 10001:10001` is one of the
# things worth proving works.
bash /deploy/user-data.sh

echo
echo "=== what the bootstrap left behind ==="
for f in /usr/local/bin/flakehound-secrets /usr/local/bin/flakehound-image \
         /usr/local/bin/flakehound-run /usr/local/bin/flakehound-poll \
         /etc/systemd/system/flakehound.service \
         /etc/systemd/system/flakehound-poll.service \
         /etc/systemd/system/flakehound-poll.timer \
         /run/flakehound/env /run/flakehound/runtime /run/flakehound/selftest.py \
         /run/flakehound/github-app.pem; do
  if [[ -e "$f" ]]; then
    printf 'ok    %-45s %s\n' "$f" "$(stat -c '%a %U:%G' "$f")"
  else
    printf 'MISSING %s\n' "$f"
    exit 1
  fi
done

echo
echo "=== the container's environment, keys only ==="
sed 's/=.*/=<redacted>/' /run/flakehound/env

echo
echo "=== the image systemd would start ==="
cat /run/flakehound/runtime

echo
echo "=== the private key is owned by the container's uid, not root ==="
stat -c '%n %a %u:%g' /run/flakehound/github-app.pem

echo
echo "=== the RDS password survived a shell round trip intact ==="
# It contains @ / and # on purpose: D-019 percent-escapes it into the URL, and an
# unquoted expansion anywhere in the chain would have mangled or split it here.
grep -q '^DB_PASSWORD=stub/pass@word#1$' /run/flakehound/env \
  && echo "ok    DB_PASSWORD is byte-exact" \
  || { echo "FAIL  DB_PASSWORD was mangled: $(grep '^DB_PASSWORD=' /run/flakehound/env)"; exit 1; }

echo
echo "=== the poll leaves a matching digest alone ==="
# The stub resolves :latest to the same digest the bootstrap already recorded, so
# this is the every-minute case: 1,440 runs a day that must do nothing at all.
out=$(flakehound-poll)
if [[ -n "$out" ]]; then
  echo "FAIL  poll acted when the digest had not moved: $out"
  exit 1
fi
echo "ok    no output, no restart"

echo
echo "=== the poll restarts when the digest has moved ==="
sed -i 's/@sha256:0*$/@sha256:'"$(printf 'a%.0s' {1..64})"'/' /run/flakehound/runtime
out=$(flakehound-poll)
echo "$out"
grep -q 'systemctl restart flakehound' <<<"$out" \
  || { echo "FAIL  poll did not restart the unit"; exit 1; }
grep -q 'moved sha256:aaaa' <<<"$out" \
  || { echo "FAIL  poll did not read the running digest out of the runtime file"; exit 1; }
echo "ok    restarted onto the new digest"

echo
echo "=== a describe that does not return a digest is not a deploy ==="
# Every failure mode of the AWS CLI ends up here — throttling, an expired
# instance profile, a repository that answers `None`. Restarting on any of them
# would turn a transient API error into an outage.
cat > "$STUBS/aws" <<'EOF'
#!/bin/bash
case "$*" in
  *"ecr describe-images"*) echo "None" ;;
  *) echo "[stub] aws $*" ;;
esac
EOF
chmod +x "$STUBS/aws"
out=$(flakehound-poll)
echo "$out"
grep -q 'did not resolve to a digest' <<<"$out" \
  || { echo "FAIL  poll accepted a non-digest"; exit 1; }
if grep -q 'systemctl restart' <<<"$out"; then
  echo "FAIL  poll restarted on a bad describe"
  exit 1
fi
echo "ok    left the service alone"

echo
echo "dry run passed"
