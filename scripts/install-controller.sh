#!/bin/sh
# Install reviewed controller units, prove connectivity, then enable apply.

set -eu

readonly app_dir="/opt/apps/severino-hq"
readonly unit_dir="${app_dir}/deploy/systemd"
readonly systemd_dir="/etc/systemd/system"
readonly env_file="${app_dir}/secrets/severino_controller_env"

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

"${app_dir}/scripts/run-controller.sh"
docker exec severino-hq python manage.py sync_content_index --json

install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-controller.service" \
    "${systemd_dir}/severino-hq-controller.service"
install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-controller.timer" \
    "${systemd_dir}/severino-hq-controller.timer"
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
    severino-hq-controller.timer \
    severino-hq-content-sync.timer \
    severino-hq-backup.timer

echo "Severino HQ controllers installed, preflighted, and enabled."
