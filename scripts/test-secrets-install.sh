#!/bin/sh
# Root reads rendered secrets from under a directory another account can
# replace. What it finds at a file's name decides whether it reads: only a file
# of the expected account's own, in a directory only its owner can enter. The
# renderer's own half of these checks is controller/secrets/install.

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

printf 'SECRET=one\n' >"${fixture}/env"
echo "system" >"${fixture}/system-file"
ln -s "${fixture}/system-file" "${fixture}/linked"
ln "${fixture}/system-file" "${fixture}/hardlinked"
mkdir "${fixture}/dir"

secrets_trusted_file "${fixture}/env" "${me}" || fail "an own file is not trusted"
secrets_trusted_file "${fixture}/linked" "${me}" && fail "a link is trusted"
secrets_trusted_file "${fixture}/hardlinked" "${me}" && fail "a hard-linked file is trusted"
secrets_trusted_file "${fixture}/dir" "${me}" && fail "a directory is trusted as a file"
secrets_trusted_file "${fixture}/absent" "${me}" && fail "a missing file is trusted"
secrets_trusted_file "${fixture}/env" 4242 && fail "another uid's file is trusted"
mkdir -m 700 "${fixture}/private"
secrets_private_dir "${fixture}/private" "${me}" || fail "a private directory is not trusted"
secrets_private_dir "${fixture}/private" 4242 && fail "another uid's directory is trusted"
chmod 755 "${fixture}/private"
secrets_private_dir "${fixture}/private" "${me}" && fail "an open directory is trusted"
chmod 700 "${fixture}/private"
ln -s "${fixture}/private" "${fixture}/private-link"
secrets_private_dir "${fixture}/private-link" "${me}" && fail "a linked directory is trusted"

[ "${failures}" -eq 0 ] || exit 1
echo "Secret reader drill passed."
