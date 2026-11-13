#!/usr/bin/env bash
# That each image CI built (the host image, the automation worker's) was signed
# by REPOSITORY's own ci workflow.
#   tools/pipeline/host-signature.sh REPOSITORY IMAGE...
# A tag derived from a commit says who should have built an image, not who
# did, so an image pushed to the same name by anything else fails here rather
# than becoming part of a production composition. IS_RELEASE=true admits
# only a build of main; anything else never publishes, so it may also compose
# onto a pull request's build.
set -euo pipefail

[ "$#" -ge 2 ] || { echo "usage: $0 REPOSITORY IMAGE..." >&2; exit 2; }
repository=$1
shift
if [ "${IS_RELEASE:-false}" = true ]; then
  signer_ref='refs/heads/main$'
else
  signer_ref='refs/(heads/main|pull/[0-9]+/merge)$'
fi

# Retried, because CI pushes the images and signs them in two steps and
# host-image.sh ends its wait at the push, so a manifest can exist for a few
# seconds before its signature does: 12 tries 15 seconds apart, for each image.
signed() {
  local image=$1 attempt
  for attempt in $(seq 1 12); do
    if cosign verify \
         --certificate-oidc-issuer https://token.actions.githubusercontent.com \
         --certificate-identity-regexp "^https://github\.com/${repository}/\.github/workflows/ci\.yml@${signer_ref}" \
         "$image" >/dev/null 2>&1; then
      echo "signature verified: $image"
      return 0
    fi
    [ "$attempt" -eq 1 ] && echo "waiting for the signature of $image..."
    sleep 15
  done
  return 1
}

for image do
  signed "$image" || {
    echo "::error title=Image is not signed::No signature by this repository's ci workflow appeared for $image within three minutes. An image published under this name by anything else must not become part of a composition."
    exit 1
  }
done
