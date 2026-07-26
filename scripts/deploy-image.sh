#!/bin/sh
# Deploy one already-built image and restore the previous image on failed health.

set -eu

readonly image="${1:?usage: deploy-image.sh IMAGE}"
readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly controller_timer="severino-hq-controller.timer"
readonly content_timer="severino-hq-content-sync.timer"
previous_image="$(
    docker inspect --format '{{.Config.Image}}' severino-hq 2>/dev/null || true
)"
controller_was_active=0
content_was_active=0
if sudo systemctl is-active --quiet "${controller_timer}"; then
    controller_was_active=1
fi
if sudo systemctl is-active --quiet "${content_timer}"; then
    content_was_active=1
fi

# Reclaim before pulling. The currently running image is referenced and cannot
# be pruned, so it remains the rollback target while stale releases are removed.
# Doing this only after deployment is too late when the filesystem is already
# too full for Docker or the runner to make progress.
docker image prune -af
docker builder prune -af
available_kb="$(df -Pk / | awk 'NR == 2 {print $4}')"
if [ "${available_kb}" -lt 524288 ]; then
    echo "Deployment requires at least 512 MiB of free root-disk space." >&2
    exit 1
fi

sudo systemctl stop \
    "${controller_timer}" \
    "${content_timer}" 2>/dev/null || true

restore_timers() {
    if [ "${controller_was_active}" -eq 1 ]; then
        sudo systemctl start "${controller_timer}"
    fi
    if [ "${content_was_active}" -eq 1 ]; then
        sudo systemctl start "${content_timer}"
    fi
}

rollback() {
    if [ -z "${previous_image}" ]; then
        echo "No previous image is available for automatic rollback." >&2
        return 1
    fi
    echo "Restoring previous image ${previous_image}." >&2
    (
        cd "${app_dir}"
        SEVERINO_IMAGE="${previous_image}" docker compose up -d --no-build app
    )
    restore_timers
    echo "Previous image and prior controller timer state restored." >&2
}

cd "${app_dir}"
if ! SEVERINO_IMAGE="${image}" docker compose pull app; then
    echo "Image pull failed; restoring prior controller timer state." >&2
    restore_timers
    exit 1
fi
if ! SEVERINO_IMAGE="${image}" docker compose up -d --no-build app; then
    echo "Application replacement failed." >&2
    rollback
    exit 1
fi

for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    status="$(
        docker inspect --format '{{.State.Health.Status}}' severino-hq \
            2>/dev/null || echo missing
    )"
    echo "health: ${status}"
    if [ "${status}" = "healthy" ]; then
        if sudo "${app_dir}/scripts/install-controller.sh"; then
            echo "Deployed healthy image ${image} with an active controller."
            exit 0
        fi
        echo "Controller activation failed; rolling back application image." >&2
        sudo systemctl stop \
            "${controller_timer}" \
            "${content_timer}" 2>/dev/null || true
        rollback
        exit 1
    fi
    sleep 5
done

echo "New image did not become healthy." >&2
docker logs --tail 50 severino-hq >&2 || true
rollback
exit 1
