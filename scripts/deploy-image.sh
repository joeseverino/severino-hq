#!/bin/sh
# Deploy one already-built image and restore the previous image on failed health.
#
# Runs as root, invoked by the deploy through a single sudoers rule naming this
# path. Everything privileged happens here rather than in the workflow, so the
# identity running the workflow needs neither the docker socket nor general
# sudo: it hands over one verified image reference and reads the outcome.
#
# Both inputs come from the cosign-verified image rather than from the deploy
# checkout: the scripts (this file included) and the compose file that decides
# what the container is allowed to be. Deployment has to be able to write to the
# checkout, so nothing root acts on is read from there.

set -eu

readonly image="${1:?usage: deploy-image.sh IMAGE}"
readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
readonly lib_dir="${SEVERINO_HQ_LIB_DIR:-/usr/local/lib/severino-hq}"
readonly sync_program="${SEVERINO_HQ_SBIN_DIR:-/usr/local/sbin}/severino-hq-sync-scripts"
# Every unit that starts work needing the root-owned tree and the running
# image to be one release, or that acts on the web container: the controller's
# timer and the path unit HQ's doorbell fires, every scheduled job, the secret
# refresh, which restarts the web container when its environment changed, and
# the check of the tree against the image. Each is held from before the image
# is replaced until the release has installed itself, so nothing of the
# previous release is started against the new image, and nothing restarts the
# container while its health is being read.
readonly held_units="severino-hq-controller.timer severino-hq-controller.path severino-hq-audit-prune.timer severino-hq-contacts-inbox.timer severino-hq-content-sync.timer severino-hq-public-registry.timer severino-hq-secrets.timer severino-hq-script-drift.timer"
# What those units start. A run already in flight is given this long to end
# against the release it began with before the image is replaced.
readonly held_services="severino-hq-controller.service severino-hq-secrets.service"
readonly drain_seconds="${SEVERINO_HQ_DRAIN_SECONDS:-180}"
readonly log_dir="${SEVERINO_HQ_LOG_DIR:-/var/log/severino-hq}"

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

# The host paths the web container binds decide what it loads as its secrets
# and trusts as a certificate authority. Compose would read them from the
# checkout's .env, which the deploy account writes, so none is taken from there
# as given. The secrets environment is the file hq-secrets renders on
# the root-only tmpfs; the others are checked against what they may be. Each
# is exported, which compose prefers over the file.
readonly root_uid="${SEVERINO_HQ_ROOT_UID:-0}"
readonly web_uid="${SEVERINO_HQ_WEB_UID:-10001}"
readonly web_secret_dir="${SEVERINO_HQ_WEB_SECRET_DIR:-/run/severino-hq-secrets/web}"
# The checkout's copy of the environment, bound only while no tmpfs copy
# exists. The deploy that binds the tmpfs copy removes it.
readonly checkout_env="${app_dir}/secrets/severino_hq_env"
env_value() {
    [ -f "${app_dir}/.env" ] || return 0
    sed -n "s/^${1}=//p" "${app_dir}/.env" | tail -n 1 | sed "s/^[\"']//; s/[\"']\$//"
}
refuse_mount() { echo "Refusing to deploy: $1" >&2; exit 1; }
# GNU stat on the hosts, BSD stat on a Mac running the drill.
owner_links() { stat -c '%u %h' "$1" 2>/dev/null || stat -f '%u %l' "$1"; }
owner_mode() { stat -c '%u %a' "$1" 2>/dev/null || stat -f '%u %Lp' "$1"; }
# A rendered environment: one regular file owned by the web user, with no
# other name, in a directory only root can enter.
rendered_env() {
    [ "$(owner_mode "$1")" = "${root_uid} 700" ] \
        || refuse_mount "$1 is not a directory only root can enter."
    if [ ! -f "$1/severino_hq_env" ] || [ -L "$1/severino_hq_env" ] \
        || [ "$(owner_links "$1/severino_hq_env")" != "${web_uid} 1" ]; then
        refuse_mount "$1/severino_hq_env is not the file hq-secrets renders."
    fi
}
if [ -e "${web_secret_dir}/severino_hq_env" ] || [ -L "${web_secret_dir}/severino_hq_env" ]; then
    rendered_env "${web_secret_dir}"
    app_env_host="${web_secret_dir}/severino_hq_env"
