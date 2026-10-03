#!/bin/sh
# The upgrade helper against a stand-in Docker and a stand-in tar: kept when
# the target proves itself, unchanged when the trial or the snapshot fails,
# rolled back (override and data) when the upgraded service does not prove
# itself or the run is stopped part way, reported when the rollback itself
# fails, never twice for one operation, and refused before anything changes
# whenever the caller asks for something outside the stack it names.
#
# Portable on purpose: it runs under macOS's bash 3.2 as sh and under dash.

set -eu
unset SUDO_UID SUDO_USER SUDO_COMMAND
umask 022

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
# Its physical path: the helper refuses a data path reached through a symlink,
# and on a Mac the temp directory itself sits behind one (/var -> /private/var).
fixture="$(cd "$(mktemp -d)" && pwd -P)"
readonly fixture
trap 'rm -rf "${fixture}"' EXIT HUP INT TERM
real_tar="$(command -v tar)"

failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }
has() { grep -q -- "$1" "$2" 2>/dev/null; }

mkdir -p "${fixture}/bin"
cat >"${fixture}/bin/id" <<'STUB'
#!/bin/sh
echo "${STUB_UID:-0}"
STUB
cat >"${fixture}/bin/tar" <<STUB
#!/bin/sh
echo "tar \$*" >>"\${FIXTURE}/calls"
case "\$*" in
    *-czf*) [ -z "\${STUB_TAR_SNAPSHOT_FAIL:-}" ] || exit 1 ;;
    *hq-trial-*) ;;
    *-xzf*)
        if [ -n "\${STUB_TAR_FULL:-}" ]; then
            echo "tar: state: Cannot write: No space left on device" >&2
            exit 2
        fi
        ;;
esac
exec "${real_tar}" "\$@"
STUB
cat >"${fixture}/bin/docker" <<'STUB'
#!/bin/sh
# Docker as the helper uses it; every call is logged, one per line.
echo "$*" >>"${FIXTURE}/calls"
bump() { count="$(cat "${FIXTURE}/$1" 2>/dev/null || echo 0)"; echo $((count + 1)) >"${FIXTURE}/$1"; echo "${count}"; }
if [ "$1" = compose ]; then
    shift
    files=""
    while :; do
        case "$1" in
            -p | --project-directory) shift 2 ;;
            -f) files="${files} $2"; shift 2 ;;
            *) break ;;
        esac
    done
    case "$1" in
        config)
            # The image each file gives each service, the last file winning,
            # printed the way compose indents its JSON.
            # shellcheck disable=SC2086
            awk '
                FNR == 1 { within = 0 }
                /^services:/ { within = 1; next }
                /^[^ #]/ { within = 0 }
                within && /^  [^ #]/ { name = $1; gsub(/["":]/, "", name); if (!(name in image)) order[++n] = name; image[name] = image[name] }
                within && /^    image:/ { value = $2; gsub(/"/, "", value); image[name] = value }
                END {
                    print "{"; print "  \"services\": {"
                    for (i = 1; i <= n; i++) {
                        print "    \"" order[i] "\": {"; print "      \"image\": \"" image[order[i]] "\""
                        print "    }" (i < n ? "," : "")
                    }
                    print "  }"; print "}"
                }
            ' ${files}
            ;;
        ps)
            case "${STUB_PS:-one}" in
                one) echo "live-1" ;;
                two) printf 'live-1\nlive-2\n' ;;
            esac
            ;;
        start) bump starts >/dev/null ;;
        run) for file in ${files}; do cp "${file}" "${FIXTURE}/trial.last"; done ;;
        up)
            count="$(bump ups)"
            if [ "${count}" = 0 ]; then
                [ -z "${STUB_TERM_ON_UP:-}" ] || kill -TERM "${PPID}"
                # The upgraded service writes over its data before failing.
                [ "${STUB_LIVE:-healthy}" = healthy ] || {
                    echo "overwritten" >"${FIXTURE}/volumes/app_data/state"
                    echo "overwritten" >"${FIXTURE}/stacks/app/app.conf"
                }
                # Something else writing in the stack swaps a data path for a
                # link to a file outside it, between the check and the rollback.
                if [ -n "${STUB_SWAP_ON_UP:-}" ]; then
                    rm -f "${FIXTURE}/stacks/app/app.conf"
                    ln -s "${STUB_SWAP_ON_UP}" "${FIXTURE}/stacks/app/app.conf"
                fi
            else
                [ -z "${STUB_ROLLBACK_UP_FAIL:-}" ] || exit 1
            fi
            ;;
    esac
    exit 0
