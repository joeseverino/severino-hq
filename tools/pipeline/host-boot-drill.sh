#!/usr/bin/env bash
# Runs IMAGE under severino-hq-web.service on this machine's systemd and docker,
# the way a host runs it, through a boot, a crash and a reboot, then sets the
# host up with the release's playbook in RUNNER, the worker image built beside
# it.
#
#   tools/pipeline/host-boot-drill.sh IMAGE RUNNER
#
# A boot starts HQ before its environment is rendered: the unit must wait,
# start nothing, and leave the file's path alone, then come up healthy once
# the file is there. A crash and a reboot must bring it back the same way. A
# release that cannot serve must fail its start, and the previous release
# must come back, as a deploy's rollback brings it back. The unit runs in the
# sandbox every HQ service shares, so the drill proves HQ serves inside it.
# The setup is `hq-host setup`, the half of `hq-host install` after it verifies
# and pulls the runner, since CI's images carry no Compose signature: applied
# through nsenter on this systemd, it must then check clean, and verify must
# name a unit edited after it. Connect and the render are stand-ins of the
# drill's own, a unit and a host drop-in beside the shipped renderer.
#
# Needs systemd as PID 1, docker, and passwordless sudo: a CI runner, not a
# laptop. Elsewhere it says it did not run and passes, unless CI is set.
set -euo pipefail

image="${1:?usage: host-boot-drill.sh IMAGE RUNNER}"
runner="${2:?usage: host-boot-drill.sh IMAGE RUNNER}"
if [ "$(cat /proc/1/comm 2>/dev/null || true)" != systemd ] || ! sudo -n true 2>/dev/null; then
  if [ "${CI:-}" = true ]; then
    echo "::error title=Host boot drill::It needs systemd as PID 1 and passwordless sudo, and CI has neither." >&2
    exit 1
  fi
  echo "The host boot drill did not run: it needs systemd as PID 1 and passwordless sudo."
  exit 0
fi

root=$(cd "$(dirname "$0")/../.." && pwd)

lib=/usr/local/lib/severino-hq
secrets=/run/severino-hq-secrets/web
env_file="$secrets/severino_hq_env"
registry=severino-hq-drill-registry
registry_image=registry:3@sha256:38907510915c3f1604c7e83d6ab602ab3020d092f73bdaed8696773693ff768c
dropin=/etc/systemd/system/severino-hq-web.service.d
sandbox=/etc/systemd/system/severino-hq-.service.d

drill=/usr/local/lib/severino-hq-drill
units=/etc/systemd/system

# What the release installs in $units, by name: a mount among them holds its
# directory until it is stopped, so cleanup stops each one before removing it.
shipped=()
for path in "$root"/deploy/systemd/*; do shipped+=("${path##*/}"); done

cleanup() {
  local running=()
  for name in "${shipped[@]}"; do
    # A drop-in directory runs nothing, and a template runs as its instances.
    case $name in *.d) ;; *) running+=("${name/@./@*.}") ;; esac
  done
  sudo systemctl stop "${running[@]}" drill-connect.service 2>/dev/null || true
  sudo rm -rf "${shipped[@]/#/$units/}"
  sudo find "$units" \( -name 'severino-hq*' -o -name drill-connect.service \
    -o -name 'actions.runner.example.service.d' \) -prune -exec rm -rf {} +
  sudo systemctl daemon-reload
  sudo docker rm --force severino-hq "$registry" >/dev/null 2>&1 || true
  sudo rm -rf "$lib" "$drill" /var/lib/severino-hq /run/severino-hq-secrets /etc/severino-hq \
    /etc/sudoers.d/severino-hq-deploy /etc/tmpfiles.d/severino-hq.conf /var/log/severino-hq
}
trap cleanup EXIT

fail() {
  echo "::error title=Host boot drill::$1"
  sudo systemctl status --no-pager --lines 30 severino-hq-web.service || true
  exit 1
}

