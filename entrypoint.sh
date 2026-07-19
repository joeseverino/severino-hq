#!/usr/bin/env sh
# Severino HQ container entrypoint.
#
# - Applies any pending migrations on boot.
# - Collects static files (WhiteNoise serves them from the ASGI app).
# - Then exec's whatever CMD was passed (Uvicorn by default).
#
# Intentionally minimal — we want boot failures to be loud and obvious.

set -eu

# Production bind-mounts the 1Password-rendered app env here; local dev has
# no mount (the bind default is /dev/null, which -f rejects) and keeps using
# whatever env the shell or compose provides.
if [ -f /run/secrets/severino_hq_env ]; then
    echo "[severino-hq] loading app env from mounted secret…"
    set -a
    . /run/secrets/severino_hq_env
    set +a
fi

echo "[severino-hq] applying migrations…"
python manage.py migrate --noinput

echo "[severino-hq] collecting static files…"
python manage.py collectstatic --noinput

echo "[severino-hq] starting: $*"
exec "$@"