fi
case "$1 $2" in
    "inspect -f")
        case "$3" in
            *Mounts*)
                printf 'volume app_data %s/volumes/app_data /data true\n' "${FIXTURE}"
                printf 'bind - %s/stacks/app/app.conf /etc/app.conf true\n' "${FIXTURE}"
                printf 'bind - /etc/ssl/certs /etc/ssl/certs false\n'
                [ -z "${STUB_EXTRA_MOUNT:-}" ] || printf 'bind - %s /srv true\n' "${STUB_EXTRA_MOUNT}"
                ;;
            *Config.Image*) echo "${STUB_RUNNING:-ghcr.io/example/app:1.2.0}" ;;
            *)
                case "$4" in
                    hq-trial-*) echo "running ${STUB_TRIAL:-healthy} 0 t0" ;;
                    *)
                        case "${STUB_LIVE:-healthy}" in
                            crashloop) n="$(bump restarts)"; echo "running none ${n} t${n}" ;;
                            *) echo "running ${STUB_LIVE:-healthy} 0 t0" ;;
                        esac
                        ;;
                esac
                ;;
        esac
        ;;
    "volume inspect") mkdir -p "${FIXTURE}/volumes/$5"; echo "${FIXTURE}/volumes/$5" ;;
    "volume create") mkdir -p "${FIXTURE}/volumes/$3" ;;
    "pull -q") [ -z "${STUB_PULL_FAIL:-}" ] || exit 1 ;;
esac
exit 0
STUB
chmod 0755 "${fixture}/bin/id" "${fixture}/bin/tar" "${fixture}/bin/docker"

DIGEST="sha256:$(printf '%064d' 0 | tr 0 a)"
TO="ghcr.io/example/app@${DIGEST}"
stack="${fixture}/stacks/app"
override="${stack}/compose.override.yaml"

reset() {
    rm -rf "${fixture}/stacks" "${fixture}/elsewhere" "${fixture}/state" "${fixture}/volumes" \
        "${fixture}/calls" "${fixture}/ups" "${fixture}/starts" "${fixture}/restarts" "${fixture}/trial.last"
    mkdir -p "${stack}" "${fixture}/volumes/app_data"
    echo "original" >"${fixture}/volumes/app_data/state"
    echo "setting" >"${stack}/app.conf"
    cat >"${stack}/compose.yaml" <<'YAML'
services:
  app:
    image: "ghcr.io/example/app:1.2.0"
    volumes: ["app_data:/data", "./app.conf:/etc/app.conf"]
  sidecar:
    image: example/sidecar:3
YAML
    cp "${stack}/compose.yaml" "${fixture}/compose.before"
}

with_override() {
    cat >"${override}" <<'YAML'
# Hardening kept beside the stack.
services:
  app:
    mem_limit: 512m
    pids_limit: 200
    security_opt:
      - no-new-privileges:true
  sidecar:
    read_only: true
YAML
    cp "${override}" "${fixture}/override.before"
}

upgrade() {
    FIXTURE="${fixture}" PATH="${fixture}/bin:${PATH}" \
        SEVERINO_UPGRADE_STACKS="${fixture}/stacks" SEVERINO_UPGRADE_STATE="${fixture}/state" \
        SEVERINO_UPGRADE_POLL=0 SEVERINO_UPGRADE_SETTLE=0 \
        sh "${script_dir}/upgrade-container.sh" --operation op-1 --project-dir "${stack}" \
        --service app --from ghcr.io/example/app:1.2.0 --to "${TO}" --tag 1.2.1 --wait "${DRILL_WAIT:-20}" "$@"
}

# One upgrade with stand-in settings: `with VAR=value... -- [argument...]`.
# A subshell, never `VAR=value upgrade`: a function's prefix assignments
# outlive the call in a POSIX shell (macOS's sh among them), and would leak
# into every case after it.
with() {
    (
        while [ $# -gt 0 ] && [ "$1" != -- ]; do
            export "${1?}"
            shift
        done
        [ $# -eq 0 ] || shift
        upgrade "$@"
    )
}

# Runs one upgrade; its exit status in ${code}, its output in ${out}.
attempt() {
    code=0
    out="$("$@")" || code=$?
}

untouched() { # untouched <what>
    cmp -s "${fixture}/compose.before" "${stack}/compose.yaml" || fail "$1 changed the compose file"
    [ "$(cat "${fixture}/volumes/app_data/state")" = original ] || fail "$1 left the data changed"
    [ "$(cat "${stack}/app.conf")" = setting ] || fail "$1 left the mounted file changed"
}

# Kept: pinned in a new override; the compose file itself untouched.
reset
attempt upgrade
[ "${code}" = 0 ] || fail "a proven upgrade exited ${code}"
case "${out}" in *'"outcome":"kept"'*) ;; *) fail "a proven upgrade did not say kept: ${out}" ;; esac
untouched "a kept upgrade"
[ "$(cat "${override}" 2>/dev/null)" = "$(printf 'services:\n  "app":\n    image: %s' "${TO}")" ] \
    || fail "the pin was not written to a new override"
