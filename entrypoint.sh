#!/usr/bin/env sh
# The container's boot: migrations first, keeping the database where the host
# says so a failed release can put it back, then the image's CMD. Any failure
# stops the boot.

set -eu

# Temporary files outlive only the process that made them; whatever a killed
# process left is removed before anything can mistake it for current.
if [ -n "${TMPDIR:-}" ] && [ "${TMPDIR}" != /tmp ]; then
    mkdir -p "${TMPDIR}"
    chmod 700 "${TMPDIR}"
    find "${TMPDIR}" -mindepth 1 -delete
fi

echo "[severino-hq] applying migrations…"
python manage.py boot_migrate

# A new image is the moment production changes what it runs, so delivery is
# read now rather than found later. Best effort: nothing about serving may
# wait on the controller.
python manage.py request_delivery_read || echo "[severino-hq] delivery read not requested"

echo "[severino-hq] starting: $*"
exec "$@"
