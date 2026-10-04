#!/bin/sh
# One local and CI contract check for every trusted HQ plugin.
#   check-plugin.sh [--plugin-root PATH]     # default: the current directory
# The plugin reference and Django app are read from the package
# (scripts/plugin-identity.py), where they are declared once.
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

hq_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
plugin_root=$(cd -- "$plugin_root" && pwd)
identity=$(python3 "$hq_root/scripts/plugin-identity.py" "$plugin_root")
plugin_reference=$(printf '%s\n' "$identity" | sed -n 's/^plugin-reference=//p')
django_app=$(printf '%s\n' "$identity" | sed -n 's/^django-app=//p')

env_path=${UV_PROJECT_ENVIRONMENT:-.venv}
case "$env_path" in
    /*) virtualenv=$env_path ;;
    *) virtualenv=$plugin_root/$env_path ;;
esac

cd "$plugin_root"
uv sync --frozen --group dev
host_requirements=$(mktemp "${TMPDIR:-/tmp}/hq-runtime.XXXXXX")
trap 'rm -f -- "$host_requirements"' EXIT HUP INT TERM
uv export --project "$hq_root" --locked --no-default-groups --no-emit-project \
    --output-file "$host_requirements" > /dev/null
uv pip install --python "$virtualenv/bin/python" --require-hashes -r "$host_requirements"
uv lock --check
"$virtualenv/bin/ruff" check src
PYTHONPATH="$hq_root" "$virtualenv/bin/python" -m hq_sdk.validation src

export DJANGO_DEBUG=true
export DJANGO_SETTINGS_MODULE=hq.config.settings
export PYTHONPATH="$hq_root"
export SEVERINO_HQ_PLUGINS="$plugin_reference"
"$virtualenv/bin/python" "$hq_root/manage.py" check
"$virtualenv/bin/python" "$hq_root/manage.py" check --tag interface --fail-level WARNING
"$virtualenv/bin/python" "$hq_root/manage.py" makemigrations --check --dry-run "$django_app"
"$virtualenv/bin/python" "$hq_root/manage.py" test "$django_app" hq.platform.application.tests.test_plugins hq.platform.application.tests.test_rendered

# Production installs admitted wheels with --no-deps. Recreate that exact
# dependency boundary in an isolated environment so an undeclared host pin
# fails here instead of after composition.
runtime_root=$(mktemp -d "${TMPDIR:-/tmp}/hq-plugin-runtime.XXXXXX")
trap 'rm -rf -- "$runtime_root"; rm -f -- "$host_requirements"' EXIT HUP INT TERM
uv build --wheel --out-dir "$runtime_root/dist"
set -- "$runtime_root"/dist/*.whl
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    echo "Expected exactly one plugin wheel." >&2
    exit 2
fi
wheel=$1
uv venv --python "$virtualenv/bin/python" "$runtime_root/venv"
uv pip install --python "$runtime_root/venv/bin/python" \
    --require-hashes -r "$host_requirements"
uv pip install --python "$runtime_root/venv/bin/python" --no-deps "$wheel"
uv pip check --python "$runtime_root/venv/bin/python"
"$runtime_root/venv/bin/python" "$hq_root/manage.py" check
