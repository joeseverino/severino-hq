#!/bin/sh
# The single local quality gate for HQ contributors and coding agents.
set -eu
unset CDPATH

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

# Optional, gitignored, and the difference between the composed pass running
# and being skipped. It supplies the three values that pass needs (an
# interpreter with the extensions importable, the path to reach them, and which
# to enable) so `./scripts/check.sh` with no arguments runs the whole gate
# instead of silently covering less. Nothing in it is committed, which is why
# the host can be told where the extensions are without naming them in source.
# See scripts/dev.env.example. Real environment variables still win.
if [ -f .env.dev ]; then
    set -a
    # shellcheck disable=SC1091  # optional, developer-local, absent in CI
    . ./.env.dev
    set +a
fi

# CHECK_PYTHON overrides the interpreter. The composed pass needs one that has
# the extensions importable, and this repository's own venv deliberately does
# not: the extensions are private and are never a dependency of the host. The
# interpreter that has them is whichever venv installed them: `hq dev` already
# knows which, and passes it here.
if [ -n "${CHECK_PYTHON:-}" ]; then
    python=$CHECK_PYTHON
elif [ -x .venv/bin/python ]; then
    python=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    python=$(command -v python3)
else
    echo "Python is required. Follow README.md#local-development." >&2
    exit 2
fi
if [ ! -x "$python" ] && ! command -v "$python" >/dev/null 2>&1; then
    echo "CHECK_PYTHON=$python is not executable." >&2
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

# The suite runs three times here, so its cost is the gate's cost. Workers get
# their own database file rather than the shared in-memory one Django would
# reach for (see core/test_runner.py) which is what makes this safe as well
# as roughly twice as fast. Set CHECK_PARALLEL=1 to rule it out when a failure
# looks order- or isolation-dependent.
parallel=${CHECK_PARALLEL:-auto}

echo "[check] Python syntax and lint"
"$ruff" check .

echo "[check] Django configuration and migration drift"
"$python" manage.py check
"$python" manage.py makemigrations --check --dry-run

echo "[check] Complete test suite"
"$python" manage.py test --noinput --parallel "$parallel"

# Again with DEBUG off, as production and the composed image run it. Some
# behaviour is chosen by that flag: plugin admission defaults to on without it.
echo "[check] Complete test suite (DEBUG off, as production runs it)"
# The host alone. The extension set is cleared rather than inherited from a dev
# server's environment; the composed run is the one below.
env -u DJANGO_DEBUG -u SEVERINO_HQ_PLUGINS \
    DJANGO_SECRET_KEY="${DJANGO_SECRET_KEY:-check-sh-not-a-real-secret}" \
    DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS:-testserver}" \
    "$python" manage.py test --noinput --parallel "$parallel"

# And once more with whatever extensions the caller supplies (PYTHONPATH and
# SEVERINO_HQ_PLUGINS), so the host and its extensions meet before compose does.
# Without them it is skipped.
if [ -n "${SEVERINO_HQ_PLUGINS:-}" ]; then
    echo "[check] Complete test suite (composed with the supplied plugin set)"
    # Admission off for this pass only. It proves a wheel was built and signed
    # by the workflow that claims it, from a lock file only compose produces;
    # a checkout cannot satisfy it. This pass asks whether the host still works
    # with every domain installed.
    env -u DJANGO_DEBUG \
        DJANGO_SECRET_KEY="${DJANGO_SECRET_KEY:-check-sh-not-a-real-secret}" \
        DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS:-testserver}" \
        SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION=0 \
        "$python" manage.py test --noinput --parallel "$parallel"
else
    echo "[check] Composed suite skipped (no SEVERINO_HQ_PLUGINS supplied)"
fi

if [ "${CHECK_BROWSER:-0}" = "1" ]; then
    echo "[check] Browser layout regressions (synthetic host fixtures)"
    env -u SEVERINO_HQ_PLUGINS "$python" manage.py test core.browser_tests --noinput --parallel 1
else
    echo "[check] Browser layout checks skipped (set CHECK_BROWSER=1 to enable)"
fi

echo "[check] Patch integrity"
git diff --check

if command -v actionlint >/dev/null 2>&1; then
    echo "[check] GitHub Actions"
    actionlint .github/workflows/*.yml
else
    echo "[check] actionlint unavailable; CI will run workflow validation"
fi

# The same two lists CI's lint job uses, from the same file, so this gate covers
# what that job covers.
. ./scripts/toolchain.env

echo "[check] Shell syntax"
# shellcheck disable=SC2086  # both lists are meant to split
set -- $SHELL_SOURCES
for shell_source; do
    case "$shell_source" in
        *backup.sh | *ci-local.sh) bash -n "$shell_source" ;;
        *) sh -n "$shell_source" ;;
    esac
done

if command -v shellcheck >/dev/null 2>&1; then
    echo "[check] Shell scripts"
    # shellcheck disable=SC2086
    shellcheck -x $SHELL_SOURCES
else
    echo "[check] shellcheck unavailable; CI will run shell validation"
fi

echo "[check] Shell suites"
# shellcheck disable=SC2086
for shell_suite in $SHELL_SUITES; do "$shell_suite"; done

echo "[check] all checks passed"
