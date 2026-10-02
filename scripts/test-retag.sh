#!/usr/bin/env bash
# Prove a retag writes the signed manifest's own bytes, and fails when the
# registry serves other bytes or a tag resolves anywhere else.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
printf '{"schemaVersion":2}' > "$work/manifest"
digest="sha256:$(sha256sum "$work/manifest" | cut -d' ' -f1)"

# A registry that serves the manifest by digest, stores what is PUT, and
# answers a HEAD with the digest of what the tag holds.
cat > "$work/bin/curl" <<'STUB'
#!/usr/bin/env bash
url="${*: -1}"; out=""; method=GET; data=""; head=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -X) method="$2"; shift ;;
    --data-binary) data="${2#@}"; shift ;;
    -fsSI) head=1 ;;
  esac
  shift
done
case "$url" in
  *token*) echo '{"token":"t"}' ;;
  */manifests/sha256:*)
    printf 'content-type: application/vnd.docker.distribution.manifest.v2+json\r\n\r\n'
    if [ -n "${TEST_SERVE_OTHER:-}" ]; then printf 'other' > "$out"; else cp "$TEST_MANIFEST" "$out"; fi ;;
  */manifests/*)
    tag="${url##*/}"
    if [ "$method" = PUT ]; then cp "$data" "$TEST_STORE/$tag"
    elif [ "$head" = 1 ]; then
      stored="$TEST_STORE/$tag"; [ -n "${TEST_REWRAP:-}" ] && printf 'wrapped' > "$stored"
      printf 'docker-content-digest: sha256:%s\r\n' "$(sha256sum "$stored" | cut -d' ' -f1)"
    fi ;;
esac
STUB
chmod +x "$work/bin/curl"
mkdir -p "$work/store"

retag() {
  PATH="$work/bin:$PATH" GITHUB_ACTOR=a GITHUB_TOKEN=t TEST_MANIFEST="$work/manifest" \
    TEST_STORE="$work/store" "$repo_dir/scripts/retag.sh" "ghcr.io/example/hq@$digest" sha-0123456789ab latest
}

retag >/dev/null || { echo "a faithful retag failed" >&2; exit 1; }
cmp -s "$work/manifest" "$work/store/sha-0123456789ab" || { echo "the tag holds other bytes" >&2; exit 1; }
if TEST_SERVE_OTHER=1 retag >/dev/null 2>&1; then echo "retagged bytes that are not the digest" >&2; exit 1; fi
if TEST_REWRAP=1 retag >/dev/null 2>&1; then echo "accepted a tag that resolves elsewhere" >&2; exit 1; fi
if PATH="$work/bin:$PATH" GITHUB_ACTOR=a GITHUB_TOKEN=t "$repo_dir/scripts/retag.sh" "ghcr.io/example/hq:latest" x >/dev/null 2>&1; then
  echo "accepted a reference that is not a digest" >&2; exit 1
fi
echo "retag tests passed"
