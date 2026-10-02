#!/usr/bin/env bash
# Boots an image the way production does and waits for its health check.
#
#   scripts/image-health.sh IMAGE [ENV=VALUE ...]
#
# Exits non-zero if it never reports healthy, with the container's last log
# lines: printed, or written to WITHHELD_DIR when that is set, because the
# composed image's logs describe the extensions and the Actions log is public.
# Used for the host image (CI) and the composed image (Compose), so both are
# held to the same check.
set -euo pipefail

image="${1:?usage: image-health.sh IMAGE [ENV=VALUE ...]}"
shift
name="severino-hq-health-$$"
# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() { docker rm --force "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

env_args=(
  --env DJANGO_SECRET_KEY=ci-only-container-key-0123456789abcdef0123456789abcdef
  --env DJANGO_ALLOWED_HOSTS=127.0.0.1
  --env SEVERINO_LOG_LEVEL=WARNING
)
for pair in "$@"; do env_args+=(--env "$pair"); done

docker run --detach --name "$name" \
  --publish 127.0.0.1::8000 \
  --health-interval 2s --health-timeout 3s --health-retries 5 --health-start-period 1s \
  "${env_args[@]}" "$image" >/dev/null

# Unhealthy is not final: boot migrates first, and Docker reports healthy again
# once a later check passes. Only the deadline, or a container that exited, ends it.
deadline=$((SECONDS + 90))
while [ "$SECONDS" -lt "$deadline" ]; do
  case "$(docker inspect --format '{{.State.Status}} {{.State.Health.Status}}' "$name")" in
    *" healthy") echo "the image came up healthy in ${SECONDS}s"; exit 0 ;;
    exited*|dead*) break ;;
  esac
  sleep 2
done
if [ -n "${WITHHELD_DIR:-}" ]; then
  mkdir -p "$WITHHELD_DIR"
  docker logs --tail 200 "$name" >"$WITHHELD_DIR/image-health.log" 2>&1
  echo "::error title=Image not healthy::The container did not report healthy within 90 seconds, or exited. Its log is in this run's sealed failure logs."
else
  echo "::error title=Image not healthy::The container did not report healthy within 90 seconds, or exited."
  docker logs --tail 40 "$name" 2>&1
fi
exit 1
