#!/bin/sh
# Production-like local server: collected static assets + the real ASGI stack.
set -eu
unset CDPATH

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

# The same developer-local file the gate reads, on the same terms. It is where
# the extensions are named and where they are found, so a dev server that did
# not read it served the host alone -- every extension page a 404, every
# extension model unimportable, and nothing saying why. Real environment
# variables still win, and a checkout without the file is unaffected.
if [ -f .env.dev ]; then
    set -a
    # shellcheck disable=SC1091  # optional, developer-local, absent in CI
    . ./.env.dev
    set +a
fi

if [ -x .venv/bin/python ]; then
    python=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    python=$(command -v python3)
else
    echo "Python is required. Follow README.md#local-development." >&2
    exit 2
fi

# The signing key, from the password manager rather than from a file. Read at
# launch and never written to disk, so the read is what asks for a fingerprint.
# Without it settings generates a throwaway and sessions end at restart, so a
# machine without the tool still starts and is told what it is getting.
if [ -z "${DJANGO_SECRET_KEY:-}" ] && command -v op >/dev/null 2>&1; then
    DJANGO_SECRET_KEY=$(op read "${HQ_DEV_SECRET_REF:-op://Infrastructure/HQ Local/django secret key}" 2>/dev/null) || true
    export DJANGO_SECRET_KEY
fi
if [ -z "${DJANGO_SECRET_KEY:-}" ]; then
    echo "No signing key: sessions will use the constant in config/settings.py." >&2
fi

export DJANGO_DEBUG="${DJANGO_DEBUG:-1}"
export DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS:-localhost,127.0.0.1}"

# Loopback unless asked otherwise. `tailnet` resolves to this machine's tailnet
# address, which is what a proxy forwards to -- written as a word rather than
# an address so it survives the address changing, and so the file that asks for
# it does not have to be edited on a machine where it differs.
host=${HQ_DEV_HOST:-127.0.0.1}
if [ "${host}" = "tailnet" ]; then
    # Found rather than called. On a Mac `tailscale` is usually a shell alias
    # to the binary inside the app bundle, which a script does not inherit --
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
# different databases -- so a second server started here is not a second view
# of the same state, it is a different estate answering on a nearby port. The
# symptom is data that appears and disappears depending on which one answered.
if command -v lsof >/dev/null 2>&1 \
    && lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Something already serves port ${port}. If that is \`hq-dev serve\`, leave it." >&2
    echo "Otherwise stop it, or pass HQ_DEV_PORT to use another port." >&2
    exit 3
fi

"$python" manage.py collectstatic --noinput --verbosity 0
exec "$python" -m uvicorn config.asgi:application \
    --host "$host" \
    --port "$port" \
    --reload
