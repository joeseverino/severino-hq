#!/bin/sh
# Production-like local server: collected static assets + the real ASGI stack.
set -eu
unset CDPATH

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

if [ -x .venv/bin/python ]; then
    python=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    python=$(command -v python3)
else
    echo "Python is required. Follow README.md#local-development." >&2
    exit 2
fi

export DJANGO_DEBUG="${DJANGO_DEBUG:-1}"
export DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS:-localhost,127.0.0.1}"

host=${HQ_DEV_HOST:-127.0.0.1}
port=${HQ_DEV_PORT:-8000}

"$python" manage.py collectstatic --noinput --verbosity 0
exec "$python" -m uvicorn config.asgi:application \
    --host "$host" \
    --port "$port" \
    --reload
