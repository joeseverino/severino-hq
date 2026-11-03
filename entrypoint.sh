#!/usr/bin/env sh
# The container's boot: migrations first, keeping the database where the host
# says so a failed release can put it back, then the image's CMD, with one
# Django setup before the server's own. A failure to migrate stops the boot;
# the delivery read `boot` asks for after it is best effort.

set -eu

# Temporary files outlive only the process that made them; whatever a killed
# process left is removed before anything can mistake it for current.
if [ -n "${TMPDIR:-}" ] && [ "${TMPDIR}" != /tmp ]; then
    mkdir -p "${TMPDIR}"
    chmod 700 "${TMPDIR}"
    find "${TMPDIR}" -mindepth 1 -delete
fi

echo "[severino-hq] applying migrations…"
python manage.py boot

echo "[severino-hq] starting: $*"
exec "$@"
