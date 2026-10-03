#!/bin/sh
# upgrade-container.sh: move one compose service to an image pinned by digest,
# the way HQ's upgrade plan describes it, and put everything back if the new
# image does not prove itself.
#
#   upgrade-container.sh --operation ID --project-dir STACKS_ROOT/NAME \
#       --service NAME --from IMAGE --to REPOSITORY@sha256:DIGEST \
#       [--tag TAG] [--wait SECONDS]
#
# Run as root through one sudo rule, by the controller on the machine the
# service runs on, for an operation HQ queued and a person approved. The caller
# is not trusted: everything that decides what root does is read from the
# machine, not from the arguments.
#
#   - The stack is a directory directly under STACKS_ROOT (/opt/apps), not a
#     symlink, and it, its compose file, any override and any .env are owned by
#     root or by the owner of STACKS_ROOT, never by the account that called
#     sudo, and are writable by nobody else. Compose runs with the file set it
#     would use by default and `-p` named from that directory.
#   - --from must be the image compose resolves for the service, and what its one
#     running container runs. --to must be the same repository, by digest.
#   - The data is the running container's writable mounts, from Docker.
#
# The steps:
#   0. check what runs, and that the override takes the pin and compose reads
#      it from there, before anything is stopped;
#   1. pull the target by digest;
#   2. stop the service and snapshot its data (so the snapshot is consistent);
#   3. run the target from the service's own compose definition, as
#      `compose run --no-deps`, with no network and no published ports,
#      against a copy of the snapshot, until it proves itself;
#   4. pin the digest in the stack's compose override, keeping everything else
#      in it and a byte-exact copy of it; the compose file itself is never
#      edited;
#   5. recreate that one service and verify it the way the trial was verified;
#   6. keep it, or restore the override and the data and recreate again.
#
# The service is down from step 2 until step 5, or until it is started again
# unchanged when the trial fails. An error or a signal at any point after
# step 2 puts it back the same way, and still records a result.
#
# Idempotent by operation: the result is kept under the state directory and
# asking again for the same operation prints it instead of running twice.
#
# Needs Docker Compose 2.24.4 or later, for `!reset` in the trial's overlay.
#
# Prints one JSON object. Exits 0 when the upgrade was kept, 3 when it was
# rolled back, 2 when it was refused or stopped before anything changed, 1 when
# it failed part way and could not be put back.
set -eu

# Fixed for anyone reaching this through sudo, which passes none of the
# variables below (env_reset, and the rule grants no SETENV). Outside sudo, as
# root already or in the drill, they can be moved.
STACKS_ROOT=/opt/apps
STATE_ROOT=/var/lib/severino-hq/upgrades
POLL=2
# How long a container must keep running, without a restart, to count.
SETTLE=10
if [ -n "${SUDO_UID:-}" ]; then
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    # The docker CLI finds plugins and credential helpers under $HOME and
    # $DOCKER_CONFIG: a sudo that kept the caller's would run the caller's
    # binaries as root. Root's own, whatever sudo passed.
    HOME=/root
    export PATH HOME
    unset DOCKER_CONFIG DOCKER_HOST DOCKER_CONTEXT DOCKER_CERT_PATH DOCKER_TLS_VERIFY
else
    STACKS_ROOT="${SEVERINO_UPGRADE_STACKS:-${STACKS_ROOT}}"
    STATE_ROOT="${SEVERINO_UPGRADE_STATE:-${STATE_ROOT}}"
    POLL="${SEVERINO_UPGRADE_POLL:-${POLL}}"
    SETTLE="${SEVERINO_UPGRADE_SETTLE:-${SETTLE}}"
fi
LC_ALL=C
export LC_ALL

operation="" project_dir="" service="" from="" to="" tag="" wait=120
refuse() { printf '{"outcome":"refused","reason":"%s"}\n' "$1"; exit 2; }

