#!/bin/sh
# Root writes rendered secrets under a directory another account can replace.
# What it finds at a destination's name decides whether it writes: only a file
# of the target's own is written in place; a link, a hard link or someone
# else's file is refused, and nothing is followed or re-owned.

set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
fixture="$(mktemp -d)"
readonly fixture
trap 'rm -rf "${fixture}"' EXIT HUP INT TERM
# shellcheck source=scripts/lib/secrets.sh
. "${script_dir}/lib/secrets.sh"

me="$(id -u)"
failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

render() { printf 'SECRET=%s\n' "$1" >"${fixture}/src"; }
install_as() {
    # A subshell, because a refusal exits.
    (secrets_install_if_changed "${fixture}/src" "$1" "${me}" "$(id -g)" 600) 2>/dev/null
}

# A fresh destination is created, and an own file is rewritten in place.
render one
install_as "${fixture}/env" || fail "a fresh destination was refused"
inode="$(stat -c %i "${fixture}/env")"
render two
install_as "${fixture}/env" || fail "an own file was refused"
[ "$(cat "${fixture}/env")" = "SECRET=two" ] || fail "an own file was not rewritten"
[ "$(stat -c %i "${fixture}/env")" = "${inode}" ] || fail "an own file lost its inode (the bind mount would keep the old one)"

# A link planted at the name: not followed.
echo "system" >"${fixture}/system-file"
ln -s "${fixture}/system-file" "${fixture}/linked"
render three
install_as "${fixture}/linked" && fail "a symlink destination was written through"
[ "$(cat "${fixture}/system-file")" = "system" ] || fail "the link's target was overwritten"

# A hard link to another file: not written.
ln "${fixture}/system-file" "${fixture}/hardlinked"
install_as "${fixture}/hardlinked" && fail "a hard-linked destination was written"
[ "$(cat "${fixture}/system-file")" = "system" ] || fail "the hard link's other name was overwritten"

# A directory where a file should be: refused.
mkdir "${fixture}/dir"
install_as "${fixture}/dir" && fail "a directory destination was accepted"

# The reader's checks.
secrets_trusted_file "${fixture}/env" "${me}" || fail "an own file is not trusted"
secrets_trusted_file "${fixture}/linked" "${me}" && fail "a link is trusted"
secrets_trusted_file "${fixture}/env" 4242 && fail "another uid's file is trusted"
mkdir -m 700 "${fixture}/private"
secrets_private_dir "${fixture}/private" "${me}" || fail "a private directory is not trusted"
chmod 755 "${fixture}/private"
secrets_private_dir "${fixture}/private" "${me}" && fail "an open directory is trusted"
ln -s "${fixture}/private" "${fixture}/private-link"
secrets_private_dir "${fixture}/private-link" "${me}" && fail "a linked directory is trusted"

[ "${failures}" -eq 0 ] || exit 1
echo "Secret install drill passed."
