#!/bin/sh
# Rehearses a release on the deploy host, touching nothing live. Run it
# before approving a release that migrates:
#
#   mise run shadow-run IMAGE          (from a checkout: copies this script to
#                                       SEVERINO_HQ_DEPLOY_HOST and runs it there)
#   scripts/shadow-run.sh IMAGE        (as root, on the deploy host)
#
#   1. Copies the production database, read-only and as HQ's account, into a
#      scratch volume.
#   2. Starts IMAGE's web process on the copy with no network, so its
#      migrations run against real rows, and waits for it to be ready.
#   3. Checks the bridge socket, the copy's integrity, scheduled work over the
#      socket, and that the process was not killed for memory.
#   4. Runs IMAGE's controller in plan mode (it reads providers and writes to
#      none) on copies of the connections and identities the host already
#      rendered. The host's launcher is not used: it runs the live image.
#   5. Fails if the live web unit or its container restarted meanwhile.
#
# It never renders secrets: hq-secrets restarts the live web unit when it
# installs. The last line is the verdict, one line of key=value pairs:
#
#   shadow-run: result=pass ready=yes socket=ok integrity=ok jobs=ok memory=ok plan=ok live=untouched
#
# Exit 0 only when result=pass.
set -eu

image="${1:?usage: shadow-run.sh IMAGE}"

# The host's layout (controller/host/layout.go). Overridable for the test.
live="${SHADOW_LIVE_CONTAINER:-severino-hq}"
web_unit="${SHADOW_WEB_UNIT:-severino-hq-web.service}"
secret_dir="${SHADOW_SECRET_DIR:-/run/severino-hq-secrets}"
acme_dir="${SHADOW_ACME_DIR:-/opt/apps/severino-hq/secrets/acme}"
# A tmpfs: the plan's copies of identities are written here.
scratch="${SHADOW_DIR:-/run/hq-shadow}"
uid=10001

shadow=hq-shadow
volume=hq-shadow-data
bridge=hq-shadow-bridge

ready=no socket=- integrity=- jobs=- memory=- plan=- live_state=-

cleanup() {
    docker rm -f "${shadow}" >/dev/null 2>&1 || true
    docker volume rm "${volume}" "${bridge}" >/dev/null 2>&1 || true
    rm -rf "${scratch}"
}

verdict() {
    result=pass
    for value in "${ready}" "${socket}" "${integrity}" "${jobs}" "${memory}" "${plan}"; do
        case "${value}" in yes | ok) ;; *) result=fail ;; esac
    done
    [ "${live_state}" = untouched ] || result=fail
    printf 'shadow-run: result=%s ready=%s socket=%s integrity=%s jobs=%s memory=%s plan=%s live=%s\n' \
        "${result}" "${ready}" "${socket}" "${integrity}" "${jobs}" "${memory}" "${plan}" "${live_state}"
    [ "${result}" = pass ]
}

live_marks() {
    printf '%s %s\n' \
        "$(systemctl show "${web_unit}" -p ActiveEnterTimestamp --value)" \
        "$(docker inspect --format '{{.State.StartedAt}}' "${live}")"
}

[ "$(id -u)" = 0 ] || { echo "shadow-run.sh must run as root." >&2; exit 1; }
trap cleanup EXIT
cleanup
umask 077
mkdir -p "${scratch}/bell" "${scratch}/plan/ssh" "${scratch}/plan/acme"
before="$(live_marks)"

echo "== image"
docker pull -q "${image}" >/dev/null
docker image inspect "${image}" \
    --format '{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}'

echo "== the production database, copied"
prod_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "${live}")"
[ -n "${prod_volume}" ] || { echo "${live} mounts no /data volume." >&2; exit 1; }
docker volume create "${volume}" >/dev/null
docker volume create "${bridge}" >/dev/null
docker run --rm --user 0:0 --network none --entrypoint sh \
    --mount "type=volume,source=${volume},target=/data" "${image}" \
    -c "mkdir -p /data/tmp && chown -R ${uid}:${uid} /data"
# Read as HQ's account, so a shared-memory file SQLite makes beside the live
# database is one HQ owns.
docker run --rm --user "${uid}:${uid}" --network none --entrypoint python \
    --mount "type=volume,source=${prod_volume},target=/src" \
    --mount "type=volume,source=${volume},target=/data" "${image}" -c '
import sqlite3
source = sqlite3.connect("file:/src/severino.sqlite3?mode=ro", uri=True)
copy = sqlite3.connect("/data/severino.sqlite3")
source.backup(copy)
copy.close()
source.close()
'

echo "== the web process, on the copy, with no network"
chown "${uid}:${uid}" "${scratch}/bell"
docker run -d --name "${shadow}" --network none --cap-drop ALL --security-opt no-new-privileges:true \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m \
    --tmpfs "/media:uid=${uid},gid=${uid}" --tmpfs "/exports:uid=${uid},gid=${uid}" \
    --pids-limit 512 --memory 1g \
    --env TMPDIR=/data/tmp \
    --env SEVERINO_CONTROLLER_DOORBELL=/run/severino-hq/controller-doorbell \
    --env SEVERINO_BRIDGE_SOCKET=/run/hq-bridge/bridge.sock \
    --env SEVERINO_APP_ENV_PATH=/run/secrets/severino_hq_env \
    --mount "type=volume,source=${volume},target=/data" \
    --mount "type=volume,source=${bridge},target=/run/hq-bridge" \
    --mount "type=bind,source=${scratch}/bell,target=/run/severino-hq" \
    --mount "type=bind,source=${secret_dir}/web/severino_hq_env,target=/run/secrets/severino_hq_env,readonly" \
    "${image}" >/dev/null