while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || refuse "an argument has no value"
    case "$1" in
        --operation) operation="$2" ;;
        --project-dir) project_dir="$2" ;;
        --service) service="$2" ;;
        --from) from="$2" ;;
        --to) to="$2" ;;
        --tag) tag="$2" ;;
        --wait) wait="$2" ;;
        --data) refuse "data is read from the running service, not given" ;;
        *) refuse "unknown argument" ;;
    esac
    shift 2
done

# One line only: grep matches per line, so a value carrying a newline would
# pass on its first line alone.
newline='
'
matches() {
    case "$1" in *"${newline}"*) return 1 ;; esac
    printf '%s' "$1" | grep -Eq "$2"
}
matches "${operation}" '^[A-Za-z0-9-]{1,64}$' || refuse "operation is not an id"
matches "${service}" '^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$' || refuse "service is not a name"
matches "${from}" '^[A-Za-z0-9][A-Za-z0-9_./:@-]{0,254}$' || refuse "from is not an image reference"
matches "${to}" '^[A-Za-z0-9][A-Za-z0-9_./:-]{0,200}@sha256:[0-9a-f]{64}$' || refuse "to is not pinned by digest"
[ -z "${tag}" ] || matches "${tag}" '^[A-Za-z0-9_.-]{1,128}$' || refuse "tag is not a tag"
matches "${wait}" '^[0-9]{1,4}$' || refuse "wait is not a number of seconds"
{ matches "${POLL}" '^[0-9]{1,3}$' && matches "${SETTLE}" '^[0-9]{1,4}$'; } || refuse "poll and settle are not numbers"

# A reference's repository: without its digest, and without a tag after its
# last slash (a registry's port is not a tag).
repository() {
    ref="${1%%@*}"
    case "${ref##*/}" in *:*) ref="${ref%:*}" ;; esac
    printf '%s' "${ref}"
}
[ "$(repository "${to}")" = "$(repository "${from}")" ] || refuse "to is not the repository from names"

# The stack: a real directory directly under the stacks root.
matches "${STACKS_ROOT}" '^/[A-Za-z0-9_./-]+$' || refuse "the stacks root is not an absolute path"
case "${STACKS_ROOT} ${project_dir}" in *..* | *//* | */\ * | */) refuse "a path climbs out or is not canonical" ;; esac
matches "${project_dir}" '^/[A-Za-z0-9_./-]+$' || refuse "project directory is not an absolute path"
[ "${project_dir%/*}" = "${STACKS_ROOT}" ] || refuse "project directory is not directly under ${STACKS_ROOT}"
name="${project_dir##*/}"
matches "${name}" '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' || refuse "project directory is not a stack name"
project="$(printf '%s' "${name}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
matches "${project}" '^[a-z0-9][a-z0-9_-]*$' || refuse "no compose project name follows from the directory"
[ "$(id -u)" -eq 0 ] || refuse "must run as root"

# Ownership, read the same way on every system: `ls -ldn` prints the mode and
# the numeric owner on Linux and on BSD alike, where stat's flags differ. Every
# path here matched a pattern without spaces first.
# shellcheck disable=SC2012
owner_of() { ls -ldn -- "$1" | awk '{print $3}'; }
# shellcheck disable=SC2012
# A "+" after the mode is an ACL, which can grant writes the mode bits do not show.
others_write() { ls -ldn -- "$1" | cut -c6,9,11 | grep -q '[w+]'; }
{ [ -d "${STACKS_ROOT}" ] && [ ! -L "${STACKS_ROOT}" ]; } || refuse "the stacks root is not a directory"
trusted="$(owner_of "${STACKS_ROOT}")"
caller="${SUDO_UID:-}"
[ "${caller}" = 0 ] && caller=""
[ "${trusted}" != "${caller}" ] || refuse "the stacks root belongs to the account asking"
! others_write "${STACKS_ROOT}" || refuse "the stacks root is writable by its group or others"
# Root's own, or the stacks root owner's; never the caller's; writable by no one else.
guarded() { # guarded <path> <what> <directory|file>
    [ ! -L "$1" ] || refuse "$2 is a symlink"
    case "$3" in
        directory) [ -d "$1" ] ;;
        *) [ -f "$1" ] ;;
    esac || refuse "$2 is not a $3"
    owner="$(owner_of "$1")"
    { [ "${owner}" = 0 ] || [ "${owner}" = "${trusted}" ]; } && [ "${owner}" != "${caller}" ] \
        || refuse "$2 is not owned by root or the owner of ${STACKS_ROOT}"
    ! others_write "$1" || refuse "$2 is writable by its group or others"
}
[ -e "${project_dir}" ] || [ -L "${project_dir}" ] || refuse "project directory does not exist"
guarded "${project_dir}" "the project directory" directory

