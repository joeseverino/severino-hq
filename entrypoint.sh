#!/usr/bin/env sh
# Severino HQ container entrypoint.
#
# - Applies any pending migrations on boot.
# - Then exec's whatever CMD was passed (Uvicorn by default).
#
# Static assets and bytecode are the image's, made when it was built.
#
# Intentionally minimal: we want boot failures to be loud and obvious.

set -eu

# The 1Password-rendered app env is loaded by hq/config/settings.py (so exec'd
# processes get it too): nothing to source here.

# Temporary files outlive only the process that made them; whatever a killed
# process left is removed before anything can mistake it for current.
if [ -n "${TMPDIR:-}" ] && [ "${TMPDIR}" != /tmp ]; then
    mkdir -p "${TMPDIR}"
    chmod 700 "${TMPDIR}"
    find "${TMPDIR}" -mindepth 1 -delete
fi

echo "[severino-hq] applying migrations…"
python manage.py migrate --noinput

# A new image is the moment production changes what it runs, so delivery is
# read now rather than found later. Best effort: nothing about serving may
# wait on the controller.
python manage.py request_delivery_read || echo "[severino-hq] delivery read not requested"

echo "[severino-hq] starting: $*"
exec "$@"
