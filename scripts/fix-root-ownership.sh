#!/bin/sh
# Point root-run units at a root-owned copy of the scripts they execute.
#
# The deploy checkout at /opt/apps/severino-hq is writable by an unprivileged
# account. Three units run out of it as root, so anything able to write that
# tree chooses what root runs on the next timer tick -- privilege escalation by
# file write, with no exploit needed.
#
# The fix is two halves and this installs the second. `severino-hq-sync-scripts`
# copies the scripts out of the verified image into /usr/local/lib/severino-hq,
# which only root can write; this drops an override on each unit so its
# ExecStart names that copy instead. `severino-hq-check-scripts`, on a daily
# timer, fails when the two drift apart.
#
# Idempotent: it writes the same three files every time and reloads once.
#
#   scripts/fix-root-ownership.sh            # install
#   scripts/fix-root-ownership.sh --remove   # undo
#
# This script used to exist only as a comment. Each drop-in said it had been
# installed by a file of this name, and no such file was in the repository, in
# the image, or anywhere on the host -- so the one instruction for undoing or
# reproducing the change named something nobody could find.

set -eu

LIB=/usr/local/lib/severino-hq
UNIT_DIR=/etc/systemd/system
DROPIN=10-root-owned-exec.conf

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

# Unit, then the ExecStart it should run, then any extra directives. Held as a
# list rather than three near-identical blocks so adding a fourth root unit is
# one line and cannot be half-done.
units="severino-hq-secrets:${LIB}/scripts/refresh-secrets.sh:
severino-hq-controller:${LIB}/scripts/run-controller.sh --apply:WorkingDirectory=${LIB}
severino-hq-backup:${LIB}/scripts/backup.sh:"

if [ "${1:-}" = "--remove" ]; then
    printf '%s\n' "${units}" | while IFS=: read -r unit _ _; do
        [ -n "${unit}" ] || continue
        rm -f "${UNIT_DIR}/${unit}.service.d/${DROPIN}"
        rmdir "${UNIT_DIR}/${unit}.service.d" 2>/dev/null || true
        echo "removed the override on ${unit}.service"
    done
    systemctl daemon-reload
    exit 0
fi

# Refuse to point units at a tree that is not there or not root's. Installing
# an ExecStart that names a missing file leaves three units that fail on their
# next tick, and pointing them at a tree somebody else can write would reinstate
# exactly the exposure this removes.
[ -d "${LIB}" ] || {
    echo "${LIB} does not exist -- run severino-hq-sync-scripts first" >&2
    exit 1
}
owner="$(stat -c '%U' "${LIB}")"
[ "${owner}" = root ] || { echo "${LIB} is owned by ${owner}, not root" >&2; exit 1; }
if [ -n "$(find "${LIB}" \( -perm -0020 -o -perm -0002 \) -print -quit)" ]; then
    echo "${LIB} contains a group- or world-writable path" >&2
    exit 1
fi

printf '%s\n' "${units}" | while IFS=: read -r unit exec_start extra; do
    [ -n "${unit}" ] || continue
    target="${exec_start%% *}"
    [ -x "${target}" ] || { echo "${target} is missing or not executable" >&2; exit 1; }
    mkdir -p "${UNIT_DIR}/${unit}.service.d"
    {
        echo "# Installed by scripts/fix-root-ownership.sh. Points a root unit at a"
        echo "# root-owned copy of the script instead of the user-writable deploy"
        echo "# checkout. Undo with: scripts/fix-root-ownership.sh --remove"
        echo "[Service]"
        # Cleared first: systemd appends to ExecStart for Type=oneshot, so an
        # override without the empty assignment adds a second command rather
        # than replacing the first, and the checkout copy still runs.
        echo "ExecStart="
        echo "ExecStart=${exec_start}"
        # Not `[ -n "$extra" ] && echo ...`: an empty extra makes that list
        # return non-zero, and under `set -e` a bare failing list ends the
        # script -- two of these three units have no extra directive.
        if [ -n "${extra}" ]; then echo "${extra}"; fi
        echo "ReadOnlyPaths=${LIB}"
    } >"${UNIT_DIR}/${unit}.service.d/${DROPIN}"
    chmod 0644 "${UNIT_DIR}/${unit}.service.d/${DROPIN}"
    echo "pinned ${unit}.service to ${target}"
done

systemctl daemon-reload

# Say what systemd resolved, not what was written. A drop-in that does not
# apply -- wrong directory, wrong unit name, a typo in the section header --
# leaves the file on disk looking correct while the unit still runs the old
# command, which is the one failure this cannot afford to report as success.
printf '%s\n' "${units}" | while IFS=: read -r unit _ _; do
    [ -n "${unit}" ] || continue
    resolved="$(systemctl show -p ExecStart --value "${unit}.service" 2>/dev/null || true)"
    case "${resolved}" in
        *"${LIB}"*) echo "verified ${unit}.service runs from ${LIB}" ;;
        *) echo "FAILED: ${unit}.service still resolves to: ${resolved}" >&2; exit 1 ;;
    esac
done