# The file set compose uses by default: the first compose file, and the override.
compose_file=""
for candidate in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do
    if [ -e "${project_dir}/${candidate}" ] || [ -L "${project_dir}/${candidate}" ]; then
        compose_file="${project_dir}/${candidate}"
        break
    fi
done
[ -n "${compose_file}" ] || refuse "no compose file in the project directory"
guarded "${compose_file}" "the compose file" file
# Compose loads only the override named after its compose file
# (compose.yaml → compose.override.yaml). A pin in any other would hold for the
# helper's own calls and vanish on the next plain `docker compose up`.
base="${compose_file##*/}"
loaded="${project_dir}/${base%.*}.override.${base##*.}"
for candidate in compose.override.yaml compose.override.yml docker-compose.override.yaml docker-compose.override.yml; do
    if [ "${project_dir}/${candidate}" != "${loaded}" ] && { [ -e "${project_dir}/${candidate}" ] || [ -L "${project_dir}/${candidate}" ]; }; then
        refuse "${candidate} is not the override compose loads beside ${base}"
    fi
done
override=""
if [ -e "${loaded}" ] || [ -L "${loaded}" ]; then
    override="${loaded}"
    guarded "${override}" "the compose override" file
fi
pin_file="${loaded}"
if [ -e "${project_dir}/.env" ] || [ -L "${project_dir}/.env" ]; then
    guarded "${project_dir}/.env" "the stack's .env" file
fi

matches "${STATE_ROOT}" '^/[A-Za-z0-9_./-]+$' || refuse "the state directory is not an absolute path"
state="${STATE_ROOT}/${operation}"
if [ -f "${state}/result.json" ]; then
    cat "${state}/result.json"
    exit "$(cat "${state}/exit" 2>/dev/null || echo 1)"
fi
umask 077
mkdir -p "${STATE_ROOT}"
mkdir "${state}" 2>/dev/null || refuse "the operation is running, or stopped without a result"

trial="hq-trial-${operation}"
steps=""
phase=checking
finished=""
step() { steps="${steps}${steps:+,}{\"step\":\"$1\",\"ok\":$2,\"detail\":\"$3\"}"; }

# docker compose with the stack's file set, and a trial's on top when set.
trial_file=""
compose() {
    [ -z "${trial_file}" ] || set -- -f "${trial_file}" "$@"
    [ -z "${override}" ] || set -- -f "${override}" "$@"
    docker compose -p "${project}" --project-directory "${project_dir}" -f "${compose_file}" "$@"
}

# Each data line is "INDEX KIND REF DESTINATION"; loops read it on fd 3 so
# nothing they run can consume it.
cleanup_trial() {
    docker rm -f "${trial}" >/dev/null 2>&1 || true
    if [ -f "${state}/data" ]; then
        while read -r index _kind _ref _destination <&3; do
            docker volume rm -f "${trial}-${index}" >/dev/null 2>&1 || true
        done 3<"${state}/data"
    fi
    trial_file=""
}

