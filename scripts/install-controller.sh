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
if [ ! -s "${env_file}" ]; then
    echo "Controller environment was not rendered." >&2
    exit 1
fi

systemd-analyze verify \
    "${unit_dir}/severino-hq-controller.service" \
    "${unit_dir}/severino-hq-controller.timer"

"${app_dir}/scripts/run-controller.sh"

install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-controller.service" \
    "${systemd_dir}/severino-hq-controller.service"
install -o root -g root -m 0644 \
    "${unit_dir}/severino-hq-controller.timer" \
    "${systemd_dir}/severino-hq-controller.timer"
systemctl daemon-reload
systemctl enable --now severino-hq-controller.timer

echo "Severino HQ controller installed and enabled."