has "compose -p app --project-directory ${stack} -f ${stack}/compose.yaml -f ${override} up -d --no-deps app" \
    "${fixture}/calls" || fail "the service was not recreated with the stack's file set and project"
[ -f "${fixture}/state/op-1/data-0.tgz" ] || fail "the volume was not snapshotted"
[ -f "${fixture}/state/op-1/data-1.tgz" ] && fail "a writable file mount was snapshotted as data"
stop_line="$(grep -n ' stop app$' "${fixture}/calls" | head -1 | cut -d: -f1)"
tar_line="$(grep -n '^tar .*-czf' "${fixture}/calls" | head -1 | cut -d: -f1)"
[ -n "${stop_line}" ] && [ -n "${tar_line}" ] && [ "${stop_line}" -lt "${tar_line}" ] \
    || fail "the data was snapshotted while the service ran"
has " run -d --no-deps --name hq-trial-op-1 app" "${fixture}/calls" || fail "the trial did not run from the service's definition"
grep -q '^run ' "${fixture}/calls" && fail "the trial was a hand-built docker run"
for line in "network_mode: none" "ports: !reset \[\]" 'target: "/data"' 'name: "hq-trial-op-1-0"'; do
    has "${line}" "${fixture}/trial.last" || fail "the trial definition lacks ${line}"
done

# The same operation again: its result, and no second run.
calls="$(wc -l <"${fixture}/calls")"
again="$(upgrade)" || fail "asking again exited $?"
[ "${again}" = "${out}" ] || fail "asking again answered differently"
[ "$(wc -l <"${fixture}/calls")" = "${calls}" ] || fail "asking again ran docker"

# An existing override keeps every other line when the pin is kept...
reset
with_override
attempt upgrade
[ "${code}" = 0 ] || fail "a proven upgrade with an override exited ${code}"
grep -v "^    image: ${TO}\$" "${override}" | cmp -s - "${fixture}/override.before" \
    || fail "the pin changed the override beyond its image line"
[ "$(sed -n 8p "${override}")" = "    image: ${TO}" ] || fail "the pin was not set on the service's own block"
cmp -s "${fixture}/override.before" "${fixture}/state/op-1/override.before" || fail "the override's copy is not byte-exact"
# ...and comes back byte for byte when it is rolled back.
reset
with_override
with STUB_LIVE=unhealthy -- >/dev/null || code=$?
[ "${code}" = 3 ] || fail "a failed upgrade with an override exited ${code}, not 3"
cmp -s "${fixture}/override.before" "${override}" || fail "the rollback did not restore the override exactly"

# A trial that fails changes nothing, and the service is started again.
reset
code=0
with STUB_TRIAL=unhealthy -- >"${fixture}/out" || code=$?
[ "${code}" = 2 ] || fail "a failed trial exited ${code}, not 2"
has '"outcome":"unchanged"' "${fixture}/out" || fail "a failed trial did not say unchanged"
untouched "a failed trial"
[ -e "${override}" ] && fail "a failed trial wrote an override"
[ -f "${fixture}/ups" ] && fail "a failed trial recreated the service"
[ "$(cat "${fixture}/starts" 2>/dev/null)" = 1 ] || fail "a failed trial left the service stopped"

