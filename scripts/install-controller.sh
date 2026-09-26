#!/bin/sh
# Install reviewed controller units, prove connectivity, then enable apply.

set -eu

readonly lib_dir="/usr/local/lib/severino-hq"
readonly unit_dir="${lib_dir}/deploy/systemd"
readonly systemd_dir="/etc/systemd/system"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/controller-env.sh
. "${script_dir}/lib/controller-env.sh"
# shellcheck source=scripts/lib/systemd-units.sh
. "${script_dir}/lib/systemd-units.sh"
readonly private_log_dir="/var/log/severino-hq"
readonly private_run="${lib_dir}/scripts/run-private.sh"

if [ "$(id -u)" -ne 0 ]; then
    echo "install-controller.sh must run as root." >&2
    exit 1
fi

# The units below run as root out of /usr/local/lib/severino-hq rather than out
# of this checkout: what root runs should not be writable by what deploys.
# Refresh that tree from the image now running, before anything starts, so a
# deploy shipping new scripts does not leave root executing the previous ones.
#
# Hard failure rather than a fallback: the units name that path, so a host
# without the tree would fail to start them anyway, and failing here says why.
if [ ! -x /usr/local/sbin/severino-hq-sync-scripts ]; then
    echo "severino-hq-sync-scripts is missing: run fix-root-ownership.sh --apply on this host first." >&2
    exit 1
fi
/usr/local/sbin/severino-hq-sync-scripts

# The verifier the *next* deploy will check this repository's signature with,
# refreshed by the same mechanism and from the same tree as the script that uses
# it. Pinned by digest inside, and a no-op when it is already correct, so this
# costs nothing on a host that is already right.
/usr/local/lib/severino-hq/scripts/install-cosign.sh

# What is installed is what the repository ships: every unit and drop-in under
# deploy/systemd, found by walking it. A unit added there is installed, verified
# and, if it is a timer or path, enabled, with nothing here edited.
shipped="$(units_shipped "${unit_dir}")"
# The secrets unit and its drop-ins go first and alone: the render has to run as
# this release describes it before anything that consumes its output is
# started, and they are the one part of the set rolled back on failure.
readonly render_unit="severino-hq-secrets.service"
is_render_unit() {
    case "$1" in "${render_unit}" | "${render_unit}.d/"*) return 0 ;; esac
    return 1
}
render_units=""
for f in ${shipped}; do
    if is_render_unit "${f}"; then render_units="${render_units} ${f}"; fi
done

# Drop-ins are not units and cannot be verified alone; systemd reads them with
# their unit from the directory the unit is installed in.
set --
for f in ${shipped}; do
    case "${f}" in */*) ;; *) set -- "$@" "${unit_dir}/${f}" ;; esac
done
systemd-analyze verify "$@"

# Restore the previous renderer units if activation or a preflight fails.
umask 077
unit_backup="$(mktemp -d /run/severino-hq-unit.XXXXXX)"
# Flattened, because a drop-in's path has a directory in it and the backup only
# needs a name per file.
backup_of() { printf '%s/%s' "${unit_backup}" "$(printf '%s' "$1" | tr / :)"; }
for f in ${render_units}; do
    if [ -f "${systemd_dir}/${f}" ]; then
        cp -p "${systemd_dir}/${f}" "$(backup_of "${f}")"
    fi
done
unit_committed=0
finish() {
    result=$?
    trap - EXIT
    if [ "${unit_committed}" -eq 0 ]; then
        for f in ${render_units}; do
            if [ -f "$(backup_of "${f}")" ]; then
                cp -p "$(backup_of "${f}")" "${systemd_dir}/${f}"
            else
                rm -f "${systemd_dir}/${f}"
            fi
        done
        systemctl daemon-reload || result=1
    fi
    rm -rf "${unit_backup}"
    exit "${result}"
}
trap finish EXIT
trap 'exit 1' HUP INT TERM
for f in ${render_units}; do
    units_install "${unit_dir}" "${systemd_dir}" "${f}"
done
systemctl daemon-reload
systemctl start "${render_unit}"
controller_require_environment

# These commands intentionally return rich machine JSON: locally useful,
# inappropriate in the public Actions stream inherited by a self-hosted deploy.
# Keep each latest result on the host with root-only permissions and expose only
# a fixed status line plus the original exit code to the deployment gate.
install -d -o root -g root -m 0700 "${private_log_dir}"
"${private_run}" \
    "Controller connection preflight" \
    "${private_log_dir}/controller-preflight.log" \
    "${lib_dir}/scripts/run-controller.sh"
"${private_run}" \
    "Content index preflight" \
    "${private_log_dir}/content-index-preflight.log" \
    docker exec severino-hq python manage.py sync_content_index --json

set --
for f in ${shipped}; do
    if is_render_unit "${f}"; then continue; fi
    units_install "${unit_dir}" "${systemd_dir}" "${f}"
    # What is enabled is every timer and path shipped: those are the units that
    # start work, and a service is only ever started by one of them.
    case "${f}" in */*) ;; *.timer | *.path) set -- "$@" "${f}" ;; esac
done
systemctl daemon-reload
systemctl enable --now "$@"

# Retire the old credential only after activation succeeds. The web UID owns
# the doorbell, never a directory containing root-sourced credentials.
rm -f /run/severino-hq/severino_controller_env
install -d -m 0755 /run/severino-hq
chown 10001:10001 /run/severino-hq
unit_committed=1
echo "Severino HQ controllers installed, preflighted, and enabled."
