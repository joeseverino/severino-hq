#!/bin/sh
# Install reviewed controller units, prove connectivity, then enable apply.

set -eu

readonly app_dir="/opt/apps/severino-hq"
readonly unit_dir="${app_dir}/deploy/systemd"
readonly systemd_dir="/etc/systemd/system"
readonly env_file="${app_dir}/secrets/severino_controller_env"
readonly private_log_dir="/var/log/severino-hq"
readonly private_run="${app_dir}/scripts/run-private.sh"

if [ "$(id -u)" -ne 0 ]; then
    echo "install-controller.sh must run as root." >&2
    exit 1
fi

systemctl start severino-hq-secrets.service
"${app_dir}/scripts/provision-controller-ssh.sh"
if [ ! -s "${env_file}" ]; then
    echo "Controller environment was not rendered." >&2
    exit 1
fi

systemd-analyze verify \
    "${unit_dir}/severino-hq-controller.service" \
    "${unit_dir}/severino-hq-controller.timer" \
    "${unit_dir}/severino-hq-content-sync.service" \
    "${unit_dir}/severino-hq-content-sync.timer" \
    "${unit_dir}/severino-hq-backup.service" \
    "${unit_dir}/severino-hq-backup.timer"

# These commands intentionally return rich machine JSON: locally useful,
# inappropriate in the public Actions stream inherited by a self-hosted deploy.
# Keep each latest result on the host with root-only permissions and expose only
# a fixed status line plus the original exit code to the deployment gate.
install -d -o root -g root -m 0700 "${private_log_dir}"
"${private_run}" \
    "Controller connection preflight" \
    "${private_log_dir}/controller-preflight.log" \
    "${app_dir}/scripts/run-controller.sh"
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
# Watches the doorbell, so pressing Save applies now rather than within a
# minute. The directory has to exist before the unit starts watching it, and
# compose creates it as the bind mount's source on first boot.
install -d -o root -g root -m 0755 /run/severino-hq
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

echo "Severino HQ controllers installed, preflighted, and enabled."
