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

# The check runs again under the host's gate verdict (tools/gate/verdict.py),
# whose last line names each failed test and the file under the host's
# var/gate that keeps them whole, with their tracebacks, under the package
# checkout's tree. It imports only its stdlib helpers beside it, run as a
# module from the host's root, so the host is on that one process's path and
# on no path the check below inherits.
if [ -z "${HQ_PLUGIN_CHECK_VERDICT:-}" ]; then
    plugin_root=$(pwd)
    cd "$hq_root"
    exec env HQ_PLUGIN_CHECK_VERDICT=1 HQ_GATE_TREE="$plugin_root" python3 -m tools.gate.verdict "plugin-check:$django_app" \
        "$hq_root/tools/pipeline/check-plugin.sh" --plugin-root "$plugin_root"
fi
if ! grep -q '^\[tool\.mypy\]' pyproject.toml; then
    echo "pyproject.toml declares no [tool.mypy]: every extension declares its type check there (docs/Plugins.md)." >&2
    exit 1
fi

# The host's structural rules for a test's read
# of a table cell or of a word a page lacks, and the shapes a shared part draws
# drawn by hand in a template (hand-drawn-*), hold the package's source as they
# hold the host's: each rule's scope reaches src/, read from the package root. The ast-grep is the
# host's pin, which mise installs where it is missing (an extension's CI sets
# up only uv), or the one on the PATH where mise has no pin. It runs here,
# since the scope is read from where it runs.
pinned() { mise -C "$hq_root" which ast-grep 2>/dev/null; }
ast_grep=$(pinned) || { mise -C "$hq_root" install --quiet ast-grep 2>/dev/null && ast_grep=$(pinned); } \
    || ast_grep=$(command -v ast-grep) || {
    echo "ast-grep is not installed: \`mise -C $hq_root install ast-grep\` installs the host's pin, or says why it cannot." >&2
    exit 1
}
"$ast_grep" scan -c "$hq_root/tools/lint/ast-grep/sgconfig.yml" --filter '^(cell-read-by-its-text|absent-text-read-from-the-text|hand-drawn-.+)$' \
    --error=unused-suppression --report-style medium src

work=$(mktemp -d "${TMPDIR:-/tmp}/hq-plugin-check.XXXXXX")
trap 'rm -rf -- "$work"' EXIT HUP INT TERM
host_requirements=$work/host-requirements.txt
# The runtime requirements, and the group of what the host's source tests
# import beyond them: the tests below run in this environment.
uv export --project "$hq_root" --locked --no-default-groups --group source-tests --no-emit-project \
    --output-file "$host_requirements" > /dev/null

uv sync --quiet --locked --group dev
python=$(uv run --no-sync python -c 'import sys; print(sys.executable)')
uv pip install --quiet --python "$python" --require-hashes -r "$host_requirements"
"$python" -m ruff check src
"$python" -m ruff format --check src
# What the package's source may import, whatever its own Ruff config selects.
"$python" -m ruff check --no-cache --config "$hq_root/tools/pipeline/extension-imports.toml" src
PYTHONPATH="$hq_root" "$python" -m hq_sdk.validation src

export DJANGO_DEBUG=true
# Each template read once, as the host's gates and production read them.
export DJANGO_TEMPLATE_CACHE=1
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
# django-stubs keys mypy's cache on INSTALLED_APPS alone, so a cache kept
# beside the package still names a host model module that has since moved;
# a cache of this run's own costs what a warm one does.
MYPYPATH="$hq_root" "$python" -m mypy --cache-dir "$work/mypy"
"$python" "$hq_root/manage.py" check
"$python" "$hq_root/manage.py" makemigrations --check --dry-run "$django_app"
# The template checks read every template Django loads, so they hold the
# plugin's wording and markup as they hold the host's; the view checks read
# every routed view's source, the plugin's included; the spelling check reads
# the package's project whole. A package keeps no copy of any of them.
"$python" "$hq_root/manage.py" test "$django_app" hq.platform.application.tests.test_plugins hq.platform.application.tests.test_rendered \
    hq.platform.application.tests.test_architecture_pages.InterfaceTextTests hq.platform.core.tests.test_counts.CountPlacementTests \
    hq.platform.core.tests.test_template_chips.TemplateChipTests hq.platform.core.tests.test_table_contract.TableContractTests \
    hq.platform.application.tests.test_reading_age.OneWordingTests \
    hq.platform.application.tests.test_script_loading hq.platform.application.tests.test_action_words.DeclaredWordsTests \
    hq.platform.application.tests.test_architecture_pages.PageTitleTests hq.platform.application.tests.test_architecture_primitives.OnePrimitiveTests \
    hq.platform.application.tests.test_architecture_primitives.FragmentPrimitiveTests \
    hq.platform.application.tests.test_shared_markup.SharedMarkupTemplateTests \
    hq.platform.core.tests.test_template_values.TemplateValueTests \
    hq.platform.core.tests.test_template_separators.TemplateSeparatorTests \
    hq.platform.application.tests.test_action_words.CallSiteWordsTests \
    hq.platform.application.tests.test_architecture_styles.StyleContractTests.test_a_rows_block_detail_opens_beneath_the_row \
    hq.platform.application.tests.test_view_writes.ViewWriteTests.test_no_view_writes_outside_the_chokepoint \
    hq.platform.application.tests.test_architecture.DeliveryAdapterArchitectureTests.test_paginated_list_views_use_the_shared_table_engine \
    hq.platform.application.tests.test_architecture_pages.PageTrailTests \
    hq.platform.application.tests.test_spelling.ExtensionSpellingTests

# Production installs admitted wheels with --no-deps. Recreate that exact
# dependency boundary in an isolated environment so an undeclared host pin
# fails here instead of after composition. Installing the package directory
# builds and installs its wheel, as `uv build` would.
uv venv --python "$python" "$work/venv"
uv pip install --quiet --python "$work/venv/bin/python" --require-hashes -r "$host_requirements"
uv pip install --quiet --python "$work/venv/bin/python" --no-deps .
uv pip check --python "$work/venv/bin/python"
"$work/venv/bin/python" "$hq_root/manage.py" check
