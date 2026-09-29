#!/bin/sh
# The deploy host's half of scripts/preflight.sh, run there over SSH as the
# login user. Read-only: it changes nothing, writes nothing, and prints counts,
# names of files this repository ships, and verdicts, never file contents or
# sudo rules. Every sudo reads /dev/null, because stdin is this script.
#
# Inputs, set by scripts/preflight.sh ahead of this text:
#   PREFLIGHT_MANIFEST       root-tree.sha256 of the branch being released
#   PREFLIGHT_UNITS          units that branch and main both ship
#   PREFLIGHT_MIN_FREE_KB    deploy-image.sh's free-space floor
# and the functions of scripts/lib/checkout.sh.
#
# Prints one "ok", "warn" or "FAIL" line per check and exits 1 on any FAIL.

set -eu

if ! command -v checkout_foreign_owned >/dev/null 2>&1; then
    # shellcheck source=scripts/lib/checkout.sh
    . "$(dirname "$0")/lib/checkout.sh"
fi

lib="${PREFLIGHT_LIB:-/usr/local/lib/severino-hq}"
sbin="${PREFLIGHT_SBIN:-/usr/local/sbin}"
app="${PREFLIGHT_APP:-/opt/apps/severino-hq}"
units_dir="${PREFLIGHT_SYSTEMD:-/etc/systemd/system}"
manifest="${PREFLIGHT_MANIFEST:?}"
failed=0

ok() { echo "ok      $*"; }
warn() { echo "warn    $*"; }
bad() { echo "FAIL    $*"; failed=1; }

# 1. The deploy pulls the checkout as the Actions runner's account.
if ! runner="$(checkout_deploy_account 2>&1)"; then
    bad "${runner}"
    runner=""
elif ! foreign="$(checkout_foreign_owned "${app}" "${runner}" 2>/dev/null)"; then
    bad "could not check ownership under ${app}/.git"
elif [ -n "${foreign}" ]; then
    bad "$(printf '%s\n' "${foreign}" | wc -l | tr -d ' ') path(s) under ${app}/.git not owned by ${runner}; fix: $(checkout_ownership_fix "${app}" "${runner}")"
else
    ok "${app}/.git is owned by ${runner}"
fi

# 2. The runner may run the deploy entrypoint through sudo, and nothing broader.
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

# 3. The root-owned tree against the branch being released, and against itself.
same=0
changed=""
added=""
while read -r sum path; do
    [ -n "${path}" ] || continue
    if [ ! -f "${lib}/${path}" ]; then
        added="${added} ${path}"
    elif [ "$(sha256sum <"${lib}/${path}" | cut -d' ' -f1)" = "${sum}" ]; then
        same=$((same + 1))
    else
        changed="${changed} ${path}"
    fi
done <<EOF
${manifest}
EOF
ok "root tree: ${same} file(s) identical to the release"
for path in ${changed}; do warn "the release changes ${path}"; done
for path in ${added}; do warn "the release adds ${path}"; done
case " ${changed} " in
    *" scripts/deploy-image.sh "*)
        warn "the host's deploy-image.sh carries this release up to its sync; the release's runs from the next" ;;
esac

if [ ! -f "${lib}/root-tree.sha256" ]; then
    warn "${lib} has no root-tree.sha256; the first release that ships one installs it"
elif sh "${lib}/scripts/root-tree-manifest.sh" "${lib}" 2>/dev/null | cmp -s - "${lib}/root-tree.sha256"; then
    ok "${lib} reproduces its own root-tree.sha256"
else
    bad "${lib} does not reproduce its own root-tree.sha256: changed on the host since its sync"
fi

# 4. Root-owned programs in sbin: each one this repository ships a copy of, and
#    none that no release updates.
for program in "${sbin}"/severino-hq-*; do
    [ -e "${program}" ] || continue
    name="${program##*/}"
    shipped="$(printf '%s\n' "${manifest}" | awk -v p="scripts/${name}" '$2 == p {print $1}')"
    if [ -z "${shipped}" ]; then
        bad "${program} is not shipped by this release, so no deploy updates it; remove it"
    elif [ "$(sudo sha256sum "${program}" </dev/null | cut -d' ' -f1)" = "${shipped}" ]; then
        ok "${program} is identical to the release"
    else
        warn "${program} differs from the release, which installs its own"
    fi
done
[ -e "${sbin}/severino-hq-sync-scripts" ] || bad "${sbin}/severino-hq-sync-scripts is missing; run fix-root-ownership.sh"

# 4b. The upgrade helper, which a sudo rule lets another account run as root:
#     root's, and the directories above it too, writable by no one else, and
#     the release's copy. Absent is not a fault: the release adds it.
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
    shipped="$(printf '%s\n' "${manifest}" | awk '$2 == "scripts/upgrade-container.sh" {print $1}')"
    if [ "${guarded}" -eq 0 ]; then
        :
    elif [ "$(sha256sum <"${helper}" | cut -d' ' -f1)" = "${shipped}" ]; then
        ok "${helper} is root's alone and identical to the release"
    else
        warn "${helper} differs from the release, which installs its own"
    fi
fi

# 5. Every unit the release expects to find already installed.
missing=0
for unit in ${PREFLIGHT_UNITS:-}; do
    [ -f "${units_dir}/${unit}" ] || { bad "${units_dir}/${unit} is not installed"; missing=1; }
done
[ "${missing}" -eq 1 ] || ok "every established unit is installed"

# 6. Room for the deploy's pull.
available_kb="$(df -Pk / | awk 'NR == 2 {print $4}')"
if [ "${available_kb}" -ge "${PREFLIGHT_MIN_FREE_KB:?}" ]; then
    ok "$((available_kb / 1024)) MiB free on /"
else
    bad "$((available_kb / 1024)) MiB free on /; the deploy needs $((PREFLIGHT_MIN_FREE_KB / 1024)) MiB"
fi

exit "${failed}"
