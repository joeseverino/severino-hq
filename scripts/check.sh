#!/bin/sh
# The single local quality gate for HQ contributors and coding agents.
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

if [ -x .venv/bin/ruff ]; then
    ruff=.venv/bin/ruff
elif command -v ruff >/dev/null 2>&1; then
    ruff=$(command -v ruff)
else
    echo "ruff is required (install the development toolchain first)." >&2
    exit 2
fi

export DJANGO_DEBUG=true
export SEVERINO_LOG_LEVEL=CRITICAL

echo "[check] Python syntax and lint"
"$ruff" check .

echo "[check] Django configuration and migration drift"
"$python" manage.py check
"$python" manage.py makemigrations --check --dry-run

echo "[check] Complete test suite"
"$python" manage.py test --noinput

echo "[check] Patch integrity"
git diff --check

if command -v actionlint >/dev/null 2>&1; then
    echo "[check] GitHub Actions"
    actionlint .github/workflows/*.yml
else
    echo "[check] actionlint unavailable; CI will run workflow validation"
fi

if command -v shellcheck >/dev/null 2>&1; then
    echo "[check] Shell scripts"
    shellcheck scripts/*.sh entrypoint.sh
else
    echo "[check] shellcheck unavailable; CI will run shell validation"
fi

echo "[check] all checks passed"
