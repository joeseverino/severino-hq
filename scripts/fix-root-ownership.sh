#!/bin/sh
# Point root-run units at a root-owned copy of the scripts they execute.
#
# The deploy checkout at /opt/apps/severino-hq is writable by an unprivileged
# account. Three units run out of it as root, so anything able to write that
# tree chooses what root runs on the next timer tick: privilege escalation by
# file write, with no exploit needed.
#
# The fix is two halves and this installs the second. `severino-hq-sync-scripts`
# copies the scripts out of the verified image into /usr/local/lib/severino-hq,
# which only root can write; this drops an override on each unit so its
# ExecStart names that copy instead. `severino-hq-check-scripts`, on a daily
# timer, fails when the two drift apart.
#
# Idempotent: it installs the same files every time and reloads once. Those
# files are the `10-root-owned-exec.conf` drop-ins shipped in deploy/systemd,
# which install-controller.sh also installs on every deploy: this is the first
# bring-up, before any deploy has run.
#
#   scripts/fix-root-ownership.sh            # install
#   scripts/fix-root-ownership.sh --remove   # undo, until the next deploy
#
# The next deploy reinstalls what the repository ships, so a lasting undo is
# deleting the drop-in from deploy/systemd.

set -eu

LIB=/usr/local/lib/severino-hq
SHIPPED="${LIB}/deploy/systemd"
UNIT_DIR=/etc/systemd/system
DROPIN=10-root-owned-exec.conf
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/systemd-units.sh
. "${script_dir}/lib/systemd-units.sh"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
[ -d "${LIB}" ] || {
    echo "${LIB} does not exist: run severino-hq-sync-scripts first" >&2
    exit 1
}

# The units this pins are the ones the repository ships a drop-in of this name
# for, derived rather than listed, so pinning a fourth root unit is shipping its
# drop-in and cannot be half-done.
shipped="$(units_shipped "${SHIPPED}")"
dropins=""
for f in ${shipped}; do
    case "${f}" in *.d/"${DROPIN}") dropins="${dropins} ${f}" ;; esac
done
if [ -z "${dropins}" ]; then
    echo "${SHIPPED} ships no ${DROPIN}" >&2
    exit 1
fi

if [ "${1:-}" = "--remove" ]; then
    for f in ${dropins}; do
        rm -f "${UNIT_DIR}/${f}"
        rmdir "${UNIT_DIR}/${f%/*}" 2>/dev/null || true
        echo "removed the override on ${f%.d/*}"
    done
    systemctl daemon-reload
    exit 0
fi

# Refuse to point units at a tree that is not there or not root's. Installing
# an ExecStart that names a missing file leaves units that fail on their next
# tick, and pointing them at a tree somebody else can write would reinstate
# exactly the exposure this removes.
owner="$(stat -c '%U' "${LIB}")"
[ "${owner}" = root ] || { echo "${LIB} is owned by ${owner}, not root" >&2; exit 1; }
if [ -n "$(find "${LIB}" \( -perm -0020 -o -perm -0002 \) -print -quit)" ]; then
    echo "${LIB} contains a group- or world-writable path" >&2
    exit 1
fi

for f in ${dropins}; do
    # The last assignment is the command; the empty one before it clears the
    # unit's, since systemd appends to ExecStart for Type=oneshot.
    target="$(sed -n 's/^ExecStart=\([^ ][^ ]*\).*/\1/p' "${SHIPPED}/${f}" | tail -1)"
    [ -x "${target}" ] || { echo "${target:-${f}: no ExecStart} is missing or not executable" >&2; exit 1; }
    units_install "${SHIPPED}" "${UNIT_DIR}" "${f}"
    echo "pinned ${f%.d/*} to ${target}"
done

systemctl daemon-reload

# Say what systemd resolved, not what was written. A drop-in that does not
# apply: wrong directory, wrong unit name, a typo in the section header,
# leaves the file on disk looking correct while the unit still runs the old
# command, which is the one failure this cannot afford to report as success.
for f in ${dropins}; do
    unit="${f%.d/*}"
    resolved="$(systemctl show -p ExecStart --value "${unit}" 2>/dev/null || true)"
    case "${resolved}" in
        *"${LIB}"*) echo "verified ${unit} runs from ${LIB}" ;;
        *) echo "FAILED: ${unit} still resolves to: ${resolved}" >&2; exit 1 ;;
    esac
done
