#!/bin/sh
# Put a verifier where root can trust it.
#
# The deploy verifies the composition's signature twice: once on the runner, so
# a bad image fails visibly in the workflow, and once inside deploy-image.sh,
# because the runner is the party the sudoers boundary exists to distrust. The
# second check is the one that decides, so it cannot use the runner's copy of
# cosign -- that binary is installed by an Action, into a directory the runner
# owns and can rewrite. Root needs its own.
#
# Pinned to a version and its published SHA-256. A download root then executes
# is only as trustworthy as what pins it, so the digest is checked before the
# file is ever made executable, and a mismatch leaves nothing behind.
#
# Idempotent: an already-correct binary is left alone, so this can run on every
# controller install without a network round trip.

set -eu

readonly version="v3.1.3"
readonly sha256="4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"
readonly lib_dir="${SEVERINO_HQ_LIB_DIR:-/usr/local/lib/severino-hq}"
readonly target="${lib_dir}/bin/cosign"
readonly url="https://github.com/sigstore/cosign/releases/download/${version}/cosign-linux-amd64"

if [ "$(id -u)" -ne 0 ]; then
    echo "install-cosign.sh must run as root." >&2
    exit 1
fi

digest_of() {
    sha256sum "${1}" | cut -d' ' -f1
}

if [ -x "${target}" ] && [ "$(digest_of "${target}")" = "${sha256}" ]; then
    exit 0
fi

install -d -o root -g root -m 0755 "${lib_dir}/bin"
staged="$(mktemp "${lib_dir}/bin/.cosign.XXXXXX")"
trap 'rm -f "${staged}"' EXIT HUP INT TERM
curl -fsSL --retry 3 --max-time 180 "${url}" -o "${staged}"

actual="$(digest_of "${staged}")"
if [ "${actual}" != "${sha256}" ]; then
    echo "Refusing to install cosign ${version}: expected ${sha256}, got ${actual}." >&2
    exit 1
fi

# Only now is it executable, and only now does it replace what was there.
chmod 0755 "${staged}"
chown root:root "${staged}"
mv -f "${staged}" "${target}"
trap - EXIT HUP INT TERM
echo "Installed cosign ${version} at ${target}."
