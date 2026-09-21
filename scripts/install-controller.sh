#!/bin/sh
# Install reviewed controller units, prove connectivity, then enable apply.

set -eu

readonly lib_dir="/usr/local/lib/severino-hq"
readonly unit_dir="${lib_dir}/deploy/systemd"
readonly systemd_dir="/etc/systemd/system"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/controller-env.sh
. "${script_dir}/lib/controller-env.sh"
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
    echo "severino-hq-sync-scripts is missing — run fix-root-ownership.sh --apply on this host first." >&2
    exit 1
fi
/usr/local/sbin/severino-hq-sync-scripts

# The verifier the *next* deploy will check this repository's signature with,
# refreshed by the same mechanism and from the same tree as the script that uses
# it. Pinned by digest inside, and a no-op when it is already correct, so this
# costs nothing on a host that is already right.
/usr/local/lib/severino-hq/scripts/install-cosign.sh

systemd-analyze verify \
    "${unit_dir}/severino-hq-secrets.service" \
    "${unit_dir}/severino-hq-controller.service" \
    "${unit_dir}/severino-hq-controller.timer" \
    "${unit_dir}/severino-hq-content-sync.service" \
    "${unit_dir}/severino-hq-content-sync.timer" \
    "${unit_dir}/severino-hq-backup.service" \
    "${unit_dir}/severino-hq-backup.timer"

# Restore the previous renderer unit if activation or a preflight fails.
umask 077
unit_backup="$(mktemp -d /run/severino-hq-unit.XXXXXX)"
readonly secrets_unit="${systemd_dir}/severino-hq-secrets.service"
if [ -f "${secrets_unit}" ]; then
    cp -p "${secrets_unit}" "${unit_backup}/previous"
fi
unit_committed=0
finish() {
    result=$?
    trap - EXIT
    if [ "${unit_committed}" -eq 0 ]; then
        if [ -f "${unit_backup}/previous" ]; then
            cp -p "${unit_backup}/previous" "${secrets_unit}"
        else
            rm -f "${secrets_unit}"
        fi
        systemctl daemon-reload || result=1
    fi
    rm -rf "${unit_backup}"
    exit "${result}"
}
trap finish EXIT
trap 'exit 1' HUP INT TERM
install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-secrets.service" "${secrets_unit}"
systemctl daemon-reload
systemctl start severino-hq-secrets.service
"${lib_dir}/scripts/provision-controller-ssh.sh"
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

install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-controller.service" \
    "${systemd_dir}/severino-hq-controller.service"
install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-controller.timer" \
    "${systemd_dir}/severino-hq-controller.timer"
install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-controller.path" \
    "${systemd_dir}/severino-hq-controller.path"
install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-content-sync.service" \
    "${systemd_dir}/severino-hq-content-sync.service"
install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-content-sync.timer" \
    "${systemd_dir}/severino-hq-content-sync.timer"
install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-backup.service" \
    "${systemd_dir}/severino-hq-backup.service"
install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-backup.timer" \
    "${systemd_dir}/severino-hq-backup.timer"
systemctl daemon-reload
systemctl enable --now \
    severino-hq-controller.path \
    severino-hq-controller.timer \
    severino-hq-content-sync.timer \
    severino-hq-backup.timer

# Retire the old credential only after activation succeeds. The web UID owns
# the doorbell, never a directory containing root-sourced credentials.
rm -f /run/severino-hq/severino_controller_env
install -d -m 0755 /run/severino-hq
chown 10001:10001 /run/severino-hq
unit_committed=1
echo "Severino HQ controllers installed, preflighted, and enabled."
