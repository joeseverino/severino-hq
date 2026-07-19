#!/usr/bin/env sh
# Severino HQ container entrypoint.
#
# - Applies any pending migrations on boot.
# - Collects static files (WhiteNoise serves them from the ASGI app).
# - Then exec's whatever CMD was passed (Uvicorn by default).
#
# Intentionally minimal — we want boot failures to be loud and obvious.

set -eu

echo "[severino-hq] applying migrations…"
python manage.py migrate --noinput

echo "[severino-hq] collecting static files…"
python manage.py collectstatic --noinput

echo "[severino-hq] starting: $*"
exec "$@"
