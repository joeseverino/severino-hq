#!/usr/bin/env bash
# Runs IMAGE under severino-hq-web.service on this machine's systemd and docker,
# the way a host runs it, through a boot, a crash and a reboot.
#
#   scripts/host-boot-drill.sh IMAGE
#
# A boot starts HQ before its environment is rendered: the unit must wait,
# start nothing, and leave the file's path alone, then come up healthy once
# the file is there. A crash and a reboot must bring it back the same way.
#
# Needs systemd as PID 1, docker, and passwordless sudo: a CI runner, not a
# laptop. Elsewhere it says it did not run and passes, unless CI is set.
set -euo pipefail

image="${1:?usage: host-boot-drill.sh IMAGE}"
if [ "$(cat /proc/1/comm 2>/dev/null || true)" != systemd ] || ! sudo -n true 2>/dev/null; then
  if [ "${CI:-}" = true ]; then
    echo "::error title=Host boot drill::It needs systemd as PID 1 and passwordless sudo, and CI has neither." >&2
    exit 1
  fi
  echo "The host boot drill did not run: it needs systemd as PID 1 and passwordless sudo."
  exit 0
fi

root=$(cd "$(dirname "$0")/.." && pwd)
lib=/usr/local/lib/severino-hq
secrets=/run/severino-hq-secrets/web
env_file="$secrets/severino_hq_env"
registry=severino-hq-drill-registry
registry_image=registry:3@sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8

cleanup() {
  sudo systemctl stop severino-hq-web.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/severino-hq-web.service
  sudo systemctl daemon-reload
  sudo docker rm --force severino-hq "$registry" >/dev/null 2>&1 || true
  sudo rm -rf "$lib" /var/lib/severino-hq /run/severino-hq-secrets
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

# What a render installs: a root-only directory holding the web account's file.
render() {
  sudo install -d -o root -g root -m 0700 "$secrets"
  printf '%s\n' \
    DJANGO_SECRET_KEY=ci-only-drill-key-0123456789abcdef0123456789abcdef \
    DJANGO_ALLOWED_HOSTS=127.0.0.1 \
    SEVERINO_LOG_LEVEL=WARNING | sudo tee "$env_file" >/dev/null
  sudo chown 10001:10001 "$env_file"
  sudo chmod 0400 "$env_file"
}

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

# A digest-pinned reference, as a deploy records: pushed to a local registry.
sudo docker run --detach --name "$registry" --publish 127.0.0.1:5000:5000 "$registry_image" >/dev/null
sudo docker tag "$image" localhost:5000/severino-hq:drill
for _ in $(seq 30); do sudo docker push --quiet localhost:5000/severino-hq:drill >/dev/null 2>&1 && break; sleep 1; done
pinned=$(sudo docker inspect --format '{{index .RepoDigests 0}}' localhost:5000/severino-hq:drill)

# Installed as a deploy installs it: the image's own hq-host and the
# repository's unit and tmpfiles rule.
sudo install -d -m 0755 "$lib/deploy/bin" /var/lib/severino-hq
container=$(sudo docker create --pull never "$image" true)
sudo docker cp "$container:/app/deploy/bin/hq-host" "$lib/deploy/bin/hq-host"
sudo docker rm "$container" >/dev/null
sudo chmod 0755 "$lib/deploy/bin/hq-host"
printf '%s\n' "$pinned" | sudo tee /var/lib/severino-hq/release >/dev/null
sudo install -m 0644 "$root/deploy/systemd/severino-hq-web.service" /etc/systemd/system/severino-hq-web.service
sudo systemd-tmpfiles --create "$root/deploy/tmpfiles/severino-hq.conf"
sudo systemctl daemon-reload

boot "Boot"

sudo docker kill severino-hq >/dev/null
await activating 30 || await active 1 || fail "Crash: the unit did not notice the container died."
await active 180 || fail "Crash: HQ did not come back."
echo "Crash: the unit brought HQ back."

sudo systemctl stop severino-hq-web.service
boot "Reboot"
