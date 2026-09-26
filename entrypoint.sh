#!/usr/bin/env sh
# Severino HQ container entrypoint.
#
# - Applies any pending migrations on boot.
# - Collects static files for the native ASGI static mount.
# - Then exec's whatever CMD was passed (Uvicorn by default).
#
# Intentionally minimal: we want boot failures to be loud and obvious.

set -eu

# The 1Password-rendered app env is loaded by config/settings.py (so exec'd
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

echo "[severino-hq] collecting static files…"
python manage.py collectstatic --noinput

echo "[severino-hq] starting: $*"
exec "$@"
