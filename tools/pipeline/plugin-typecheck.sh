#!/bin/sh
# The type check an extension declares in its pyproject.toml's [tool.mypy],
# run with its own mypy against the host's typed hq_sdk. From the package root,
# in check-plugin.sh's environment (SEVERINO_HQ_PLUGINS names the package).
#   tools/pipeline/plugin-typecheck.sh PYTHON
# django-stubs settings: the package names `hq.config.settings_typecheck`.
set -eu
unset CDPATH

[ "$#" -eq 1 ] || { echo "usage: $0 PYTHON" >&2; exit 2; }
python=$1
hq_root=$(cd -- "$(dirname -- "$0")/../.." && pwd)

declared=$("$python" -c 'import tomllib; print("mypy" in tomllib.load(open("pyproject.toml", "rb")).get("tool", {}))')
if [ "$declared" != True ]; then
    echo "pyproject.toml declares no [tool.mypy]: no type check to run."
    exit 0
fi

MYPYPATH=$hq_root PYTHONPATH=$hq_root exec "$python" -m mypy
