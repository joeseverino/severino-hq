#!/bin/sh
# upgrade-container.sh: move one compose service to an image pinned by digest,
# the way HQ's upgrade plan describes it, and put everything back if the new
# image does not prove itself.
#
#   upgrade-container.sh --operation ID --project-dir DIR --service NAME \
#       --from IMAGE --to REPO@sha256:DIGEST [--tag TAG] \
#       [--data VOLUME:DESTINATION]... [--wait SECONDS]
#
# Run as root through one sudo rule, by the controller on the machine the
# service runs on, for an operation HQ queued and a person approved:
#   1. snapshot each named data volume (a tar of its mountpoint);
#   2. pull the target by digest;
#   3. run it beside the live one, on an internal network with no route out,
#      against a copy of its data, with the live one's environment, until it
#      reports healthy (or keeps running, when it has no health check);
#   4. pin the digest in the compose file, replacing exactly the image line
#      the service runs today, with the tag kept as a comment;
#   5. recreate that one service;
#   6. verify it the way the trial was verified;
#   7. keep it, or restore the compose file and the data and recreate again.
#
# Nothing on the live service changes before the trial passes. Idempotent by
# operation: the result is kept under the state directory and asking again for
# the same operation prints it instead of running twice.
#
# Prints one JSON object. Exits 0 when the upgrade was kept, 3 when it was
# rolled back, 2 when the request was refused before anything changed, 1 when
# it failed part way and could not be rolled back.
set -eu

STATE_ROOT="${SEVERINO_UPGRADE_STATE:-/var/lib/severino-hq/upgrades}"
POLL="${SEVERINO_UPGRADE_POLL:-2}"
# How long a container with no health check must keep running to count.
SETTLE="${SEVERINO_UPGRADE_SETTLE:-10}"

operation="" project_dir="" service="" from="" to="" tag="" data="" wait=120
refuse() { printf '{"outcome":"refused","reason":"%s"}\n' "$1"; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --operation) operation="${2:-}"; shift 2 ;;
        --project-dir) project_dir="${2:-}"; shift 2 ;;
        --service) service="${2:-}"; shift 2 ;;
        --from) from="${2:-}"; shift 2 ;;
        --to) to="${2:-}"; shift 2 ;;
        --tag) tag="${2:-}"; shift 2 ;;
        --data) data="${data} ${2:-}"; shift 2 ;;
        --wait) wait="${2:-}"; shift 2 ;;
        *) refuse "unknown argument" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || refuse "must run as root"
matches() { printf '%s' "$1" | grep -Eq "$2"; }
matches "${operation}" '^[A-Za-z0-9-]{1,64}$' || refuse "operation is not an id"
matches "${service}" '^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$' || refuse "service is not a name"
matches "${project_dir}" '^/[A-Za-z0-9_./-]+$' || refuse "project directory is not an absolute path"
case "${project_dir}" in *..*) refuse "project directory climbs out" ;; esac
matches "${from}" '^[A-Za-z0-9][A-Za-z0-9_./:@-]{0,254}$' || refuse "from is not an image reference"
matches "${to}" '^[A-Za-z0-9][A-Za-z0-9_./:-]{0,200}@sha256:[0-9a-f]{64}$' || refuse "to is not pinned by digest"
[ -z "${tag}" ] || matches "${tag}" '^[A-Za-z0-9_.-]{1,128}$' || refuse "tag is not a tag"
matches "${wait}" '^[0-9]{1,4}$' || refuse "wait is not a number of seconds"
for item in ${data}; do
    matches "${item}" '^[A-Za-z0-9][A-Za-z0-9_.-]*:/[A-Za-z0-9_./-]*$' || refuse "data is not VOLUME:/destination"
done

compose_file=""
for name in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do
    [ -f "${project_dir}/${name}" ] && { compose_file="${project_dir}/${name}"; break; }
done
[ -n "${compose_file}" ] || refuse "no compose file in the project directory"

state="${STATE_ROOT}/${operation}"
if [ -f "${state}/result.json" ]; then
    cat "${state}/result.json"
    exit "$(cat "${state}/exit" 2>/dev/null || echo 1)"
fi

# The image line the service runs today, which must be the only one naming it.
from_pattern="$(printf '%s' "${from}" | sed 's/[.[\*^$/]/\\&/g')"
image_line="^\\([[:space:]]*image:[[:space:]]*\\)[\"']\\{0,1\\}${from_pattern}[\"']\\{0,1\\}[[:space:]]*\$"
count="$(grep -c "${image_line}" "${compose_file}" || true)"
[ "${count}" = 1 ] || refuse "the compose file names ${from} ${count} times, not once"

