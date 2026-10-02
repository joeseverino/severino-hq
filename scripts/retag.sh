#!/usr/bin/env bash
# Points tags at an image by digest without rewriting it.
#   scripts/retag.sh ghcr.io/OWNER/NAME@sha256:… TAG [TAG ...]
# The manifest's own bytes are written under each tag, so the tag resolves to
# the digest that was signed; a tool that re-wraps the manifest would produce a
# new, unsigned digest. Each tag is read back and must name that digest. Needs
# GITHUB_ACTOR and GITHUB_TOKEN with packages: write.
set -euo pipefail

pinned="${1:?usage: retag.sh REGISTRY/NAME@sha256:DIGEST TAG [TAG ...]}"
shift
[ "$#" -gt 0 ] || { echo "retag.sh: no tag given" >&2; exit 2; }
[[ "$pinned" =~ ^ghcr\.io/([a-z0-9._/-]+)@(sha256:[0-9a-f]{64})$ ]] \
  || { echo "retag.sh: not a ghcr.io digest reference: $pinned" >&2; exit 2; }
name="${BASH_REMATCH[1]}"
digest="${BASH_REMATCH[2]}"
api="https://ghcr.io/v2/${name}/manifests"
types="application/vnd.oci.image.manifest.v1+json,application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json"

token="$(curl -fsS -u "${GITHUB_ACTOR:?}:${GITHUB_TOKEN:?}" \
  "https://ghcr.io/token?scope=repository:${name}:pull,push" | jq -r .token)"
manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT
type="$(curl -fsS -H "Authorization: Bearer $token" -H "Accept: $types" \
  -D - -o "$manifest" "$api/$digest" | tr -d '\r' | sed -n 's/^[Cc]ontent-[Tt]ype: //p')"
[ "sha256:$(sha256sum "$manifest" | cut -d' ' -f1)" = "$digest" ] \
  || { echo "retag.sh: the registry served other bytes for $digest" >&2; exit 1; }

for tag in "$@"; do
  [[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$ ]] || { echo "retag.sh: not a tag: $tag" >&2; exit 2; }
  curl -fsS -X PUT -H "Authorization: Bearer $token" -H "Content-Type: $type" \
    --data-binary @"$manifest" "$api/$tag" >/dev/null
  resolved="$(curl -fsSI -H "Authorization: Bearer $token" -H "Accept: $types" "$api/$tag" \
    | tr -d '\r' | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: //p')"
  [ "$resolved" = "$digest" ] || { echo "retag.sh: $tag resolves to $resolved, not $digest" >&2; exit 1; }
  echo "$tag -> $digest"
done
