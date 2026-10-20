#!/usr/bin/env bash
# That the host image was signed by REPOSITORY's own ci workflow.
#   tools/pipeline/host-signature.sh REPOSITORY IMAGE
# A tag derived from a commit says who should have built an image, not who
# did, so an image pushed to the same name by anything else fails here rather
# than becoming the base of a production composition. IS_RELEASE=true admits
# only a build of main; anything else never publishes, so it may also compose
# onto a pull request's build.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 REPOSITORY IMAGE" >&2; exit 2; }
repository=$1 image=$2
if [ "${IS_RELEASE:-false}" = true ]; then
  signer_ref='refs/heads/main$'
else
  signer_ref='refs/(heads/main|pull/[0-9]+/merge)$'
fi

# Retried, because the host build pushes the image and signs it in two steps
# and host-image.sh ends its wait at the push, so the manifest can exist for a
# few seconds before its signature does: 12 tries 15 seconds apart.
for attempt in $(seq 1 12); do
  if cosign verify \
       --certificate-oidc-issuer https://token.actions.githubusercontent.com \
       --certificate-identity-regexp "^https://github\.com/${repository}/\.github/workflows/ci\.yml@${signer_ref}" \
       "$image" >/dev/null 2>&1; then
    echo "host image signature verified"
    exit 0
  fi
  [ "$attempt" -eq 1 ] && echo "waiting for the host image signature..."
  sleep 15
done
echo "::error title=Host image is not signed::No signature by this repository's ci workflow appeared for the host image within three minutes. An image published under this name by anything else must not become the base of a composition."
exit 1
