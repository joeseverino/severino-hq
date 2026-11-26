#!/bin/sh
# What a composition is made of, as one digest.
#   tools/pipeline/composition-identity.sh HOST_IMAGE RUNNER_IMAGE STAGED
# The host and worker images by digest, and the build context
# compose_plugins.py staged in STAGED: every admitted wheel's digest, the
# merged lock, the policy, the enabled references and what the release may
# retire. A composition carries it as its dev.severino.hq.composition label,
# so a release whose inputs equal those of a pull request's verified
# composition promotes that one (tools/pipeline/proven-on-pr.sh). A hash of
# each file, never its contents: they name the extensions, and this prints
# to a public log.
set -eu

[ "$#" -eq 3 ] || { echo "usage: $0 HOST_IMAGE RUNNER_IMAGE STAGED" >&2; exit 2; }
staged=$3

# Gathered first, so a missing file fails here rather than hashing less.
listing=$(
  printf '%s\n%s\n' "$1" "$2"
  for name in digests plugin-lock.json policy-sha256 references plugin-retired.json; do
    sha256sum <"$staged/$name"
  done
)
printf '%s\n' "$listing" | sha256sum | cut -d' ' -f1
