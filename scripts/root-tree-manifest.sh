#!/bin/sh
# Print the manifest of the tree root runs: a sha256sum line for every file
# under scripts/, config/ and deploy/, and for docker-compose.yml, sorted.
#
#   scripts/root-tree-manifest.sh DIR
#
# The image build writes it to /app/root-tree.sha256; severino-hq-sync-scripts
# refuses a synced tree that does not reproduce it, and severino-hq-check-scripts
# compares the installed tree against it daily. One generator, so the three
# cannot disagree about what the tree is.

set -eu

root="${1:?usage: root-tree-manifest.sh DIR}"
cd "${root}"
[ -f docker-compose.yml ] || { echo "No docker-compose.yml in ${root}." >&2; exit 1; }
for d in scripts config deploy; do
    [ -d "${d}" ] || { echo "No ${d}/ in ${root}." >&2; exit 1; }
done

{
    find scripts config deploy -type f \
        ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name '.DS_Store'
    echo docker-compose.yml
} | LC_ALL=C sort | while IFS= read -r f; do
    sha256sum -- "${f}"
done
