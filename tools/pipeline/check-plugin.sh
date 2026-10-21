#!/bin/sh
# One extension package's contract check against this host, as its CI runs
# it. Run by path where there is no mise (the installer image, the reusable
# workflow); `mise run plugin:check` is the same program.
#   tools/pipeline/check-plugin.sh [--plugin-root PATH]
# The plugin reference and Django app are read from the package
# (tools/pipeline/plugin_identity.py), where they are declared once.
set -eu
unset CDPATH

usage() {
    echo "usage: $0 [--plugin-root PATH]" >&2
    exit 2
}

plugin_root=.
while [ "$#" -gt 0 ]; do
    case "$1" in
        --plugin-root) [ "$#" -ge 2 ] || usage; plugin_root=$2; shift 2 ;;
        *) usage ;;
    esac
done

hq_root=$(cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$plugin_root"
identity=$(PYTHONPATH="$hq_root" python3 -P -m tools.pipeline.plugin_identity . ${PLUGIN_REPOSITORY:+--repository "$PLUGIN_REPOSITORY"})
plugin_reference=$(printf '%s\n' "$identity" | sed -n 's/^plugin-reference=//p')
django_app=$(printf '%s\n' "$identity" | sed -n 's/^django-app=//p')
if ! grep -q '^\[tool\.mypy\]' pyproject.toml; then
    echo "pyproject.toml declares no [tool.mypy]: every extension declares its type check there (docs/Plugins.md)." >&2
    exit 1
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/hq-plugin-check.XXXXXX")
trap 'rm -rf -- "$work"' EXIT HUP INT TERM
host_requirements=$work/host-requirements.txt
uv export --project "$hq_root" --locked --no-default-groups --no-emit-project \
    --output-file "$host_requirements" > /dev/null

uv sync --locked --group dev
python=$(uv run --no-sync python -c 'import sys; print(sys.executable)')
uv pip install --python "$python" --require-hashes -r "$host_requirements"
"$python" -m ruff check src
PYTHONPATH="$hq_root" "$python" -m hq_sdk.validation src

export DJANGO_DEBUG=true
export DJANGO_SETTINGS_MODULE=hq.config.settings
# A check never opens the host checkout's own database.
export SEVERINO_DATABASE_PATH="$work/check.sqlite3"
# The settings refuse to load without a signing key. A check signs nothing, and
# this one exists for its processes alone.
DJANGO_SECRET_KEY="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
export DJANGO_SECRET_KEY
export PYTHONPATH="$hq_root"
export SEVERINO_HQ_PLUGINS="$plugin_reference"
# The package's own mypy against the host's typed hq_sdk; its django-stubs
# settings are hq.config.settings_typecheck, with this package installed.
MYPYPATH="$hq_root" "$python" -m mypy
"$python" "$hq_root/manage.py" check
"$python" "$hq_root/manage.py" makemigrations --check --dry-run "$django_app"
# InterfaceTextTests reads the plugin's wording and markup with the host's.
"$python" "$hq_root/manage.py" test "$django_app" hq.platform.application.tests.test_plugins hq.platform.application.tests.test_rendered \
    hq.platform.application.tests.test_architecture.InterfaceTextTests

# Production installs admitted wheels with --no-deps. Recreate that exact
# dependency boundary in an isolated environment so an undeclared host pin
# fails here instead of after composition. Installing the package directory
# builds and installs its wheel, as `uv build` would.
uv venv --python "$python" "$work/venv"
uv pip install --python "$work/venv/bin/python" --require-hashes -r "$host_requirements"
uv pip install --python "$work/venv/bin/python" --no-deps .
uv pip check --python "$work/venv/bin/python"
"$work/venv/bin/python" "$hq_root/manage.py" check
