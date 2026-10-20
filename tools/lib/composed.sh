# shellcheck shell=sh
# Whether the extension set mise.local.toml names was supplied. Without one it
# fails when REQUIRE_COMPOSED=1 says the composed suite is required.
#   composed_set || <what to do without an extension set>
composed_set() {
    [ -n "${HQ_LOCAL_PLUGINS:-}" ] && return 0
    if [ "${REQUIRE_COMPOSED:-0}" = 1 ]; then
        echo "The composed suite is required and HQ_LOCAL_PLUGINS is unset (tools/dev/mise.local.example.toml)." >&2
        exit 1
    fi
    return 1
}