umask 077
mkdir -p "${state}"
trial="hq-trial-${operation}"
steps=""
step() { steps="${steps}${steps:+,}{\"step\":\"$1\",\"ok\":$2,\"detail\":\"$3\"}"; }
compose() { docker compose --project-directory "${project_dir}" -f "${compose_file}" "$@"; }

cleanup() {
    docker rm -f "${trial}" >/dev/null 2>&1 || true
    for item in ${data}; do docker volume rm -f "${trial}-${item%%:*}" >/dev/null 2>&1 || true; done
    docker network rm "${trial}" >/dev/null 2>&1 || true
    rm -f "${state}/trial.env"
}
trap cleanup EXIT HUP INT TERM

finish() {
    printf '{"operation":"%s","outcome":"%s","service":"%s","image":"%s","steps":[%s]}\n' \
        "${operation}" "$1" "${service}" "${to}" "${steps}" >"${state}/result.json"
    echo "$2" >"${state}/exit"
    cat "${state}/result.json"
    exit "$2"
}

# Healthy, or running for SETTLE seconds with no health check, within the wait.
proves() {
    deadline=$(($(date +%s) + wait))
    since=""
    while :; do
        seen="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || echo "gone gone")"
        case "${seen}" in
            "running healthy") return 0 ;;
            "running none")
                since="${since:-$(date +%s)}"
                [ $(($(date +%s) - since)) -ge "${SETTLE}" ] && return 0 ;;
            *" unhealthy" | exited* | dead* | gone*) return 1 ;;
        esac
        [ "$(date +%s)" -lt "${deadline}" ] || return 1
        sleep "${POLL}"
    done
}

mountpoint() { docker volume inspect -f '{{.Mountpoint}}' "$1"; }

# 1. Snapshot.
for item in ${data}; do
    volume="${item%%:*}"
    tar -C "$(mountpoint "${volume}")" -czf "${state}/${volume}.tgz" . || { step snapshot false "${volume}"; finish failed 1; }
done
step snapshot true "${data:- nothing to snapshot}"

# 2. Pull by digest.
docker pull -q "${to}" >/dev/null || { step pull false "${to}"; finish refused 2; }
step pull true "${to}"

# 3. Trial beside the live one.
live="$(compose ps -q "${service}")"
[ -n "${live}" ] || { step trial false "${service} is not running"; finish refused 2; }
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "${live}" >"${state}/trial.env"
docker network create --internal "${trial}" >/dev/null
mounts=""
for item in ${data}; do
    volume="${item%%:*}"
    docker volume create "${trial}-${volume}" >/dev/null
    tar -C "$(mountpoint "${trial}-${volume}")" -xzf "${state}/${volume}.tgz"
    mounts="${mounts} -v ${trial}-${volume}:${item#*:}"
done
# shellcheck disable=SC2086 # the mounts are words by construction
docker run -d --name "${trial}" --network "${trial}" --env-file "${state}/trial.env" ${mounts} "${to}" >/dev/null
if ! proves "${trial}"; then
    step trial false "the target did not prove itself beside the live one"
    finish refused 2
fi
step trial true "healthy beside the live one"
cleanup

# 4. Pin, keeping the file it replaces.
cp -p "${compose_file}" "${state}/compose.before"
pinned="${to}${tag:+ # ${tag}}"
sed -i "s|${image_line}|\\1${pinned}|" "${compose_file}"
step pin true "${compose_file##*/}"

# 5 and 6. Recreate, and verify.
if compose up -d --no-deps "${service}" >/dev/null 2>&1 && proves "$(compose ps -q "${service}")"; then
    step verify true "running and proven"
    finish kept 0
fi
step verify false "the upgraded service did not prove itself"

# 7. Roll back: the compose file, then the data, then recreate.
cp -p "${state}/compose.before" "${compose_file}"
compose stop "${service}" >/dev/null 2>&1 || true
for item in ${data}; do
    volume="${item%%:*}"
    target="$(mountpoint "${volume}")"
    case "${target}" in "" | /) step rollback false "${volume} has no mountpoint"; finish failed 1 ;; esac
    find "${target}" -mindepth 1 -delete
    tar -C "${target}" -xzf "${state}/${volume}.tgz"
done
if compose up -d --no-deps "${service}" >/dev/null 2>&1; then
    step rollback true "the previous image and data are back"
    finish rolled_back 3
fi
step rollback false "the previous image could not be started again"
finish failed 1
