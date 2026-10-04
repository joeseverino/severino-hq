#!/usr/bin/env bash
# Run the CI gates that can run on a development machine, before pushing.
#
# `scripts/check.sh` answers "do my changes work?". This answers "will the
# pipeline accept them?": ruff and mypy at the pinned versions, the shell gates, the
# deployment check, the browser layout gate, CodeQL and Scorecard as code
# scanning runs them, the image build, and the suite inside that image.
#
# The tools come from scripts/toolchain.env, the same file CI reads. The list of
# gates does not: keep it in step with .github/workflows/ci.yml by hand. What
# cannot run locally is named at the end of each run rather than passed over.
#
# Usage:
#   scripts/ci-local.sh
#   CI_LOCAL_REQUIRE_ALL=1 scripts/ci-local.sh   # a gate that cannot run fails
#   PY=/path/to/python scripts/ci-local.sh
#   SEVERINO_CI_PYTHONS="/a/bin/python /b/bin/python" scripts/ci-local.sh
#
# CI runs a 3.13/3.14 matrix. Interpreters are named by path rather than
# by version because each needs the pinned requirements installed; a bare
# `python3.13` from PATH has no Django, and would report a failure that says
# more about this machine than about the change.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

# The same optional, gitignored file `scripts/check.sh` reads, so the composed
# set is stated once for both. See scripts/dev.env.example.
if [ -f .env.dev ]; then
  set -a
  # shellcheck disable=SC1091  # optional, developer-local, absent in CI
  . ./.env.dev
  set +a
fi

# Removed from the environment. CI sets this for exactly one step; leaving it
# set here would make every later step try to import extensions that are not
# on this interpreter's path.
unset SEVERINO_HQ_PLUGINS

# The same declaration CI reads: ruff pin, python matrix, coverage floor and
# the shell source list.
# shellcheck source=scripts/toolchain.env
. ./scripts/toolchain.env

PY="${PY:-.venv/bin/python}"
failed=0
skipped=()

step() { printf '\n\033[1m[ci-local]\033[0m %s\n' "$1"; }
# Warn when a pinned tool is not the pinned version. The point of this script is
# predicting the pipeline, and it cannot do that with a different linter than the
# pipeline runs.
pinned() { # pinned <tool> <pinned-version> <version-extractor>
  [ -n "$2" ] || return 0
  have="$(eval "$3" 2>/dev/null)"
  [ "$have" = "$2" ] || printf '  \033[33mwarn\033[0m    %s %s locally, CI pins %s\n' "$1" "${have:-unknown}" "$2"
}
ok()   { printf '  \033[32mok\033[0m      %s\n' "$1"; }
bad()  { printf '  \033[31mFAILED\033[0m  %s\n' "$1"; failed=1; }
skip() { printf '  \033[2mskip\033[0m    %s\n' "$1"; skipped+=("$1"); }

run() { # run <label> <command...>
  local label="$1"; shift
  if "$@" >/tmp/ci-local.$$ 2>&1; then ok "$label"; else
    bad "$label"; sed 's/^/      /' /tmp/ci-local.$$ | tail -25
  fi
  rm -f /tmp/ci-local.$$
}

# ---------------------------------------------------------------- checks
step "lint"
if command -v ruff >/dev/null; then
  pinned ruff "$RUFF_VERSION" "ruff --version | awk '{print \$2}'"
  run "ruff check ." ruff check .
else
  skip "ruff is not installed"
fi

# The typed seams (mypy.ini). Run on the interpreter with the requirements
# installed, because django-stubs loads the host's settings.
if "$PY" -c "import mypy, mypy_django_plugin" 2>/dev/null; then
  pinned mypy "$MYPY_VERSION" "'$PY' -m mypy --version | awk '{print \$2}'"
  run "mypy (the typed seams in mypy.ini)" "$PY" -m mypy
else
  skip "mypy is not installed on $PY (run: $PY -m pip install --require-hashes --no-deps -r requirements-tools.txt)"
fi

# shellcheck disable=SC2086  # both lists are meant to split
set -- $SHELL_SOURCES
if command -v shellcheck >/dev/null; then
  pinned shellcheck "$SHELLCHECK_VERSION" "shellcheck --version | awk '/^version:/{print \$2}'"
  run "shellcheck" shellcheck -x "$@"
else
  skip "shellcheck is not installed"
fi
run "bash -n" bash -n "$@"

# shellcheck disable=SC2086
for suite in $SHELL_SUITES; do
  run "$suite" "$suite"
done

