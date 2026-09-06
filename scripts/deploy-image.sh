#!/bin/sh
# Deploy one already-built image and restore the previous image on failed health.
#
# Runs as root, invoked by the deploy through a single sudoers rule naming this
# path. Everything privileged happens here rather than in the workflow, so the
# identity running the workflow needs neither the docker socket nor general
# sudo -- it hands over one verified image reference and reads the outcome.
#
# Both inputs come from the cosign-verified image rather than from the deploy
# checkout: the scripts (this file included) and the compose file that decides
# what the container is allowed to be. Deployment has to be able to write to the
# checkout, so nothing root acts on is read from there.

set -eu

readonly image="${1:?usage: deploy-image.sh IMAGE}"
readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly lib_dir="${SEVERINO_HQ_LIB_DIR:-/usr/local/lib/severino-hq}"
readonly controller_timer="severino-hq-controller.timer"
readonly content_timer="severino-hq-content-sync.timer"

if [ "$(id -u)" -ne 0 ]; then
    echo "deploy-image.sh must run as root." >&2
    exit 1
fi

# A sudoers rule naming this script lets the caller choose the argument, and the
# argument decides what root runs as a container. Constrain it here rather than
# trusting the caller: only a digest-pinned reference to this repository's own
# composition is deployable. A tag would be mutable and a different repository
# would be somebody else's code.
readonly expected_prefix="${SEVERINO_HQ_IMAGE_PREFIX:-ghcr.io/joeseverino/severino-hq/composition@sha256:}"
case "${image}" in
    "${expected_prefix}"*)
        digest="${image#"${expected_prefix}"}"
        case "${digest}" in
            *[!0-9a-f]* | "") echo "Image digest is not hexadecimal." >&2; exit 1 ;;
        esac
        [ "${#digest}" -eq 64 ] || { echo "Image digest is not 64 characters." >&2; exit 1; }
        ;;
    *)
        echo "Refusing to deploy ${image}: not a digest-pinned composition reference." >&2
        exit 1
        ;;
esac

# The registry credential arrives on stdin as two lines, username then token,
# never as arguments or environment: arguments are readable in the process table
# while the command runs, and passing environment through sudo would need a
# SETENV tag that widens the one rule this script exists to keep narrow.
# Optional, so a host that already holds a credential for this registry can
# deploy without one.
registry_user=""
registry_token=""
if [ ! -t 0 ]; then
    IFS= read -r registry_user || true
    IFS= read -r registry_token || true
fi
if [ -n "${registry_token}" ]; then
    printf '%s' "${registry_token}" \
        | docker login ghcr.io -u "${registry_user:-x-access-token}" --password-stdin >/dev/null
    # Whatever happens next, the credential does not outlive this deploy. The
    # host is a machine in the house, not a container thrown away afterwards.
    trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT INT TERM
fi

# The compose file root acts on. Falls back to the checkout only when the
# root-owned tree has not been populated yet -- a first bring-up, before
# severino-hq-sync-scripts has run -- and says so, because that path is the one
# this script exists to stop using silently.
if [ -f "${lib_dir}/docker-compose.yml" ]; then
    readonly compose_file="${lib_dir}/docker-compose.yml"
else
    readonly compose_file="${app_dir}/docker-compose.yml"
    echo "warning: ${lib_dir}/docker-compose.yml is absent; using the checkout copy." >&2
fi

# --project-directory keeps volume names and relative paths resolving exactly as
# they did when compose was invoked from the checkout, so moving only the file
# changes nothing about what the deployment means.
compose() {
    docker compose \
        -f "${compose_file}" \
        --env-file "${app_dir}/.env" \
        --project-directory "${app_dir}" \
        "$@"
}

previous_image="$(
    docker inspect --format '{{.Config.Image}}' severino-hq 2>/dev/null || true
)"
controller_was_active=0
content_was_active=0
if systemctl is-active --quiet "${controller_timer}"; then
    controller_was_active=1
fi
if systemctl is-active --quiet "${content_timer}"; then
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

systemctl stop \
    "${controller_timer}" \
    "${content_timer}" 2>/dev/null || true

restore_timers() {
    if [ "${controller_was_active}" -eq 1 ]; then
        systemctl start "${controller_timer}"
    fi
    if [ "${content_was_active}" -eq 1 ]; then
        systemctl start "${content_timer}"
    fi
}

rollback() {
    if [ -z "${previous_image}" ]; then
        echo "No previous image is available for automatic rollback." >&2
        return 1
    fi
    echo "Restoring previous image ${previous_image}." >&2
    SEVERINO_IMAGE="${previous_image}" compose up -d --no-build app
    restore_timers
    echo "Previous image and prior controller timer state restored." >&2
}

if ! SEVERINO_IMAGE="${image}" compose pull app; then
    echo "Image pull failed; restoring prior controller timer state." >&2
    restore_timers
    exit 1
fi
if ! SEVERINO_IMAGE="${image}" compose up -d --no-build app; then
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
        if "${lib_dir}/scripts/install-controller.sh"; then
            echo "Deployed healthy image ${image} with an active controller."
            exit 0
        fi
        echo "Controller activation failed; rolling back application image." >&2
        systemctl stop \
            "${controller_timer}" \
            "${content_timer}" 2>/dev/null || true
        rollback
        exit 1
    fi
    sleep 5
done

echo "New image did not become healthy." >&2
# Application logs can contain runtime inventory even when they contain no
# credential. Preserve the failure evidence on the host without publishing it
# through the self-hosted Actions runner.
install -d -o root -g root -m 0700 /var/log/severino-hq
"${lib_dir}/scripts/run-private.sh" \
    "Application failure diagnostics" \
    /var/log/severino-hq/application-failure.log \
    docker logs --tail 50 severino-hq || true
rollback
exit 1