finish() { # finish <outcome> <exit>
    finished=1
    trap - EXIT HUP INT TERM
    set +e
    cleanup_trial
    json="$(printf '{"operation":"%s","outcome":"%s","service":"%s","image":"%s","steps":[%s]}' \
        "${operation}" "$1" "${service}" "${to}" "${steps}")"
    if ! { echo "$2" >"${state}/exit" && printf '%s\n' "${json}" >"${state}/result.part" \
        && mv "${state}/result.part" "${state}/result.json"; } 2>/dev/null; then
        echo "upgrade-container: the result could not be recorded in ${state}" >&2
    fi
    printf '%s\n' "${json}"
    exit "$2"
}

# The image compose resolves for the service. `config --format json` indents
# two spaces a level, so the service's own image is at six.
service_image() {
    compose config --format json 2>/dev/null | awk -v service="${service}" '
        /^  "services": [{]/ { within = 1; next }
        within && /^  [}]/ { within = 0 }
        within && $0 == "    \"" service "\": {" { found = 1; next }
        found && /^    [}]/ { exit }
        found && /^      "image": "/ { sub(/^      "image": "/, ""); sub(/",?$/, ""); print; exit }
    '
}

# The image compose gives the service with $1 standing in for the override.
pinned_image() (
    override="$1"
    service_image
)

# Running, healthy or with no health check, and not restarted, for SETTLE
# seconds across at least two readings, within the wait. A restart starts the
# window again, so a crash loop never finishes it.
proves() {
    deadline=$(($(date +%s) + wait))
    since="" last=""
    while :; do
        seen="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}} {{.State.StartedAt}}' "$1" 2>/dev/null)" || seen="gone gone"
        now="$(date +%s)"
        run="${seen#* }"
        run="${run#* }"
        case "${seen}" in
            "running healthy "* | "running none "*)
                if [ -n "${since}" ] && [ "${run}" = "${last}" ]; then
                    [ $((now - since)) -lt "${SETTLE}" ] || return 0
                else
                    since="${now}" last="${run}"
                fi
                ;;
            "running starting "* | restarting* | created*) since="" last="" ;;
            *) return 1 ;;
        esac
        [ "${now}" -lt "${deadline}" ] || return 1
        sleep "${POLL}"
    done
}

# The one container compose runs for the service; more than one is refused.
only_container() {
    ids="$(compose ps -q "${service}" 2>/dev/null)" || return 1
    [ "$(printf '%s\n' "${ids}" | grep -c .)" = 1 ] || return 1
    matches "${ids}" '^[A-Za-z0-9_.-]{1,128}$' || return 1
    printf '%s' "${ids}"
}

# Where one data line lives on this machine.
data_path() { # data_path <kind> <ref>
    case "$1" in
        volume) docker volume inspect -f '{{.Mountpoint}}' "$2" ;;
        bind | file) printf '%s' "$2" ;;
        *) return 1 ;;
    esac
}

# Whether a path resolves to itself: no symlink anywhere along it, so what root
# is about to read or replace is the path that was checked, not one another
# writer in the stack swapped in since. Asked when the mounts are read and
# again right before each snapshot and each restore.
unmoved() { # unmoved <path>
    case "$1" in /*/ | "" | /) return 1 ;; esac
    parent="${1%/*}"
    real="$(CDPATH='' cd -P -- "${parent:-/}" 2>/dev/null && pwd -P)" || return 1
    [ "${real%/}/${1##*/}" = "$1" ] && [ ! -L "$1" ]
}