# The same badge/matrix agreement CI's Checks job enforces.
# shellcheck disable=SC2086
expected_pythons="$(printf '%s\n' $PYTHON_VERSIONS | paste -sd'|' -)"
claimed_pythons="$(sed -nE 's/.*badge\/python-(.*)-blue.*/\1/p' README.md | head -1 | sed 's/%20%7C%20/|/g')"
if [ "$expected_pythons" = "$claimed_pythons" ]; then
  ok "README python badge agrees with the matrix ($expected_pythons)"
else
  bad "README python badge says '$claimed_pythons'; the matrix runs '$expected_pythons'"
fi

# ---------------------------------------------------------------- tests
# The badge quotes the oldest interpreter's coverage, so it is compared on that
# run and reported as not run only when no interpreter here is that version.
badge_python="${PYTHON_VERSIONS%% *}"
badge_checked=0
for python_bin in ${SEVERINO_CI_PYTHONS:-$PY}; do
  if [ ! -x "$python_bin" ]; then
    skip "$python_bin is not an executable interpreter"; continue
  fi
  if ! "$python_bin" -c "import django" 2>/dev/null; then
    skip "$python_bin has no Django installed"; continue
  fi
  step "test ($("$python_bin" --version 2>&1))"
  export DJANGO_DEBUG=1 DJANGO_SECRET_KEY=ci-only-secret-key-not-for-production
  export DJANGO_ALLOWED_HOSTS="127.0.0.1,testserver"
  run "manage.py check" "$python_bin" manage.py check
  SEVERINO_HQ_PLUGINS=example_hq_plugin.plugin:plugin \
    run "public plugin contract" "$python_bin" manage.py check
  run "makemigrations --check" "$python_bin" manage.py makemigrations --check --dry-run
  if "$python_bin" -c "import coverage" 2>/dev/null; then
    run "tests with coverage gate" sh -c \
      "SEVERINO_HQ_PLUGINS= '$python_bin' -m coverage run manage.py test --parallel auto >/dev/null 2>&1 && '$python_bin' -m coverage combine --quiet && '$python_bin' -m coverage report --fail-under=$COVERAGE_FLOOR >/dev/null"
    python_version="$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if [ "$python_version" = "$badge_python" ]; then
      run "README coverage badge (scripts/coverage-badge.sh)" scripts/coverage-badge.sh "$python_bin"
      badge_checked=1
    fi
  else
    run "tests" "$python_bin" manage.py test
    skip "coverage is not installed on $python_bin: gate not checked"
  fi
done
if [ "$badge_checked" -eq 0 ]; then
  # Not ok: nothing was compared, and a check that did not run is reported as
  # not run, never as green.
  skip "coverage badge not checked (no Python ${badge_python} interpreter with coverage)"
fi

# ------------------------------------------------------------- browser job
# Real-browser layout invariants over synthetic pages (core/browser_tests.py).
# Optional for check.sh, required here: a gate nobody runs rots.
step "browser"
browser_pin="$(sed -n 's/^playwright==\([^ ]*\).*/\1/p' requirements-browser.txt)"
browser_install="$PY -m pip install --require-hashes -r requirements-browser.txt && $PY -m playwright install chromium"
if ! "$PY" -c "import playwright" 2>/dev/null; then
  skip "playwright is not installed on $PY (run: $browser_install)"
elif ! "$PY" -c "from playwright.sync_api import sync_playwright as p
with p() as run: run.chromium.launch().close()" >/dev/null 2>&1; then
  skip "Chromium for playwright is not installed (run: $PY -m playwright install chromium)"
else
  pinned playwright "$browser_pin" "'$PY' -c 'import importlib.metadata as m; print(m.version(\"playwright\"))'"
  run "browser layout gate (core.browser_tests)" env \
    DJANGO_DEBUG=1 DJANGO_SECRET_KEY=ci-only-secret-key-not-for-production \
    DJANGO_ALLOWED_HOSTS="127.0.0.1,testserver" SEVERINO_LOG_LEVEL=CRITICAL \
    "$PY" manage.py test core.browser_tests --noinput --parallel 1
fi

# ---------------------------------------------------------- controller job
step "controller"
if command -v go >/dev/null; then
  pinned go "$GO_VERSION" "go env GOVERSION | sed -E 's/^go([0-9]+[.][0-9]+).*/\\1/'"
  run "controller/scripts/check.sh (Go checks)" controller/scripts/check.sh
else
  skip "go is not installed: controller checks not run"
fi

# ------------------------------------------------------------ security
step "security"
run "manage.py check --deploy --fail-level WARNING" env \
  DJANGO_DEBUG=0 \
  DJANGO_SECRET_KEY="ci-only-deploy-check-key-0123456789abcdef0123456789abcdef" \
  DJANGO_ALLOWED_HOSTS="hq.example.com" \
  SEVERINO_OIDC_ISSUER="https://sso.example.com" \
  DJANGO_BEHIND_TLS_PROXY=1 DJANGO_SESSION_COOKIE_SECURE=1 DJANGO_CSRF_COOKIE_SECURE=1 \
  DJANGO_HSTS_SECONDS=31536000 DJANGO_HSTS_INCLUDE_SUBDOMAINS=1 DJANGO_HSTS_PRELOAD=1 \
  "$PY" manage.py check --deploy --fail-level WARNING