# Wait up to $2 seconds for the unit to be $1.
await() {
  local deadline=$((SECONDS + $2))
  while [ "$SECONDS" -lt "$deadline" ]; do
    [ "$(systemctl show --property ActiveState --value severino-hq-web.service)" = "$1" ] && return 0
    sleep 1
  done
  return 1
}

# What a render installs: a root-only directory holding the web account's
# file, and a connections document with none, root's alone at 0400 as the
# controller's preflight requires. The setup below runs it as the renderer's
# ExecStart, so it is a file of the drill's own.
connections=/run/severino-hq-secrets/controller-connections.json
sudo install -d -m 0755 "$drill"
printf '%s\n' 'set -eu' "mkdir -p -m 0700 $secrets" \
  "printf '%s\\n' DJANGO_SECRET_KEY=ci-only-drill-key-0123456789abcdef0123456789abcdef DJANGO_ALLOWED_HOSTS=127.0.0.1 SEVERINO_LOG_LEVEL=WARNING > $env_file" \
  "chown 10001:10001 $env_file" "chmod 0400 $env_file" \
  "printf '%s\\n' '{\"schema_version\": 2, \"connections\": []}' > $connections" "chmod 0400 $connections" |
  sudo tee "$drill/render" >/dev/null
render() { sudo sh "$drill/render"; }

# A boot: /run is empty, and the unit starts before the render.
boot() {
  sudo rm -rf /run/severino-hq-secrets
  sudo install -d -o root -g root -m 0700 /run/severino-hq-secrets
  sudo systemctl start --no-block severino-hq-web.service
  sleep 6
  [ "$(systemctl show --property ActiveState --value severino-hq-web.service)" = activating ] \
    || fail "$1: the unit did not wait for its environment."
  if sudo docker inspect severino-hq >/dev/null 2>&1; then
    fail "$1: a container started without its environment."
  fi
  if sudo test -e "$env_file"; then
    fail "$1: something was made at the environment's path before the render."
  fi
  render
  await active 180 || fail "$1: HQ did not come up once its environment was rendered."
  curl --fail --silent --max-time 5 http://127.0.0.1:8000/health/ready/ >/dev/null \
    || fail "$1: the unit is active but HQ does not answer."
  echo "$1: HQ waited for its environment and came up healthy."
}

# The digest the local registry holds for $1: an image can carry several, and
# only this one resolves from the registry.
registry_digest() {
  sudo docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$1" | grep '^localhost:5000/' | head -n 1
}

# A digest-pinned reference, as a deploy records: pushed to a local registry.
sudo docker run --detach --name "$registry" --publish 127.0.0.1:5000:5000 "$registry_image" >/dev/null
sudo docker tag "$image" localhost:5000/severino-hq:drill
for _ in $(seq 30); do sudo docker push --quiet localhost:5000/severino-hq:drill >/dev/null 2>&1 && break; sleep 1; done
pinned=$(registry_digest localhost:5000/severino-hq:drill)

# Installed as a deploy installs it: the image's own hq-host and the
# repository's unit and tmpfiles rule.
sudo install -d -m 0755 "$lib/deploy/bin" /var/lib/severino-hq
container=$(sudo docker create --pull never "$image" true)
sudo docker cp "$container:/app/deploy/bin/hq-host" "$lib/deploy/bin/hq-host"
sudo docker rm "$container" >/dev/null
sudo chmod 0755 "$lib/deploy/bin/hq-host"
printf '%s\n' "$pinned" | sudo tee /var/lib/severino-hq/release >/dev/null
sudo install -m 0644 "$root/deploy/systemd/severino-hq-web.service" /etc/systemd/system/severino-hq-web.service
sudo install -D -m 0644 "$root/deploy/systemd/severino-hq-.service.d/20-sandbox.conf" "$sandbox/20-sandbox.conf"
# The image's own start period rather than a host's: a release that cannot
# serve fails its start after this, and a healthy one is ready well inside it.
sudo install -d -m 0755 "$dropin"
printf '[Service]\nEnvironment=SEVERINO_HQ_HEALTH_START_PERIOD=20s\n' | sudo tee "$dropin/drill.conf" >/dev/null
sudo systemd-tmpfiles --create "$root/deploy/tmpfiles/severino-hq.conf"
sudo systemctl daemon-reload