# The container's writable mounts as data lines: volumes, and directories and
# files inside the stack. A socket is a channel, not data. Anything writable
# outside the stack is refused: nothing here may snapshot or restore it.
read_data() { # read_data <container>
    : >"${state}/data"
    docker inspect -f '{{range .Mounts}}{{.Type}} {{if .Name}}{{.Name}}{{else}}-{{end}} {{.Source}} {{.Destination}} {{.RW}}{{"\n"}}{{end}}' "$1" >"${state}/mounts"
    count=0
    while read -r kind volume source destination writable <&3; do
        [ -n "${kind}" ] && [ "${writable}" = true ] || continue
        matches "${destination}" '^/[A-Za-z0-9_./@+-]*$' || { step check false "a mount has an unreadable destination"; return 1; }
        case "${kind}" in
            volume)
                matches "${volume}" '^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$' || { step check false "a volume has an unreadable name"; return 1; }
                echo "${count} volume ${volume} ${destination}" >>"${state}/data"
                ;;
            bind)
                matches "${source}" '^/[A-Za-z0-9_./@+-]*$' || { step check false "a mount has an unreadable source"; return 1; }
                unmoved "${source}" || { step check false "a mount at ${destination} is reached through a symlink"; return 1; }
                if [ -f "${source}" ]; then
                    shape="file"
                elif [ -d "${source}" ]; then
                    shape=bind
                else
                    continue
                fi
                case "${source}/" in
                    *..*) step check false "a mount source climbs out"; return 1 ;;
                    "${project_dir}"/?*) echo "${count} ${shape} ${source} ${destination}" >>"${state}/data" ;;
                    *) step check false "a writable path outside the stack is mounted at ${destination}"; return 1 ;;
                esac
                ;;
            tmpfs) continue ;;
            *) step check false "a mount of an unknown kind"; return 1 ;;
        esac
        count=$((count + 1))
    done 3<"${state}/mounts"
}

# Put the data back from its snapshots, reporting a full disk as such.
restore_data() {
    while read -r index kind ref _destination <&3; do
        target="$(data_path "${kind}" "${ref}")" || target=""
        case "${target}" in
            "" | /) step restore false "data-${index} has no mountpoint"; return 1 ;;
        esac
        if [ "${kind}" != volume ] && ! unmoved "${target}"; then
            step restore false "data-${index} could not be restored: its path now leads through a symlink; its snapshot is kept in the state directory"
            return 1
        fi
        if [ "${kind}" = file ]; then
            put_back() { cp -p "${state}/data-${index}.file" "${target}"; }
        else
            find "${target}" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null
            put_back() { tar -C "${target}" -xzf "${state}/data-${index}.tgz"; }
        fi
        if ! put_back 2>"${state}/restore.err"; then
            if grep -qi "no space left" "${state}/restore.err"; then
                step restore false "the disk filled restoring data-${index}; its snapshot is kept in the state directory"
            else
                step restore false "data-${index} could not be restored; its snapshot is kept in the state directory"
            fi
            return 1
        fi
    done 3<"${state}/data"
    rm -f "${state}/restore.err"
    step restore true "the data is back"
}