if command -v pip-audit >/dev/null; then
  pinned pip-audit "$PIP_AUDIT_VERSION" "pip-audit --version | awk '{print \$2}'"
  run "pip-audit" pip-audit -r requirements.txt -r requirements-tools.txt -r requirements-browser.txt -r requirements-dev.txt
else
  skip "pip-audit is not installed"
fi

# ------------------------------------------------------- code scanning
# What GitHub code scanning reports after a push, reported here before it. The
# pinned CLIs are fetched on first use (scripts/install-scan-tools.sh).
step "code scanning"
run "CodeQL $CODEQL_VERSION, security-and-quality: no alerts" \
  env CHECK_PYTHON="$PY" scripts/security-scan.sh codeql
run "Scorecard $SCORECARD_VERSION, file-based checks at 10" \
  env CHECK_PYTHON="$PY" scripts/security-scan.sh scorecard

# ------------------------------------------------------- structural bar
# AGENTS.md's structural bar: no new near-duplicate outside tests, and the
# largest files only shrink (scripts/structural-baseline.txt).
step "structural bar"
if command -v codebase-memory-mcp >/dev/null 2>&1; then
  pinned codebase-memory-mcp "$CODEBASE_MEMORY_MCP_VERSION" "codebase-memory-mcp --version | awk '{print \$2}'"
  run "no new duplicate; the largest files did not grow" scripts/structural-bar.sh
else
  skip "structural bar: install codebase-memory-mcp to run it"
fi

# ------------------------------------------------- container + composition
step "container"
if docker info >/dev/null 2>&1; then
  run "docker build" docker build -q -t severino-hq:ci-local .
  run "image: Python 3.14, no package managers" docker run --rm --entrypoint python \
    severino-hq:ci-local -c \
    "import importlib.util, shutil, sys; sys.exit(0 if sys.version_info[:2] == (3, 14) and importlib.util.find_spec('pip') is None and shutil.which('pip') is None and shutil.which('uv') is None else 1)"
  run "image: manage.py check" docker run --rm --entrypoint python \
    --env DJANGO_SECRET_KEY=ci-only-composition-key-0123456789abcdef0123456789abcdef \
    --env DJANGO_ALLOWED_HOSTS=localhost severino-hq:ci-local manage.py check
  # requirements-dev.txt is never installed in the image.
  run "image: no development layer (debug_toolbar) is importable" docker run --rm \
    --entrypoint python severino-hq:ci-local -c \
    "import importlib.util, sys; sys.exit(importlib.util.find_spec('debug_toolbar') is not None)"
  run "image: carries the controller" sh -c \
    'docker run --rm --entrypoint /usr/local/bin/hq-controller severino-hq:ci-local -help 2>&1 | grep -q -- -apply'
  run "image: manage.py test" docker run --rm --entrypoint python \
    --env DJANGO_SECRET_KEY=ci-only-composition-key-0123456789abcdef0123456789abcdef \
    --env DJANGO_ALLOWED_HOSTS=localhost severino-hq:ci-local manage.py test --verbosity 0
else
  skip "no container runtime: image build and in-image suite not run"
fi

# ------------------------------------------------------------------ report
printf '\n'
if [ "${#skipped[@]}" -gt 0 ]; then
  printf '\033[2m[ci-local] not run: %s\033[0m\n' "${#skipped[@]}"
  printf '\033[2m  - %s\033[0m\n' "${skipped[@]}"
fi
cat <<'NOTE'
[ci-local] never covered here: image signing and registry push, the Trivy
  scan, composition against the real private extension set, and Scorecard's
  project-level checks (branch protection, code review, fuzzing, CII badge).
  Those need credentials, a registry or GitHub's view of the project.
NOTE
# scripts/preflight.sh requires every gate: one that could not run is a failure.
if [ "${CI_LOCAL_REQUIRE_ALL:-0}" = 1 ] && [ "${#skipped[@]}" -gt 0 ]; then
  printf '\033[31m[ci-local] %s gate(s) not run, and every gate is required\033[0m\n' "${#skipped[@]}"
  failed=1
fi
if [ "$failed" -ne 0 ]; then
  printf '\033[31m[ci-local] FAILED\033[0m\n'; exit 1
fi
printf '\033[32m[ci-local] every gate available locally passed\033[0m\n'