boot "Boot"

sudo docker kill severino-hq >/dev/null
await activating 30 || await active 1 || fail "Crash: the unit did not notice the container died."
await active 180 || fail "Crash: HQ did not come back."
echo "Crash: the unit brought HQ back."

sudo systemctl stop severino-hq-web.service
boot "Reboot"

# A release whose health check fails: the restart onto it fails, then the
# previous release is recorded and comes back.
printf 'FROM %s\nHEALTHCHECK --interval=2s --timeout=2s --retries=1 CMD ["false"]\n' "$pinned" |
  sudo docker build --quiet --tag localhost:5000/severino-hq:broken - >/dev/null
sudo docker push --quiet localhost:5000/severino-hq:broken >/dev/null
broken=$(registry_digest localhost:5000/severino-hq:broken)
printf '%s\n' "$broken" | sudo tee /var/lib/severino-hq/release >/dev/null
if sudo systemctl restart severino-hq-web.service; then
  fail "Rollback: a release that cannot serve started."
fi
printf '%s\n' "$pinned" | sudo tee /var/lib/severino-hq/release >/dev/null
sudo systemctl restart severino-hq-web.service || fail "Rollback: the previous release did not come back."
curl --fail --silent --max-time 5 http://127.0.0.1:8000/health/ready/ >/dev/null \
  || fail "Rollback: the unit is active but HQ does not answer."
echo "Rollback: a release that cannot serve failed its start, and the previous one came back."

# A release that changes the schema, then cannot serve: its boot keeps the
# database before migrating, and a rollback puts that back (as hq-host's
# restoreSchema does) so the previous release runs on the schema it knows.
data=$(sudo docker volume inspect --format '{{.Mountpoint}}' severino-hq_severino_data)
# One number read from the database on the data volume.
query() {
  sudo docker run --rm --mount type=volume,source=severino-hq_severino_data,target=/data \
    --entrypoint python "$pinned" -c 'import sqlite3, sys
from contextlib import closing
with closing(sqlite3.connect("/data/severino.sqlite3")) as db:
    print(db.execute(sys.argv[1]).fetchone()[0])' "$1"
}
migrated() { [ "$(query "SELECT count(*) FROM sqlite_master WHERE name = 'drill_schema_change'")" = 1 ]; }
applied() { query "SELECT count(*) FROM django_migrations"; }
context=$(mktemp -d)
latest=$(sudo docker run --rm --entrypoint sh "$pinned" -c 'ls hq/platform/core/migrations | grep -E "^[0-9]{4}_.*\.py$" | sort | tail -n 1')
cat >"$context/9999_drill_schema_change.py" <<EOF
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "${latest%.py}")]
    operations = [migrations.RunSQL("CREATE TABLE drill_schema_change (id integer)")]
EOF
cat >"$context/Dockerfile" <<EOF
FROM $pinned
COPY 9999_drill_schema_change.py hq/platform/core/migrations/
HEALTHCHECK --interval=2s --timeout=2s --retries=1 CMD ["false"]
EOF
sudo docker build --quiet --tag localhost:5000/severino-hq:migrating "$context" >/dev/null
rm -rf "$context"
sudo docker push --quiet localhost:5000/severino-hq:migrating >/dev/null
migrating=$(registry_digest localhost:5000/severino-hq:migrating)
sudo systemctl stop severino-hq-web.service
before=$(applied)
# As Release does: a copy an earlier boot kept (the first boot kept the empty
# database) is not this release's, and restoring it would restore nothing.
sudo rm -f "$data/severino.sqlite3.before-migrate"
printf '%s\n' "$migrating" | sudo tee /var/lib/severino-hq/release >/dev/null
if sudo systemctl restart severino-hq-web.service; then
  fail "Schema rollback: a release that cannot serve started."
