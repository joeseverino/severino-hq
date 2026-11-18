# shellcheck shell=sh
# The extension set a file task composes, as a checkout composes it: the
# plugins HQ_LOCAL_PLUGINS names, imported from the sources HQ_LOCAL_PYTHONPATH
# lists (mise.local.toml, or the environment, which wins), as the host tree
# can load them (tools/dev/extension_set.py, pick), admission off since a
# checkout has no admitted wheels. Sourced, then `compose_extensions [tree]`
# (this checkout by default), before anything else the task prints:
#   . ./tools/lib/extension-set.sh
#   compose_extensions
#
# The pick composes a repository's branch named as the one the host tree lands
# on where it carries one, as CI does; otherwise each package's tree as named
# when every package requires the plugin API the host tree speaks, else the
# newest commit of its repository that does, else no extension. Its line
# names what it composed and why. A heavy task runs its body again
# once it has the turn, with this run's environment, so the line is said once
# per run, on stderr, where a task whose output a program reads keeps it out.
# HQ_LOCAL_* stay as named, so a step this one starts picks for its own tree.
compose_extensions() {
    picked="$(PYTHONPATH='' uv run --quiet python -c 'from tools.dev.extension_set import shell; shell()' "${1:-.}")" || return 1
    PYTHONPATH="$(printf '%s\n' "${picked}" | sed -n 1p)"
    SEVERINO_HQ_PLUGINS="$(printf '%s\n' "${picked}" | sed -n 2p)"
    picked="$(printf '%s\n' "${picked}" | sed -n 3p)"
    SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION=0
    export SEVERINO_HQ_PLUGINS PYTHONPATH SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION
    [ -z "${HQ_EXTENSIONS_SAID:-}" ] || return 0
    HQ_EXTENSIONS_SAID=1
    export HQ_EXTENSIONS_SAID
    echo "${picked}" >&2
}
