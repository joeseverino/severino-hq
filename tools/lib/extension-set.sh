# shellcheck shell=sh
# The extension set a file task composes, as a checkout composes it: the
# plugins HQ_LOCAL_PLUGINS names, imported from the sources HQ_LOCAL_PYTHONPATH
# lists (mise.local.toml, or the environment, which wins), admission off since
# a checkout has no admitted wheels. Sourced, then `compose_extensions`, before
# anything else the task prints:
#   . ./tools/lib/extension-set.sh
#   compose_extensions
#
# Its first line names the tree each package is imported from, since a host
# worktree composes the extensions' main checkouts unless HQ_LOCAL_PYTHONPATH
# names a worktree's. A heavy task runs its body again once it has the turn,
# with this run's environment, so the line is said once per run. Without a set
# it says the host runs alone.
compose_extensions() {
    SEVERINO_HQ_PLUGINS="${HQ_LOCAL_PLUGINS:-}"
    PYTHONPATH="${HQ_LOCAL_PYTHONPATH:-}"
    SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION=0
    export SEVERINO_HQ_PLUGINS PYTHONPATH SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION
    [ -z "${HQ_EXTENSIONS_SAID:-}" ] || return 0
    HQ_EXTENSIONS_SAID=1
    export HQ_EXTENSIONS_SAID
    if [ -z "${SEVERINO_HQ_PLUGINS}" ]; then
        echo "No HQ_LOCAL_PLUGINS supplied: the host alone, as the image runs it (mise run setup:extensions names a set)."
        return 0
    fi
    echo "Extensions composed from: $(printf '%s' "${PYTHONPATH}" | sed 's/:/, /g') (HQ_LOCAL_PYTHONPATH; one on the command line composes another tree)"
}