# The override with the service's image set to the pin: replaced where the
# service names one, added where it does not, with the service or services:
# added when missing. Every other line is kept as it is. Block style only.
edit_override() {
    awk -v service="${service}" -v image="${to}" '
        function pad(width,  text) { text = ""; while (width-- > 0) text = text " "; return text }
        function indent(line) { match(line, /^ */); return RLENGTH }
        function names(line, name) { return line ~ ("^ *\"?" name "\"?:") }
        function bare(line, name) { return line ~ ("^ *\"?" name "\"?:[ ]*(#.*)?$") }
        function emit_image(width) { print pad(width) "image: " image; done = 1 }
        function emit_service(width) { print pad(width) "\"" service "\":"; emit_image(width * 2) }
        # The name as a pattern: a dot in it is a dot.
        BEGIN { literal = service; gsub(/[.]/, "[.]", literal) }
        done || /^[ ]*(#.*)?$/ { print; next }
        # A document written as one flow mapping, JSON among them, has no
        # line this can add to.
        !in_services && /^[ ]*[{[]/ { bad = 1; exit }
        !in_services {
            if (indent($0) == 0 && names($0, "services")) {
                if (!bare($0, "services")) { bad = 1; exit }
                in_services = 1
            }
            print; next
        }
        {
            width = indent($0)
            if (width == 0) {
                if (in_service) emit_image(child_width ? child_width : service_width * 2)
                else emit_service(service_width ? service_width : 2)
                print; next
            }
            if (!service_width) service_width = width
            if (!in_service) {
                if (width == service_width && names($0, literal)) {
                    if (!bare($0, literal)) { bad = 1; exit }
                    in_service = 1
                }
                print; next
            }
            if (!child_width) {
                if (width <= service_width) { emit_image(service_width * 2); print; next }
                child_width = width
            }
            if (width < child_width) { emit_image(child_width); print; next }
            if (width == child_width && $0 ~ /^ *image:/) { emit_image(child_width); next }
            print
        }
        END {
            if (bad) exit 3
            if (done) exit 0
            if (!in_services) { print "services:"; emit_service(2) }
            else if (!in_service) emit_service(service_width ? service_width : 2)
            else emit_image(child_width ? child_width : service_width * 2)
        }
    '
}

# Undo the pin: the override exactly as it was, then the data, then recreate.
roll_back() {
    set +e
    if [ -f "${state}/override.before" ]; then
        cp -p "${state}/override.before" "${pin_file}.hq-restore" && mv "${pin_file}.hq-restore" "${pin_file}"
    else
        rm -f "${pin_file}" && override=""
    fi || { step rollback false "the compose override could not be restored"; finish failed 1; }
    compose stop "${service}" >/dev/null 2>&1
    restore_data || finish failed 1
    if compose up -d --no-deps "${service}" >/dev/null 2>&1; then
        step rollback true "the previous image and data are back"
        finish rolled_back 3
    fi
    step rollback false "the previous image could not be started again"
    finish failed 1
}

# Start the stopped service again as it was, having changed nothing.
unchanged() {
    set +e
    cleanup_trial
    if compose start "${service}" >/dev/null 2>&1; then
        step restart true "the service is running again, unchanged"
        finish unchanged 2
    fi
    step restart false "the service could not be started again"
    finish failed 1
}

# Any error or signal from here on still puts things back and records a result.
# shellcheck disable=SC2329  # invoked by the trap below
on_exit() {
    [ -z "${finished}" ] || return 0
    # Ignored, not reset: a second signal must not stop a restore half done.
    trap - EXIT
    trap '' HUP INT TERM
    case "${phase}" in
        checking) step stopped false "stopped before anything changed"; finish refused 2 ;;
        stopped) step stopped false "stopped part way, before the pin"; unchanged ;;
        *) step stopped false "stopped part way, after the pin"; roll_back ;;
    esac
}
trap on_exit EXIT
trap 'exit 143' HUP INT TERM

# 0. What runs today, from the machine.
[ "$(service_image)" = "${from}" ] || { step check false "compose does not resolve ${service} to ${from}"; finish refused 2; }
live="$(only_container)" || { step check false "${service} is not exactly one running container"; finish refused 2; }
[ "$(docker inspect -f '{{.Config.Image}}' "${live}")" = "${from}" ] \
    || { step check false "the running container is not ${from}"; finish refused 2; }
read_data "${live}" || finish refused 2
# The pin is written and proved here, while the service still runs: an
# override this cannot edit, or one compose does not read the pin from, is
# refused with nothing stopped.
pinned="${state}/override.pinned"
if [ -n "${override}" ]; then
    cp -p "${override}" "${pinned}"
    edit_override <"${override}" >"${pinned}" || pinned=""
else
    : >"${pinned}"
    chmod 0644 "${pinned}"
    edit_override </dev/null >"${pinned}" || pinned=""
fi
[ -n "${pinned}" ] \
    || { step check false "the compose override is not block style YAML this can edit; nothing changed"; finish refused 2; }
[ "$(pinned_image "${pinned}")" = "${to}" ] \
    || { step check false "compose would not resolve the pin from the override; nothing changed"; finish refused 2; }
step check true "${service} runs ${from}, with ${count} data mount(s)"

# 1. Pull by digest.
docker pull -q "${to}" >/dev/null || { step pull false "${to}"; finish refused 2; }
step pull true "${to}"

