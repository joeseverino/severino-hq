# shellcheck shell=sh
# What a root script checks before it reads a rendered secret. Source this; do
# not execute it. The renderer itself is controller/cmd/hq-secrets.
#
# Contract:
#   secrets_trusted_file <file> <uid>     a regular single-link file <uid> owns
#   secrets_private_dir <dir> [uid]       a directory only <uid> can enter
#   secrets_app_env_dir <tmpfs> <checkout> where the application environment is

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

# The directory holding the rendered application environment: the tmpfs copy
# once hq-secrets has rendered one, and the checkout's only on a host
# no refresh has reached yet. The order deploy-image.sh binds in, which matters
# because the deploy that binds the tmpfs copy deletes the checkout's: a reader
# that knew only the checkout would lose its file to a successful deploy.
#   secrets_app_env_dir <tmpfs web dir> <checkout secret dir>
secrets_app_env_dir() {
    if [ -e "$1/severino_hq_env" ] || [ -L "$1/severino_hq_env" ]; then
        printf '%s\n' "$1"
    else
        printf '%s\n' "$2"
    fi
}
