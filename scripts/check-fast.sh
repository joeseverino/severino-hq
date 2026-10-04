#!/bin/sh
# The inner-loop gate: the tests the change can reach, plus ruff, migration
# drift and the architecture tests. scripts/check.sh is still the gate before a
# push; this is not a substitute for it.
set -eu
unset CDPATH

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

if [ -f .env.dev ]; then
    set -a
    # shellcheck disable=SC1091  # optional, developer-local, absent in CI
    . ./.env.dev
    set +a
fi

if [ -n "${CHECK_PYTHON:-}" ]; then
    python=$CHECK_PYTHON
elif [ -x .venv/bin/python ]; then
    python=.venv/bin/python
else
    python=$(command -v python3 || true)
fi
if [ -z "$python" ]; then
    echo "Python is required. Follow README.md#local-development." >&2
    exit 2
fi

if [ -x .venv/bin/ruff ]; then
    ruff=.venv/bin/ruff
elif command -v ruff >/dev/null 2>&1; then
    ruff=$(command -v ruff)
else
    echo "ruff is required (install the development toolchain first)." >&2
    exit 2
fi

FAST_PYTHON=$python FAST_RUFF=$ruff exec "$python" scripts/fast_gate.py "$@"
