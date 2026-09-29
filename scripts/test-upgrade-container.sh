#!/bin/sh
# The upgrade helper against a stand-in Docker: kept when the target proves
# itself, refused before anything changes when the trial fails, rolled back
# (compose file and data) when the upgraded service does not, and never twice
# for one operation. Secrets from the live environment never reach argv.

# upgrade() takes extra arguments only through refused(), which shellcheck
# cannot follow.
# shellcheck disable=SC2119,SC2120

set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
fixture="$(mktemp -d)"
readonly fixture
trap 'rm -rf "${fixture}"' EXIT HUP INT TERM

failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

mkdir -p "${fixture}/bin" "${fixture}/volumes"
cat >"${fixture}/bin/id" <<'STUB'
#!/bin/sh
echo "${STUB_UID:-0}"
STUB
cat >"${fixture}/bin/docker" <<'STUB'
#!/bin/sh
# Docker as the helper uses it; every call is logged, one per line.
echo "$*" >>"${FIXTURE}/calls"
case "$1 $2" in
    "compose --project-directory")
        shift 5
        case "$1" in
            ps) echo "live-1" ;;
            up)
                count="$(cat "${FIXTURE}/ups" 2>/dev/null || echo 0)"
                echo $((count + 1)) >"${FIXTURE}/ups"
                # The upgraded service writes over its data before failing.
                [ "${STUB_LIVE:-healthy}" = healthy ] || [ "${count}" != 0 ] \
                    || echo "overwritten" >"${FIXTURE}/volumes/app_data/state"
                ;;
        esac
        ;;
    "inspect -f")
        case "$3" in
            *Config.Env*) printf 'PATH=/usr/bin\nSECRET_TOKEN=example-secret-value\n' ;;
            *)
                case "$4" in
                    hq-trial-*) echo "running ${STUB_TRIAL:-healthy}" ;;
                    *) echo "running ${STUB_LIVE:-healthy}" ;;
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
chmod 0755 "${fixture}/bin/id" "${fixture}/bin/docker"

DIGEST="sha256:$(printf 'a%.0s' $(seq 64))"
TO="ghcr.io/example/app@${DIGEST}"

reset() {
    rm -rf "${fixture}/project" "${fixture}/state" "${fixture}/calls" "${fixture}/ups" "${fixture}/volumes"
    mkdir -p "${fixture}/project" "${fixture}/volumes/app_data"
    echo "original" >"${fixture}/volumes/app_data/state"
    cat >"${fixture}/project/compose.yaml" <<'YAML'
services:
  app:
    image: "ghcr.io/example/app:1.2.0"
    volumes: ["app_data:/data"]
  sidecar:
    image: example/sidecar:3
YAML
    cp "${fixture}/project/compose.yaml" "${fixture}/compose.before"
}

upgrade() {
    FIXTURE="${fixture}" PATH="${fixture}/bin:${PATH}" \
        SEVERINO_UPGRADE_STATE="${fixture}/state" SEVERINO_UPGRADE_POLL=0 SEVERINO_UPGRADE_SETTLE=0 \
        sh "${script_dir}/upgrade-container.sh" --operation op-1 --project-dir "${fixture}/project" \
        --service app --from ghcr.io/example/app:1.2.0 --to "${TO}" --tag 1.2.1 \
        --data app_data:/data --wait 1 "$@"
}

# Kept: pinned in place, the tag kept as a comment, nothing else touched.
reset
out="$(upgrade)" || fail "a proven upgrade exited $?"
grep -q "\"outcome\":\"kept\"" <<EOF_OUT || fail "a proven upgrade did not say kept: ${out}"
${out}
EOF_OUT
grep -q "^    image: ${TO} # 1.2.1\$" "${fixture}/project/compose.yaml" || fail "the digest was not pinned in place"
grep -q "^    image: example/sidecar:3\$" "${fixture}/project/compose.yaml" || fail "another service's image changed"
[ -f "${fixture}/state/op-1/app_data.tgz" ] || fail "the data was not snapshotted"
grep -q "SECRET_TOKEN\|example-secret-value" "${fixture}/calls" && fail "a secret reached docker's argv"
[ -f "${fixture}/state/op-1/trial.env" ] && fail "the trial's environment was left on disk"

# The same operation again: its result, and no second run.
calls="$(wc -l <"${fixture}/calls")"
again="$(upgrade)" || fail "asking again exited $?"
[ "${again}" = "${out}" ] || fail "asking again answered differently"
[ "$(wc -l <"${fixture}/calls")" = "${calls}" ] || fail "asking again ran docker"

# A trial that fails changes nothing.
reset
STUB_TRIAL=unhealthy upgrade >/dev/null && fail "a failed trial exited 0"
cmp -s "${fixture}/compose.before" "${fixture}/project/compose.yaml" || fail "a failed trial changed the compose file"
[ -f "${fixture}/ups" ] && fail "a failed trial recreated the service"

# An upgrade that does not prove itself is rolled back: compose file and data.
reset
code=0
STUB_LIVE=unhealthy upgrade >/dev/null || code=$?
[ "${code}" = 3 ] || fail "a failed upgrade exited ${code}, not 3 (rolled back)"
cmp -s "${fixture}/compose.before" "${fixture}/project/compose.yaml" || fail "the rollback left the pin"
[ "$(cat "${fixture}/volumes/app_data/state")" = original ] || fail "the rollback left the data overwritten"
[ "$(cat "${fixture}/ups")" = 2 ] || fail "the rollback did not recreate the service"

# Refused before anything changes.
refused() {
    reset
    code=0
    "$@" >/dev/null || code=$?
    [ "${code}" = 2 ] || fail "$1-refusal exited ${code}"
    [ -f "${fixture}/calls" ] && fail "a refused request called docker ($*)"
    cmp -s "${fixture}/compose.before" "${fixture}/project/compose.yaml" || fail "a refused request changed the compose file"
}
refused upgrade --to ghcr.io/example/app:1.2.1
reset
printf '  other:\n    image: "ghcr.io/example/app:1.2.0"\n' >>"${fixture}/project/compose.yaml"
code=0
upgrade >/dev/null || code=$?
[ "${code}" = 2 ] || fail "an image named twice was pinned (exit ${code})"
reset
code=0
STUB_UID=1000 upgrade >/dev/null || code=$?
[ "${code}" = 2 ] || fail "a non-root run was not refused (exit ${code})"

[ "${failures}" -eq 0 ] || exit 1
echo "Upgrade helper drill passed."
