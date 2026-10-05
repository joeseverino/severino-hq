# shellcheck shell=sh
# What a root script checks before it reads a rendered secret. Source this; do
# not execute it. The renderer itself is controller/cmd/hq-secrets.
#
# Contract:
#   secrets_trusted_file <file> <uid>     a regular single-link file <uid> owns
#   secrets_private_dir <dir> [uid]       a directory only <uid> can enter

# A regular file with one name, not a link, owned by <uid>: the only kind of
# destination root writes into in place, or reads a secret back from.
secrets_trusted_file() {
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    # GNU stat on the hosts, BSD stat on a Mac running the drills.
    [ "$(stat -c '%u %h' "$1" 2>/dev/null || stat -f '%u %l' "$1")" = "$2 1" ]
}

# A directory, not a link, that only <uid> (root by default) can enter.
secrets_private_dir() {
    [ -d "$1" ] && [ ! -L "$1" ] || return 1
    [ "$(stat -c '%u %a' "$1" 2>/dev/null || stat -f '%u %Lp' "$1")" = "${2:-0} 700" ]
}
