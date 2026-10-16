#!/bin/sh
# The deploy host's half of scripts/preflight.sh, run there over SSH as the
# login user. Read-only: it changes nothing, writes nothing, and prints
# verdicts, never file contents or sudo rules. Every sudo reads /dev/null,
# because stdin is this script.
#
# Inputs, set by scripts/preflight.sh ahead of this text:
#   PREFLIGHT_UNITS          units that branch and main both ship
#   PREFLIGHT_MIN_FREE_KB    hq-host deploy's free-space floor
#
# Prints one "ok", "warn" or "FAIL" line per check and exits 1 on any FAIL.

set -eu

lib="${PREFLIGHT_LIB:-/usr/local/lib/severino-hq}"
units_dir="${PREFLIGHT_SYSTEMD:-/etc/systemd/system}"
failed=0

ok() { echo "ok      $*"; }
warn() { echo "warn    $*"; }
bad() { echo "FAIL    $*"; failed=1; }

# 1. The runner deploys through one sudo rule, and may do nothing broader.
runner_units="$(systemctl list-units --all --plain --no-legend 'actions.runner.*.service' 2>/dev/null |
    awk '{print $1}')"
runner=""
if [ "$(printf '%s\n' "${runner_units}" | grep -c .)" -ne 1 ]; then
    bad "expected one Actions runner unit, found: $(printf '%s' "${runner_units:-none}" | tr '\n' ' ')"
else
    runner="$(systemctl show -p User --value "${runner_units}" 2>/dev/null)"
    case "${runner}" in
        '' | root) bad "the Actions runner runs as '${runner:-root}'; it must run as the deploy account"; runner="" ;;
    esac
fi
entrypoint="${lib}/scripts/deploy-image.sh"
if [ -n "${runner}" ]; then
    if ! rules="$(sudo -l -U "${runner}" </dev/null 2>/dev/null)"; then
        bad "could not list ${runner}'s sudo rules"
    elif printf '%s\n' "${rules}" | grep -Eq '(^|[[:space:]:])ALL[[:space:]]*$'; then
        bad "${runner} may run any command through sudo"
    elif printf '%s\n' "${rules}" | grep -q "NOPASSWD:.*${entrypoint}"; then
        ok "${runner} may run ${entrypoint} through sudo"
    else
        bad "${runner} has no NOPASSWD sudo rule for ${entrypoint}"
    fi
fi

# 2. The host is the release it runs: the tree, the units, nothing else.
if drift="$(sudo -n "${lib}/deploy/bin/hq-host" verify </dev/null 2>&1)"; then
    ok "the host is the release it runs"
elif printf '%s\n' "${drift}" | grep -q '^drift: '; then
    printf '%s\n' "${drift}" | sed -n 's/^drift: //p' | while IFS= read -r line; do echo "FAIL    ${line}"; done
    failed=1
else
    warn "could not run hq-host verify without a password; run 'sudo ${lib}/deploy/bin/hq-host verify' there"
fi

# 3. The upgrade helper, which a sudo rule lets another account run as root:
#    root's, and the directories above it too, writable by no one else.
helper="${lib}/scripts/upgrade-container.sh"
root_uid="${PREFLIGHT_ROOT_UID:-0}"
if [ -e "${helper}" ] || [ -L "${helper}" ]; then
    guarded=1
    for path in "${lib}" "${lib}/scripts" "${helper}"; do
        # shellcheck disable=SC2012  # ls -ln reads mode and owner alike on every system
        listing="$(ls -ldn -- "${path}")"
        if [ -L "${path}" ]; then
            bad "${path} is a symlink; a sudo rule runs the helper as root"
        elif [ "$(printf '%s\n' "${listing}" | awk '{print $3}')" != "${root_uid}" ]; then
            bad "${path} is not owned by root; a sudo rule runs the helper as root"
        elif printf '%s\n' "${listing}" | cut -c6,9 | grep -q w; then
            bad "${path} is writable by its group or others; a sudo rule runs the helper as root"
        else
            continue
        fi
        guarded=0
    done
    [ "${guarded}" -eq 0 ] || ok "${helper} is root's alone"
fi

# 4. Every unit the release expects to find already installed.
missing=0
for unit in ${PREFLIGHT_UNITS:-}; do
    [ -f "${units_dir}/${unit}" ] || { bad "${units_dir}/${unit} is not installed"; missing=1; }
done
[ "${missing}" -eq 1 ] || ok "every established unit is installed"

# 5. Room for the deploy's pull.
available_kb="$(df -Pk / | awk 'NR == 2 {print $4}')"
if [ "${available_kb}" -ge "${PREFLIGHT_MIN_FREE_KB:?}" ]; then
    ok "$((available_kb / 1024)) MiB free on /"
else
    bad "$((available_kb / 1024)) MiB free on /; the deploy needs $((PREFLIGHT_MIN_FREE_KB / 1024)) MiB"
fi

exit "${failed}"
