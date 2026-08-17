#!/bin/sh
# One local and CI contract check for every trusted HQ plugin.
set -eu
unset CDPATH

usage() {
    echo "usage: $0 --plugin-root PATH --plugin-reference MODULE:ATTRIBUTE --django-app APP" >&2
    exit 2
}

plugin_root=
plugin_reference=
django_app=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --plugin-root) [ "$#" -ge 2 ] || usage; plugin_root=$2; shift 2 ;;
        --plugin-reference) [ "$#" -ge 2 ] || usage; plugin_reference=$2; shift 2 ;;
        --django-app) [ "$#" -ge 2 ] || usage; django_app=$2; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$plugin_root" ] && [ -n "$plugin_reference" ] && [ -n "$django_app" ] || usage

hq_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
plugin_root=$(cd -- "$plugin_root" && pwd)
if [ ! -f "$plugin_root/pyproject.toml" ] || [ ! -d "$plugin_root/src/$django_app" ]; then
    echo "Plugin root must contain pyproject.toml and src/$django_app." >&2
    exit 2
fi

env_path=${UV_PROJECT_ENVIRONMENT:-.venv}
case "$env_path" in
    /*) virtualenv=$env_path ;;
    *) virtualenv=$plugin_root/$env_path ;;
esac

cd "$plugin_root"
uv sync --frozen --group dev
uv pip install --python "$virtualenv/bin/python" --require-hashes -r "$hq_root/requirements.txt"
uv lock --check
"$virtualenv/bin/ruff" check src
PYTHONPATH="$hq_root" "$virtualenv/bin/python" -m hq_sdk.validation src

export DJANGO_DEBUG=true
export DJANGO_SETTINGS_MODULE=config.settings
export PYTHONPATH="$hq_root"
export SEVERINO_HQ_PLUGINS="$plugin_reference"
"$virtualenv/bin/python" "$hq_root/manage.py" check
"$virtualenv/bin/python" "$hq_root/manage.py" makemigrations --check --dry-run "$django_app"
"$virtualenv/bin/python" "$hq_root/manage.py" test "$django_app" application.test_plugins

# Production installs admitted wheels with --no-deps. Recreate that exact
# dependency boundary in an isolated environment so an undeclared host pin
# fails here instead of after composition.
runtime_root=$(mktemp -d "${TMPDIR:-/tmp}/hq-plugin-runtime.XXXXXX")
trap 'rm -rf -- "$runtime_root"' EXIT HUP INT TERM
uv build --wheel --out-dir "$runtime_root/dist"
set -- "$runtime_root"/dist/*.whl
[ "$#" -eq 1 ] && [ -f "$1" ] || {
    echo "Expected exactly one plugin wheel." >&2
    exit 2
}
wheel=$1
uv venv --python "$virtualenv/bin/python" "$runtime_root/venv"
uv pip install --python "$runtime_root/venv/bin/python" \
    --require-hashes -r "$hq_root/requirements.txt"
uv pip install --python "$runtime_root/venv/bin/python" --no-deps "$wheel"
uv pip check --python "$runtime_root/venv/bin/python"
"$runtime_root/venv/bin/python" "$hq_root/manage.py" check