# 2. Stop, and snapshot each mount to a temporary name, renamed only when whole.
phase=stopped
compose stop "${service}" >/dev/null || { step stop false "the service did not stop"; unchanged; }
while read -r index kind ref _destination <&3; do
    source_path="$(data_path "${kind}" "${ref}")" || source_path=""
    if [ "${kind}" = file ]; then
        suffix="file"
        take() { cp -p "${source_path}" "${state}/data-${index}.part"; }
    else
        suffix=tgz
        take() { tar -C "${source_path}" -czf "${state}/data-${index}.part" .; }
    fi
    if [ -z "${source_path}" ] || [ "${source_path}" = / ] \
        || { [ "${kind}" != volume ] && ! unmoved "${source_path}"; } \
        || ! take 2>/dev/null \
        || ! mv "${state}/data-${index}.part" "${state}/data-${index}.${suffix}"; then
        rm -f "${state}/data-${index}.part"
        step snapshot false "data-${index} could not be snapshotted; nothing changed"
        unchanged
    fi
done 3<"${state}/data"
step snapshot true "${count} data mount(s)"

# 3. Trial: the service's own definition, the target image, copies of the
#    data, no network and no published ports.
trial_file="${state}/trial.yaml"
{
    printf 'services:\n  "%s":\n    image: "%s"\n    network_mode: none\n' "${service}" "${to}"
    printf '    networks: !reset {}\n    ports: !reset []\n    volumes:\n'
    while read -r index kind _ref destination <&3; do
        if [ "${kind}" = file ]; then
            # Its own copy, so the trial never writes the live file.
            printf '      - type: bind\n        source: "%s/trial-%s.file"\n        target: "%s"\n' "${state}" "${index}" "${destination}"
        else
            printf '      - type: volume\n        source: hq_trial_%s\n        target: "%s"\n' "${index}" "${destination}"
        fi
    done 3<"${state}/data"
    if grep -qv '^[0-9]* file ' "${state}/data"; then
        printf 'volumes:\n'
        while read -r index kind _ref _destination <&3; do
            [ "${kind}" = file ] && continue
            printf '  hq_trial_%s:\n    external: true\n    name: "%s-%s"\n' "${index}" "${trial}" "${index}"
        done 3<"${state}/data"
    fi
} >"${trial_file}"
while read -r index kind _ref _destination <&3; do
    if [ "${kind}" = file ]; then
        cp -p "${state}/data-${index}.file" "${state}/trial-${index}.file" || {
            step trial false "data-${index} could not be copied for the trial"
            unchanged
        }
        continue
    fi
    if ! docker volume create "${trial}-${index}" >/dev/null \
        || ! copy="$(data_path volume "${trial}-${index}")" || [ -z "${copy}" ] \
        || ! tar -C "${copy}" -xzf "${state}/data-${index}.tgz"; then
        step trial false "data-${index} could not be copied for the trial"
        unchanged
    fi
done 3<"${state}/data"
if ! compose run -d --no-deps --name "${trial}" "${service}" >/dev/null 2>&1 || ! proves "${trial}"; then
    step trial false "the target did not prove itself as the service would run it"
    unchanged
fi
step trial true "proven on a copy of the data, with no network"
cleanup_trial

# 4. Pin in the override, keeping a byte-exact copy and every other line.
[ -z "${override}" ] || cp -p "${override}" "${state}/override.before"
cp -p "${pinned}" "${pin_file}.hq-pin"
phase=pinned
mv "${pin_file}.hq-pin" "${pin_file}"
override="${pin_file}"
[ "$(service_image)" = "${to}" ] || { step pin false "compose does not resolve the pin"; roll_back; }
step pin true "${pin_file##*/}${tag:+, ${tag}}"

# 5. Recreate, and verify.
if compose up -d --no-deps "${service}" >/dev/null 2>&1 && upgraded="$(only_container)" && proves "${upgraded}"; then
    step verify true "running and proven"
    finish kept 0
fi
step verify false "the upgraded service did not prove itself"

# 6. Roll back.
roll_back