elif [ -e "${checkout_env}" ] || [ -L "${checkout_env}" ]; then
    rendered_env "${app_dir}/secrets"
    app_env_host="${checkout_env}"
else
    app_env_host=/dev/null
fi
controller_run_host="$(env_value SEVERINO_CONTROLLER_RUN_DIR)"
case "${controller_run_host}" in
    "" | /run/severino-hq) controller_run_host=/run/severino-hq ;;
    *) refuse_mount "SEVERINO_CONTROLLER_RUN_DIR must be /run/severino-hq." ;;
esac
controller_ca_host="$(env_value SEVERINO_CONTROLLER_CA_FILE_HOST)"
case "${controller_ca_host}" in
    "" | /dev/null) controller_ca_host=/dev/null ;;
    *..*) refuse_mount "SEVERINO_CONTROLLER_CA_FILE_HOST may not climb out of the certificate store." ;;
    /usr/local/share/ca-certificates/*.crt | /usr/local/share/ca-certificates/*.pem) ;;
    *) refuse_mount "SEVERINO_CONTROLLER_CA_FILE_HOST must be a certificate under /usr/local/share/ca-certificates." ;;
esac
SEVERINO_APP_ENV_FILE_HOST="${app_env_host}"
SEVERINO_CONTROLLER_RUN_DIR="${controller_run_host}"
SEVERINO_CONTROLLER_CA_FILE_HOST="${controller_ca_host}"
export SEVERINO_APP_ENV_FILE_HOST SEVERINO_CONTROLLER_RUN_DIR SEVERINO_CONTROLLER_CA_FILE_HOST

# The registry credential arrives on stdin as two lines, username then token,
# never as arguments or environment: arguments are readable in the process table
# while the command runs, and passing environment through sudo would need a
# SETENV tag that widens the one rule this script exists to keep narrow.
# Optional, so a host that already holds a credential for this registry can
# deploy without one.
registry_user=""
registry_token=""

# Everything this run creates that must not outlive it: the ephemeral registry
# credential, the compose files staged out of the image, and the container they
# are copied from. One trap for all of them, because a second `trap ... EXIT`
# replaces the first rather than adding to it.
docker_config=""
compose_stage=""
compose_cid=""
# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() {
    [ -n "${compose_cid}" ] && docker rm -f "${compose_cid}" >/dev/null 2>&1
    [ -n "${compose_stage}" ] && rm -rf "${compose_stage}"
    [ -n "${docker_config}" ] && rm -rf "${docker_config}"
    return 0
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
if [ ! -t 0 ]; then
    IFS= read -r registry_user || true
    IFS= read -r registry_token || true
fi
if [ -n "${registry_token}" ]; then
    # Written, not logged in. `docker login` stores the token in root's own
    # ~/.docker/config.json, and says so, in a warning, every deploy. That
    # store outlives the command that made it and is read by every later root
    # docker call, so a logout is the only thing standing between one deploy's
    # credential and the next. An ephemeral config directory removes the
    # question: the credential exists in a private tmpfs file for the length of
    # this script, is pointed at by DOCKER_CONFIG rather than by root's home,
    # and the trap that already had to fire deletes it outright.
    docker_config="$(mktemp -d "${SEVERINO_HQ_RUN_DIR:-/run}/severino-hq-deploy-docker.XXXXXX")"
    chmod 0700 "${docker_config}"
    umask 077
    printf '{"auths":{"ghcr.io":{"auth":"%s"}}}\n' \
        "$(printf '%s:%s' "${registry_user:-x-access-token}" "${registry_token}" \
            | base64 | tr -d '\n')" \
        > "${docker_config}/config.json"
    export DOCKER_CONFIG="${docker_config}"
fi

# Verified here, by root, and not only by the workflow step that verified it
# already. The sudoers rule exists because the runner is the party this script
# does not trust; a check that runs on the runner's side of that boundary is a
# check the runner can decline to run. The shape guard above proves the argument
# names this repository's composition, which is exactly what a stolen registry
# token would also be able to push. Only the signature proves who composed it.
#
# The verifier is root's own copy, at an absolute path, because the runner
# installs its cosign into a directory the runner owns. Beside the synced tree
# rather than in it: that tree is replaced wholesale, from the image, by the
# sync this deploy is about to trigger, so a verifier inside it would be
# supplied by the thing it exists to check. No PATH lookup, and no fallback:
# an absent verifier fails the deploy rather than quietly reducing it to the
# shape guard.
readonly cosign="${SEVERINO_HQ_VERIFIER_DIR:-/usr/local/lib/severino-hq-verifier}/cosign"
if [ ! -x "${cosign}" ]; then
    echo "No verifier at ${cosign}; run install-cosign.sh. Refusing to deploy." >&2
    exit 1
fi
if ! "${cosign}" verify \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp \
        "^https://github\.com/${SEVERINO_HQ_REPOSITORY:-joeseverino/severino-hq}/\.github/workflows/compose\.yml@refs/heads/main$" \
    "${image}" >/dev/null 2>&1; then
    echo "Refusing to deploy ${image}: not signed by this repository's compose workflow." >&2
    exit 1
fi

# The compose file the *running* release was deployed with. Falls back to the
# checkout only when the root-owned tree has not been populated yet (a first
# bring-up, before severino-hq-sync-scripts has run), and says so.
#
# It pulls the new image and, snapshotted, puts the old one back on rollback. It
# does not start the new release, which runs under the file its own image
# carries, staged below once that image has been pulled.
if [ -f "${lib_dir}/docker-compose.yml" ]; then
    compose_file="${lib_dir}/docker-compose.yml"
else
    compose_file="${app_dir}/docker-compose.yml"
    echo "warning: ${lib_dir}/docker-compose.yml is absent; using the checkout copy." >&2
fi

# Compose, the sync program and the installer can name ports, volumes and host
# paths, and this script's output is a public Actions log. Their output goes to
# a root-only log on the host; the log shows only whether each one passed.
# run-private.sh is staged beside the compose files, because the sync replaces
# the tree it is read from while it runs.
private() {
    label="$1"
    name="$2"
    shift 2
    install -d -o root -g root -m 0700 "${log_dir}"
    if [ -x "${compose_stage}/run-private.sh" ]; then
        "${compose_stage}/run-private.sh" "${label}" "${log_dir}/${name}.log" "$@"
    else
        "$@"
    fi
}
# --project-directory keeps volume names and relative paths resolving exactly as
# they did when compose was invoked from the checkout, so moving only the file
# changes nothing about what the deployment means.
private_compose() {
    label="$1"
    name="$2"
    shift 2
    private "${label}" "${name}" docker compose \
        -f "${compose_file}" \
        --env-file "${app_dir}/.env" \
        --project-directory "${app_dir}" \
        "$@"
}

previous_image="$(
    docker inspect --format '{{.Config.Image}}' severino-hq 2>/dev/null || true
)"
held_active=""
for unit in ${held_units}; do
    if systemctl is-active --quiet "${unit}"; then
        held_active="${held_active} ${unit}"
    fi
done

# Reclaim before pulling. The currently running image is referenced and cannot
# be pruned, so it remains the rollback target while stale releases are removed.
# Doing this only after deployment is too late when the filesystem is already
# too full for Docker or the runner to make progress.
docker image prune -af >/dev/null
docker builder prune -af >/dev/null
available_kb="$(df -Pk / | awk 'NR == 2 {print $4}')"
if [ "${available_kb}" -lt 524288 ]; then
    echo "Deployment requires at least 512 MiB of free root-disk space." >&2
    exit 1
fi

# Unquoted on purpose: the list is this script's own words, one unit each.
# shellcheck disable=SC2086
hold_units() { systemctl stop ${held_units} 2>/dev/null || true; }
# Exactly the units that were active before the deploy, and no others.
restore_timers() {
    for unit in ${held_active}; do
        systemctl start "${unit}"
    done
}
# Stopping a timer or a path unit starts nothing new; it does not end a run it
# already started. `is-active` is true of a oneshot only while it runs.
drain_runs() {
    waited=0
    while [ "${waited}" -lt "${drain_seconds}" ]; do
        running=0
        for unit in ${held_services}; do
            if systemctl is-active --quiet "${unit}"; then running=1; fi
        done
        [ "${running}" -eq 1 ] || return 0
        sleep 5
        waited=$((waited + 5))
    done
    echo "A run started before the deploy is still going; it fails against the new image and the next one retries." >&2
}
hold_units
drain_runs

controller_backup=""
sync_backup=""
# The root tree and the sync program exactly as they were: the new release's
# files are removed, not merely overwritten.
restore_root_tree() {
    [ -n "${controller_backup}" ] || return 0
    rm -rf "${lib_dir}"
    cp -Rp "${controller_backup}" "${lib_dir}"
    chmod 0755 "${lib_dir}"
    rm -rf "${controller_backup}"
    controller_backup=""
    if [ -n "${sync_backup}" ]; then
        install -o root -g root -m 0755 "${sync_backup}" "${sync_program}"
    fi
}
rollback() {
    if [ -z "${previous_image}" ]; then
        echo "No previous image is available for automatic rollback." >&2
        return 1
    fi
    echo "Restoring previous image ${previous_image}." >&2
    compose_file="${previous_compose}"
    SEVERINO_IMAGE="${previous_image}" private_compose "Restore" restore up -d --no-build app
    restore_root_tree
    restore_timers
    echo "Previous image and the held units restored." >&2
}

# Root-owned and private: root is about to act on what lands here, so nothing
# else may be able to write it between the copy and the `up`.
compose_stage="$(mktemp -d "${SEVERINO_HQ_RUN_DIR:-/run}/severino-hq-compose.XXXXXX")"
chmod 0700 "${compose_stage}"
readonly previous_compose="${compose_stage}/previous.yml"
cp -p "${compose_file}" "${previous_compose}"
if [ -x "${lib_dir}/scripts/run-private.sh" ]; then
    cp -p "${lib_dir}/scripts/run-private.sh" "${compose_stage}/run-private.sh"
fi

if ! SEVERINO_IMAGE="${image}" private_compose "Image pull" pull pull app; then
    echo "Image pull failed; restoring the held units." >&2
    restore_timers
    exit 1
fi

# Out of the image that was verified and pulled above, by its digest, and never
# pulled again: `--pull never` makes a missing local copy an error rather than a
# second fetch, so neither file can come from anything cosign did not check.
# The compose file the new release runs under, and the sync program that
# installs its root tree. No fallback to the host's copies: each is the release's
# own or the deploy stops.
if ! compose_cid="$(docker create --pull never "${image}" true)" \
    || ! docker cp "${compose_cid}:/app/docker-compose.yml" "${compose_stage}/next.yml" \
    || ! docker cp "${compose_cid}:/app/scripts/severino-hq-sync-scripts" "${compose_stage}/sync"; then
    echo "Could not read the release's files from ${image}; restoring the held units." >&2
    restore_timers
    exit 1
fi
docker rm -f "${compose_cid}" >/dev/null
compose_cid=""
compose_file="${compose_stage}/next.yml"

if ! SEVERINO_IMAGE="${image}" private_compose "Replace" replace up -d --no-build app; then
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
        controller_backup="$(mktemp -d "${SEVERINO_HQ_RUN_DIR:-/run}/severino-hq-scripts.XXXXXX")"
        cp -Rp "${lib_dir}/." "${controller_backup}/"
        if [ -f "${sync_program}" ]; then
            sync_backup="${compose_stage}/sync.previous"
            cp -p "${sync_program}" "${sync_backup}"
        fi
        # The release installs itself: the image's own sync program refreshes
        # the root tree from the image now running and is installed as the
        # host's, and the installer that runs is the one it just synced.
        # shellcheck disable=SC2016  # expanded by the inner sh, from its arguments
        if private "Release install" release-install sh -c \
            'sh "$1" && install -o root -g root -m 0755 "$1" "$2" && SEVERINO_HQ_INSTALLER_SYNCED=1 sh "$3"' \
            release-install "${compose_stage}/sync" "${sync_program}" "${lib_dir}/scripts/install-controller.sh"; then
            rm -rf "${controller_backup}"
            controller_backup=""
            # The running container binds the tmpfs copy, so the one on the
            # checkout's disk has no reader.
            if [ "${app_env_host}" != "${checkout_env}" ]; then
                rm -f "${checkout_env}"
            fi
            # The installer enables every shipped timer and path; this puts
            # back whatever was held and is not one of those.
            restore_timers
            echo "Deployed healthy image ${image} with an active controller."
            exit 0
        fi
        echo "Controller activation failed; rolling back application image." >&2
        hold_units
        rollback
        exit 1
    fi
    sleep 5
done

echo "New image did not become healthy." >&2
# Application logs can contain runtime inventory even when they contain no
# credential. Preserve the failure evidence on the host without publishing it
# through the self-hosted Actions runner.
private "Application failure diagnostics" application-failure \
    docker logs --tail 50 severino-hq || true
rollback
exit 1
