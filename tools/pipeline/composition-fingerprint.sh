#!/usr/bin/env bash
# Whether this composition has been published before, and whether to build it.
#   tools/pipeline/composition-fingerprint.sh HOST_IMAGE ADMISSIONS POLICY
# What a composition IS: this host image, these admitted wheels (ADMISSIONS,
# from admissions.sh find) and this admission policy. Two runs agreeing on all
# three produce the same image, so there is nothing to build and nothing to
# deploy. Asked before anything is downloaded or verified.
#
# Writes fingerprint, tag (ghcr.io/$GITHUB_REPOSITORY/composition:fp-…),
# changed and build to $GITHUB_OUTPUT. FORCE=false, which only an admission's
# dispatch passes, is the one case that does not rebuild what is published.
set -euo pipefail

[ "$#" -eq 3 ] || { echo "usage: $0 HOST_IMAGE ADMISSIONS POLICY" >&2; exit 2; }
host_image=$1 admissions=$2 policy=$3

fingerprint=$(printf '%s\n%s\n%s\n' \
  "$host_image" \
  "$(sha256sum "$admissions" | cut -d' ' -f1)" \
  "$(openssl dgst -sha256 "$policy" | awk '{print $NF}')" |
  sha256sum | cut -c1-32)
tag="ghcr.io/$GITHUB_REPOSITORY/composition:fp-$fingerprint"

# A tag rather than a file or a variable: the registry already holds the only
# copy of the truth that matters, and a tag that exists is proof the exact same
# composition was published.
if docker buildx imagetools inspect "$tag" >/dev/null 2>&1; then
  changed=false
  echo "composition fp-$fingerprint is already published."
else
  changed=true
  echo "composition fp-$fingerprint has not been built before."
fi
if [ "$changed" = false ] && [ "${FORCE:-true}" = false ]; then build=false; else build=true; fi

{
  echo "fingerprint=$fingerprint"
  echo "tag=$tag"
  echo "changed=$changed"
  echo "build=$build"
} >>"$GITHUB_OUTPUT"
