#!/usr/bin/env bash
# Run the CI gates that can run on a development machine, before pushing.
#
# `scripts/check.sh` answers "do my changes work?". This answers "will the
# pipeline accept them?" -- ruff at the pinned version, the shell gates, the
# deployment check, the image build, and the suite inside that image.
#
# The tools come from scripts/toolchain.env, the same file CI reads. The list of
# gates does not: keep it in step with .github/workflows/ci.yml by hand. What
# cannot run locally is named at the end of each run rather than passed over.
#
# Usage:
#   scripts/ci-local.sh
#   PY=/path/to/python scripts/ci-local.sh
#   SEVERINO_CI_PYTHONS="/a/bin/python /b/bin/python" scripts/ci-local.sh
#
# CI runs a 3.12/3.13/3.14 matrix. Interpreters are named by path rather than
# by version because each needs the pinned requirements installed; a bare
# `python3.12` from PATH has no Django, and would report a failure that says
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

# Read once, then removed from the environment. CI sets this for exactly one
# step; leaving it set here would make every later step try to import
# extensions that are not on this interpreter's path.
EXTENSION_REFS="${SEVERINO_HQ_PLUGINS:-}"
unset SEVERINO_HQ_PLUGINS

# The same declaration CI reads: ruff pin, python matrix, coverage floor and
# the shell source list.
# shellcheck source=scripts/toolchain.env
. ./scripts/toolchain.env

PY="${PY:-.venv/bin/python}"
failed=0
skipped=()

step() { printf '\n\033[1m[ci-local]\033[0m %s\n' "$1"; }
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

# ---------------------------------------------------------------- lint job
step "lint"
if command -v ruff >/dev/null; then
  have="$(ruff --version | awk '{print $2}')"
  [ "$have" = "$RUFF_VERSION" ] || printf '  \033[33mwarn\033[0m    ruff %s locally, CI pins %s\n' "$have" "$RUFF_VERSION"
  run "ruff check ." ruff check .
else
  skip "ruff is not installed"
fi

# shellcheck disable=SC2086  # both lists are meant to split
set -- $SHELL_SOURCES
if command -v shellcheck >/dev/null; then
  run "shellcheck" shellcheck -x "$@"
else
  skip "shellcheck is not installed"
fi
run "bash -n" bash -n "$@"

# shellcheck disable=SC2086
for suite in $SHELL_SUITES; do
  run "$suite" "$suite"
done

# The same badge/matrix agreement CI enforces in its lint job.
# shellcheck disable=SC2086
expected_pythons="$(printf '%s\n' $PYTHON_VERSIONS | paste -sd'|' -)"
claimed_pythons="$(sed -nE 's/.*badge\/python-(.*)-blue.*/\1/p' README.md | head -1 | sed 's/%20%7C%20/|/g')"
if [ "$expected_pythons" = "$claimed_pythons" ]; then
  ok "README python badge agrees with the matrix ($expected_pythons)"
else
  bad "README python badge says '$claimed_pythons'; the matrix runs '$expected_pythons'"
fi

# The private-name check CI runs from the COMPOSITION_EXTENSIONS variable. The
# variable is not available locally, so the same grep runs against whatever
# extension checkouts SEVERINO_HQ_PLUGINS names -- which is the set a developer
# actually has, and the one they might accidentally mention.
if [ -n "$EXTENSION_REFS" ]; then
  leaked=0
  for ref in ${EXTENSION_REFS//,/ }; do
    stem="${ref%%.*}"
    for form in "$stem" "${stem//_/-}" "${stem//_/.}"; do
      hits="$(git grep -Iril -e "$form" -- . ':!LICENSE' || true)"
      if [ -n "$hits" ]; then
        bad "an extension identifier appears in tracked files: $form"
        printf '%s\n' "$hits" | sed 's/^/      /'
        leaked=1
      fi
    done
  done
  [ "$leaked" -eq 0 ] && ok "no extension identifiers in the tracked tree"
else
  skip "SEVERINO_HQ_PLUGINS unset — cannot check for extension identifiers"
fi

# ---------------------------------------------------------------- test job
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
      "SEVERINO_HQ_PLUGINS= '$python_bin' -m coverage run manage.py test >/dev/null 2>&1 && '$python_bin' -m coverage report --fail-under=$COVERAGE_FLOOR >/dev/null"
    measured="$("$python_bin" -m coverage report --format=total 2>/dev/null || echo '')"
    claimed="$(sed -nE 's/.*coverage-([0-9]+)%25-.*/\1/p' README.md | head -1)"
    python_version="$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    badge_python="${PYTHON_VERSIONS%% *}"
    if [ "$python_version" != "$badge_python" ]; then
      ok "coverage measured ${measured}% (the README quotes Python ${badge_python})"
    elif [ -n "$measured" ] && [ "$measured" != "$claimed" ]; then
      bad "README badge says ${claimed}%, this run measured ${measured}%"
    elif [ -n "$measured" ]; then
      ok "README coverage badge agrees with the measured ${measured}%"
    fi
  else
    run "tests" "$python_bin" manage.py test
    skip "coverage is not installed — gate and badge not checked"
  fi
done

# ------------------------------------------------------------ security job
step "security"
run "manage.py check --deploy --fail-level WARNING" env \
  DJANGO_DEBUG=0 \
  DJANGO_SECRET_KEY="ci-only-deploy-check-key-0123456789abcdef0123456789abcdef" \
  DJANGO_ALLOWED_HOSTS="hq.example.com" \
  DJANGO_BEHIND_TLS_PROXY=1 DJANGO_SESSION_COOKIE_SECURE=1 DJANGO_CSRF_COOKIE_SECURE=1 \
  DJANGO_HSTS_SECONDS=31536000 DJANGO_HSTS_INCLUDE_SUBDOMAINS=1 DJANGO_HSTS_PRELOAD=1 \
  "$PY" manage.py check --deploy --fail-level WARNING
if command -v pip-audit >/dev/null; then
  run "pip-audit" pip-audit -r requirements.txt
else
  skip "pip-audit is not installed"
fi

# ------------------------------------------------- container + composition
step "container"
if docker info >/dev/null 2>&1; then
  run "docker build" docker build -q -t severino-hq:ci-local .
  run "image: manage.py check" docker run --rm --entrypoint python \
    --env DJANGO_SECRET_KEY=ci-only-composition-key-0123456789abcdef0123456789abcdef \
    --env DJANGO_ALLOWED_HOSTS=localhost severino-hq:ci-local manage.py check
  run "image: manage.py test" docker run --rm --entrypoint python \
    --env DJANGO_SECRET_KEY=ci-only-composition-key-0123456789abcdef0123456789abcdef \
    --env DJANGO_ALLOWED_HOSTS=localhost severino-hq:ci-local manage.py test --verbosity 0
else
  skip "no container runtime — image build and in-image suite not run"
fi

# ------------------------------------------------------------------ report
printf '\n'
if [ "${#skipped[@]}" -gt 0 ]; then
  printf '\033[2m[ci-local] not run: %s\033[0m\n' "${#skipped[@]}"
  printf '\033[2m  - %s\033[0m\n' "${skipped[@]}"
fi
cat <<'NOTE'
[ci-local] never covered here: CodeQL, image signing and registry push, the
  Trivy scan, and composition against the real private extension set. Those
  need credentials or a registry and only run in the pipeline.
NOTE
if [ "$failed" -ne 0 ]; then
  printf '\033[31m[ci-local] FAILED\033[0m\n'; exit 1
fi
printf '\033[32m[ci-local] every gate available locally passed\033[0m\n'
