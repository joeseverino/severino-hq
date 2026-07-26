#!/bin/sh
# Deploy one already-built image and restore the previous image on failed health.

set -eu

readonly image="${1:?usage: deploy-image.sh IMAGE}"
readonly app_dir="/opt/apps/severino-hq"
previous_image="$(
    docker inspect --format '{{.Config.Image}}' severino-hq 2>/dev/null || true
)"

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
    severino-hq-controller.timer \
    severino-hq-content-sync.timer 2>/dev/null || true

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
    echo "Controller timer remains stopped pending operator review." >&2
}

cd "${app_dir}"
SEVERINO_IMAGE="${image}" docker compose pull app
SEVERINO_IMAGE="${image}" docker compose up -d --no-build app

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
            severino-hq-controller.timer \
            severino-hq-content-sync.timer 2>/dev/null || true
        rollback
        exit 1
    fi
    sleep 5
done

echo "New image did not become healthy." >&2
docker logs --tail 50 severino-hq >&2 || true
rollback
exit 1