fi
sudo systemctl stop severino-hq-web.service
sudo test -f "$data/severino.sqlite3.before-migrate" \
  || fail "Schema rollback: the release migrated without keeping the database first."
migrated || fail "Schema rollback: the release did not migrate, so the drill proves nothing."
sudo mv "$data/severino.sqlite3.before-migrate" "$data/severino.sqlite3"
sudo rm -f "$data/severino.sqlite3-wal" "$data/severino.sqlite3-shm"
if migrated || [ "$(applied)" != "$before" ]; then
  fail "Schema rollback: the database kept before migrating is not the one the release found."
fi
printf '%s\n' "$pinned" | sudo tee /var/lib/severino-hq/release >/dev/null
sudo systemctl restart severino-hq-web.service || fail "Schema rollback: the previous release did not come back."
curl --fail --silent --max-time 5 http://127.0.0.1:8000/health/ready/ >/dev/null \
  || fail "Schema rollback: the unit is active but HQ does not answer."
echo "Schema rollback: a release that migrated and could not serve was undone, schema and all."

# The setup. Connect is a unit that only starts, and the renderer runs the
# drill's render: both the drill's own files, which cleanup removes.
sudo docker tag "$runner" localhost:5000/severino-hq-runner:drill
for _ in $(seq 30); do sudo docker push --quiet localhost:5000/severino-hq-runner:drill >/dev/null 2>&1 && break; sleep 1; done
runner_pinned=$(registry_digest localhost:5000/severino-hq-runner:drill)
sudo "$lib/deploy/bin/hq-host" sync "$pinned"
sudo install -d -m 0755 "$lib/deploy/automation" /etc/severino-hq "$units/severino-hq-secrets.service.d"
printf '%s\n' "$runner_pinned" | sudo tee "$lib/deploy/automation/image" >/dev/null
printf '%s\n' '{"vault": "Example Vault", "env_item": "example env",' \
  '"connect": {"unit": "drill-connect.service", "url": "http://127.0.0.1:880", "credential": "op_connect_example"},' \
  '"runner": {"unit": "actions.runner.example.service", "user": "nobody"}}' | sudo tee /etc/severino-hq/host.json >/dev/null
printf '[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/true\n' | sudo tee "$units/drill-connect.service" >/dev/null
printf '[Service]\nLoadCredentialEncrypted=\nExecStart=\nExecStart=/bin/sh %s/render\n' "$drill" |
  sudo tee "$units/severino-hq-secrets.service.d/90-drill.conf" >/dev/null
sudo systemctl daemon-reload

setup_failed() {
  sudo tail -n 120 /var/log/severino-hq/setup.log || true
  fail "Setup: $1"
}
sudo "$lib/deploy/bin/hq-host" setup "$runner_pinned" || setup_failed "the playbook did not apply through the runner image."
for unit in severino-hq-secrets.timer severino-hq-controller.timer severino-hq-unit.socket; do
  systemctl is-active --quiet "$unit" || setup_failed "$unit is not started."
done
sudo test -f /etc/sudoers.d/severino-hq-deploy || setup_failed "the deploy rule is not installed."
check=$(sudo "$lib/deploy/bin/hq-host" install --check) || setup_failed "the check did not run: $check"
grep -q 'changed=0 ' <<<"$check" || setup_failed "a check right after the apply would change something: $check"
echo "Setup: the playbook applied through the runner image and nsenter, and a check finds nothing to change."

echo '# edited' | sudo tee -a "$units/severino-hq-web.service" >/dev/null
drift=$(sudo "$lib/deploy/bin/hq-host" verify 2>&1 || true)
grep -qF "Install every drop-in and the units the restart needs: $units/severino-hq-web.service" <<<"$drift" \
  || setup_failed "verify did not name the unit edited after the setup: $drift"
echo "Setup: verify names a unit edited after the setup."