for _ in $(seq 1 "${SHADOW_READY_TRIES:-60}"); do
    if docker exec "${shadow}" hq-controller ready http://127.0.0.1:8000/health/ready/ >/dev/null 2>&1; then
        ready=yes
        break
    fi
    [ "$(docker inspect --format '{{.State.Running}}' "${shadow}")" = true ] || break
    sleep 1
done
docker logs "${shadow}" 2>&1 | grep -E 'Applying|Traceback' | cut -c1-200 | tail -40 || true

if [ "${ready}" = yes ]; then
    echo "== the bridge socket"
    if docker exec "${shadow}" python -c '
import os, stat, sys
info = os.stat("/run/hq-bridge/bridge.sock")
sys.exit(0 if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() else 1)'; then
        socket=ok
    else
        socket=missing
    fi

    echo "== the copy's integrity"
    answer="$(docker exec "${shadow}" python -c '
import sqlite3
print(sqlite3.connect("file:/data/severino.sqlite3?mode=ro", uri=True).execute("pragma integrity_check").fetchone()[0])')" ||
        answer=unreadable
    echo "${answer}"
    if [ "${answer}" = ok ]; then integrity=ok; else integrity=damaged; fi

    echo "== scheduled work over the socket"
    jobs=ok
    for name in audit.prune sessions.clear; do
        if said="$(docker exec "${shadow}" hq-controller job "${name}")"; then
            printf '%s: %s\n' "${name}" "${said}" | cut -c1-200
        else
            echo "${name} failed: ${said}" | cut -c1-200
            jobs=failed
        fi
    done
    # A job HQ does not declare is refused, not run.
    if docker exec "${shadow}" hq-controller job no.such.job >/dev/null 2>&1; then jobs=failed; fi

    echo "== memory"
    docker stats --no-stream --format 'shadow {{.MemUsage}}' "${shadow}"
    docker stats --no-stream --format 'live   {{.MemUsage}}' "${live}"
    if [ "$(docker inspect --format '{{.State.OOMKilled}}' "${shadow}")" = false ]; then memory=ok; else memory=killed; fi

    echo "== the new controller, plan only"
    cp "${secret_dir}/controller-connections.json" "${scratch}/plan/connections.json"
    if [ -d "${secret_dir}/ssh" ]; then cp -R "${secret_dir}/ssh/." "${scratch}/plan/ssh/"; fi
    if [ -d "${acme_dir}" ]; then cp -R "${acme_dir}/." "${scratch}/plan/acme/"; fi
    chown -R "${uid}:${uid}" "${scratch}/plan"
    set --
    if op="$(command -v op)"; then set -- --mount "type=bind,source=${op},target=/usr/bin/op,readonly"; fi
    status=0
    docker run --rm --network host --user "${uid}:${uid}" --cap-drop ALL --security-opt no-new-privileges:true \
        --tmpfs /tmp:size=64m,noexec,nosuid,nodev --entrypoint /usr/local/bin/hq-controller \
        --mount "type=bind,source=${scratch}/plan/connections.json,target=/run/secrets/controller-connections.json,readonly" \
        --mount "type=bind,source=${scratch}/plan/ssh,target=/run/secrets/controller-ssh,readonly" \
        --mount "type=bind,source=${scratch}/plan/acme,target=/var/lib/severino-hq/acme" \
        --mount "type=volume,source=${bridge},target=/run/hq-bridge,readonly" \
        "$@" \
        --env HQ_CONTROLLER_CONNECTIONS=/run/secrets/controller-connections.json \
        --env HQ_CONTROLLER_SSH_DIR=/run/secrets/controller-ssh \
        --env HQ_ACME_DIR=/var/lib/severino-hq/acme \
        --env HQ_CONTROLLER_IMAGE="${image}" \
        --env SEVERINO_BRIDGE_SOCKET=/run/hq-bridge/bridge.sock \
        "${image}" >"${scratch}/plan.out" 2>"${scratch}/plan.err" || status=$?
    rm -rf "${scratch}/plan"
    cut -c1-260 "${scratch}/plan.err" | tail -15
    cut -c1-260 "${scratch}/plan.out" | tail -15
    if [ "${status}" -eq 0 ]; then plan=ok; else plan="exit-${status}"; fi
else
    echo "== the shadow did not become ready"
    docker logs "${shadow}" 2>&1 | tail -40 | cut -c1-300 || true
fi

echo "== live state"
after="$(live_marks)"
if [ "${after}" = "${before}" ]; then
    live_state=untouched
else
    echo "The live web restarted: ${before} -> ${after}" >&2
    live_state=restarted
fi
verdict