# A snapshot that fails changes nothing, and says so.
reset
code=0
with STUB_TAR_SNAPSHOT_FAIL=1 -- >"${fixture}/out" || code=$?
[ "${code}" = 2 ] || fail "a failed snapshot exited ${code}, not 2"
has 'nothing changed' "${fixture}/out" || fail "a failed snapshot did not say nothing changed"
untouched "a failed snapshot"
[ -f "${fixture}/ups" ] && fail "a failed snapshot recreated the service"
[ "$(cat "${fixture}/starts" 2>/dev/null)" = 1 ] || fail "a failed snapshot left the service stopped"
for part in "${fixture}"/state/op-1/*.part; do
    [ ! -e "${part}" ] || fail "a failed snapshot left a partial file"
done

# An upgrade that does not prove itself is rolled back: override and data.
reset
code=0
with STUB_LIVE=unhealthy -- >/dev/null || code=$?
[ "${code}" = 3 ] || fail "a failed upgrade exited ${code}, not 3 (rolled back)"
untouched "a rollback"
[ -e "${override}" ] && fail "the rollback left the pin"
[ "$(cat "${fixture}/ups" 2>/dev/null)" = 2 ] || fail "the rollback did not recreate the service"

# The trial writes a copy of the mounted file, never the live one.
reset
attempt upgrade
has 'trial-1.file' "${fixture}/trial.last" || fail "the trial was not given its own copy of the mounted file"

# An override compose would not load beside compose.yaml is refused, not used.
reset
echo "services: {}" >"${stack}/docker-compose.override.yml"
code=0
upgrade >"${fixture}/out" || code=$?
[ "${code}" = 2 ] || fail "an override compose would not load was not refused (exit ${code})"
has 'is not the override compose loads' "${fixture}/out" || fail "an unloaded override was refused for another reason: $(cat "${fixture}/out")"

# A value carrying a newline passes no pattern on its first line alone.
reset
code=0
with -- --service "app
other" >"${fixture}/out" || code=$?
[ "${code}" = 2 ] || fail "a service name with a newline was not refused (exit ${code})"

# A crash loop never verifies: each restart starts the window again.
reset
code=0
with STUB_LIVE=crashloop DRILL_WAIT=2 -- >/dev/null || code=$?
[ "${code}" = 3 ] || fail "a crash-looping service was kept (exit ${code})"

# Stopped part way after the pin: put back, and still recorded.
reset
with_override
code=0
with STUB_TERM_ON_UP=1 STUB_LIVE=unhealthy -- >"${fixture}/out" 2>/dev/null || code=$?
[ "${code}" = 3 ] || fail "a run stopped after the pin exited ${code}, not 3"
cmp -s "${fixture}/override.before" "${override}" || fail "a run stopped after the pin kept the pin"
untouched "a stopped run"
has 'stopped part way' "${fixture}/state/op-1/result.json" || fail "a stopped run recorded no result"

# A data path swapped for a link after it was checked is not restored through:
# the rollback fails loudly and the file outside the stack is untouched.
reset
echo "outside" >"${fixture}/outside.conf"
code=0
with STUB_LIVE=unhealthy "STUB_SWAP_ON_UP=${fixture}/outside.conf" -- >"${fixture}/out" || code=$?
[ "${code}" = 1 ] || fail "a rollback through a swapped link exited ${code}, not 1"
[ "$(cat "${fixture}/outside.conf")" = outside ] || fail "the rollback wrote through a link out of the stack"
has 'could not be restored' "${fixture}/out" || fail "a swapped data path was not reported"

# A rollback that fails is reported as a failure, a full disk as such.
reset
code=0
with STUB_LIVE=unhealthy STUB_TAR_FULL=1 -- >"${fixture}/out" || code=$?
[ "${code}" = 1 ] || fail "a rollback onto a full disk exited ${code}, not 1"
has 'the disk filled' "${fixture}/out" || fail "a full disk during the rollback was not reported"
[ -e "${override}" ] && fail "a failed rollback left the pin"
reset
code=0
with STUB_LIVE=unhealthy STUB_ROLLBACK_UP_FAIL=1 -- >"${fixture}/out" || code=$?
[ "${code}" = 1 ] || fail "a rollback that could not restart exited ${code}, not 1"
has 'could not be started again' "${fixture}/out" || fail "a rollback that could not restart was not reported"

# Refused after reading the machine, before anything changes.
refused_by_docker() { # refused_by_docker <what> <reason> <setting...> [-- <argument...>]
    what="$1"
    reason="$2"
    shift 2
    reset
    code=0
    with "$@" >"${fixture}/out" || code=$?
    [ "${code}" = 2 ] || fail "${what} was not refused (exit ${code})"
    has "${reason}" "${fixture}/out" || fail "${what} was refused for another reason: $(cat "${fixture}/out")"
    has ' stop ' "${fixture}/calls" && fail "${what} stopped the service"
    untouched "${what}"
    [ -e "${override}" ] && fail "${what} wrote an override"
    return 0
}
refused_by_docker "two containers for one service" "not exactly one running container" STUB_PS=two --
refused_by_docker "a container not running --from" "the running container is not" \
    STUB_RUNNING=ghcr.io/example/app:1.1.0 --
refused_by_docker "a writable directory outside the stack" "outside the stack is mounted at /srv" \
    "STUB_EXTRA_MOUNT=${fixture}" --
refused_by_docker "a failed pull" '"step":"pull","ok":false' STUB_PULL_FAIL=1 --
refused_by_docker "a --from compose does not resolve" "compose does not resolve app to" \
    -- --from ghcr.io/example/app:1.1.0 --to "ghcr.io/example/app@${DIGEST}"

# Refused from the arguments and the filesystem alone: no docker at all.
refused() { # refused <what> <command...>
    what="$1"
    shift
    code=0
    "$@" >"${fixture}/out" || code=$?
    [ "${code}" = 2 ] || fail "${what} was not refused (exit ${code})"
    [ -f "${fixture}/calls" ] && fail "${what} called docker"
    untouched "${what}"
    [ -f "${override}" ] && [ ! -L "${override}" ] && fail "${what} wrote an override"
    return 0
}
reset
refused "a tag for --to" upgrade --to ghcr.io/example/app:1.2.1
reset
refused "a foreign --to repository" upgrade --to "ghcr.io/example/other@${DIGEST}"
reset
refused "a --to on another registry" upgrade --to "registry.example.com/example/app@${DIGEST}"
reset
refused "caller-supplied data" upgrade --data app_data:/data
reset
mkdir -p "${fixture}/elsewhere/app"
cp "${stack}/compose.yaml" "${fixture}/elsewhere/app/"
refused "a stack outside the stacks root" upgrade --project-dir "${fixture}/elsewhere/app"
reset
refused "a stack below a stack" upgrade --project-dir "${stack}/nested"
reset
refused "a path that climbs out" upgrade --project-dir "${fixture}/stacks/../elsewhere"
reset
ln -s "${stack}" "${fixture}/stacks/linked"
refused "a symlinked stack directory" upgrade --project-dir "${fixture}/stacks/linked"
reset
mkdir -p "${fixture}/stacks/other"
ln -s "${stack}/compose.yaml" "${fixture}/stacks/other/compose.yaml"
refused "a symlinked compose file" upgrade --project-dir "${fixture}/stacks/other"
reset
ln -s "${stack}/compose.yaml" "${override}"
refused "a symlinked override" upgrade
rm -f "${override}"
reset
chmod g+w "${stack}/compose.yaml"
refused "a group-writable compose file" upgrade
reset
chmod o+w "${stack}"
refused "a world-writable stack directory" upgrade
chmod o-w "${stack}"
reset
printf 'services:\n  app: {image: x}\n' >"${override}"
code=0
upgrade >"${fixture}/out" || code=$?
[ "${code}" = 2 ] || fail "a flow-style override was edited (exit ${code})"
has 'not block style' "${fixture}/out" || fail "a flow-style override was not named as the reason"
has ' stop app' "${fixture}/calls" && fail "a flow-style override stopped the service before it was refused"
[ "$(cat "${override}")" = "$(printf 'services:\n  app: {image: x}')" ] || fail "a flow-style override was changed"
rm -f "${override}"
# An override written as JSON is refused the same way, with nothing stopped.
reset
printf '{\n  "services": {\n    "app": {\n      "mem_limit": "512m"\n    }\n  }\n}\n' >"${override}"
cp "${override}" "${fixture}/override.json"
code=0
upgrade >"${fixture}/out" || code=$?
[ "${code}" = 2 ] || fail "a JSON override was edited (exit ${code})"
has 'not block style' "${fixture}/out" || fail "a JSON override was not named as the reason"
has ' stop app' "${fixture}/calls" && fail "a JSON override stopped the service before it was refused"
cmp -s "${fixture}/override.json" "${override}" || fail "a JSON override was changed"
rm -f "${override}"
reset
refused "a non-root run" with STUB_UID=1000 --
# Through sudo the stacks root cannot be moved: SUDO_UID pins it.
reset
code=0
with SUDO_UID=1000 -- >"${fixture}/out" || code=$?
[ "${code}" = 2 ] || fail "a run through sudo used the caller's stacks root (exit ${code})"
has 'not directly under /opt/apps' "${fixture}/out" || fail "a run through sudo did not use the fixed stacks root: $(cat "${fixture}/out")"

[ "${failures}" -eq 0 ] || exit 1
echo "Upgrade helper drill passed."
