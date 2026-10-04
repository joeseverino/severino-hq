#!/bin/sh
# Production-like local server: collected static assets + the real ASGI stack.
set -eu
unset CDPATH

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

# Run as `mise run dev`: mise supplies mise.local.toml, which is where the
# extensions are named and found. Without it this serves the host alone.
if [ -z "${DJANGO_SECRET_KEY:-}" ] && command -v op >/dev/null 2>&1; then
    DJANGO_SECRET_KEY=$(op read "${HQ_DEV_SECRET_REF:-op://Infrastructure/HQ Local/django secret key}" 2>/dev/null) || true
    export DJANGO_SECRET_KEY
fi
if [ -z "${DJANGO_SECRET_KEY:-}" ]; then
    echo "No signing key: sessions will use the constant in hq/config/settings.py." >&2
fi

export DJANGO_DEBUG="${DJANGO_DEBUG:-1}"
export DJANGO_ALLOWED_HOSTS="${HQ_DEV_ALLOWED_HOSTS:-localhost,127.0.0.1}"
if [ -n "${HQ_DEV_CSRF_TRUSTED_ORIGINS:-}" ]; then
    export DJANGO_CSRF_TRUSTED_ORIGINS="${HQ_DEV_CSRF_TRUSTED_ORIGINS}"
fi
if [ -n "${HQ_LOCAL_PLUGINS:-}" ]; then
    export SEVERINO_HQ_PLUGINS="${HQ_LOCAL_PLUGINS}"
    export PYTHONPATH="${HQ_LOCAL_PYTHONPATH:-}"
fi

# Loopback unless asked otherwise. `tailnet` resolves to this machine's tailnet
# address, which is what a proxy forwards to: written as a word rather than
# an address so it survives the address changing, and so the file that asks for
# it does not have to be edited on a machine where it differs.
host=${HQ_DEV_HOST:-127.0.0.1}
if [ "${host}" = "tailnet" ]; then
    # Found rather than called. On a Mac `tailscale` is usually a shell alias
    # to the binary inside the app bundle, which a script does not inherit,
    # so asking for it by name works when typed and fails here.
    tailscale_bin=$(command -v tailscale 2>/dev/null \
        || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
    host=$("${tailscale_bin}" ip -4 2>/dev/null | head -1)
    if [ -z "${host}" ]; then
        echo "HQ_DEV_HOST=tailnet, but no tailnet address was found." >&2
        exit 2
    fi
fi
port=${HQ_DEV_PORT:-8000}

# Refuse rather than race. This binds loopback by default while the personal
# `hq-dev serve` command serves the proxied development host, and the two use
# different databases, so a second server started here is not a second view
# of the same state, it is a different estate answering on a nearby port. The
# symptom is data that appears and disappears depending on which one answered.
if command -v lsof >/dev/null 2>&1 \
    && lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Something already serves port ${port}. If that is \`hq-dev serve\`, leave it." >&2
    echo "Otherwise stop it, or pass HQ_DEV_PORT to use another port." >&2
    exit 3
fi

uv run --locked python manage.py collectstatic --noinput --verbosity 0
exec uv run --locked python -m uvicorn hq.config.asgi:application \
    --host "$host" \
    --port "$port" \
    --reload
