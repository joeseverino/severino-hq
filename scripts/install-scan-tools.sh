#!/bin/sh
# Print the path of a pinned scanner, fetching it first if it is not cached.
#
# The version and the SHA-256 of each download come from scripts/toolchain.env.
# The archive is checked before it is unpacked, and a mismatch leaves nothing
# behind. Cached per user, outside the repository, so every worktree shares one
# copy.
#
# Usage:
#   scripts/install-scan-tools.sh codeql      # the CodeQL bundle CI's action uses
#   scripts/install-scan-tools.sh scorecard   # the OpenSSF Scorecard CLI
set -eu
unset CDPATH

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
# shellcheck source=scripts/toolchain.env
. ./scripts/toolchain.env

cache="${XDG_CACHE_HOME:-$HOME/.cache}/severino-hq"

digest_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# fetch <url> <sha256> <file>
fetch() {
    echo "Fetching $1" >&2
    curl -fsSL --retry 3 "$1" -o "$3"
    actual=$(digest_of "$3")
    if [ "$actual" != "$2" ]; then
        rm -f "$3"
        echo "Refusing $1: expected sha256 $2, got $actual." >&2
        exit 1
    fi
}

os=$(uname -s)
arch=$(uname -m)

case "${1:-}" in
    codeql)
        target="$cache/codeql-$CODEQL_VERSION"
        if [ ! -x "$target/codeql/codeql" ]; then
            command -v zstd >/dev/null 2>&1 || { echo "zstd is required to unpack the CodeQL bundle." >&2; exit 2; }
            case "$os" in
                Darwin) platform=osx64 sha256=$CODEQL_SHA256_OSX64 ;;
                Linux) platform=linux64 sha256=$CODEQL_SHA256_LINUX64 ;;
                *) echo "No pinned CodeQL bundle for $os." >&2; exit 2 ;;
            esac
            mkdir -p "$cache"
            staged=$(mktemp -d "$cache/.codeql.XXXXXX")
            trap 'rm -rf "$staged"' EXIT HUP INT TERM
            fetch "https://github.com/github/codeql-action/releases/download/codeql-bundle-v$CODEQL_VERSION/codeql-bundle-$platform.tar.zst" \
                "$sha256" "$staged/bundle.tar.zst"
            zstd -dc "$staged/bundle.tar.zst" | tar -xf - -C "$staged"
            rm -f "$staged/bundle.tar.zst"
            rm -rf "$target"
            mv "$staged" "$target"
            trap - EXIT HUP INT TERM
        fi
        echo "$target/codeql/codeql"
        ;;
    scorecard)
        target="$cache/scorecard-$SCORECARD_VERSION"
        if [ ! -x "$target/scorecard" ]; then
            case "$os/$arch" in
                Darwin/arm64) platform=darwin_arm64 sha256=$SCORECARD_SHA256_DARWIN_ARM64 ;;
                Darwin/x86_64) platform=darwin_amd64 sha256=$SCORECARD_SHA256_DARWIN_AMD64 ;;
                Linux/x86_64) platform=linux_amd64 sha256=$SCORECARD_SHA256_LINUX_AMD64 ;;
                Linux/aarch64) platform=linux_arm64 sha256=$SCORECARD_SHA256_LINUX_ARM64 ;;
                *) echo "No pinned Scorecard build for $os/$arch." >&2; exit 2 ;;
            esac
            mkdir -p "$cache"
            staged=$(mktemp -d "$cache/.scorecard.XXXXXX")
            trap 'rm -rf "$staged"' EXIT HUP INT TERM
            fetch "https://github.com/ossf/scorecard/releases/download/v$SCORECARD_VERSION/scorecard_${SCORECARD_VERSION}_$platform.tar.gz" \
                "$sha256" "$staged/scorecard.tar.gz"
            tar -xzf "$staged/scorecard.tar.gz" -C "$staged" scorecard
            rm -f "$staged/scorecard.tar.gz"
            rm -rf "$target"
            mv "$staged" "$target"
            trap - EXIT HUP INT TERM
        fi
        echo "$target/scorecard"
        ;;
    *)
        echo "Usage: $0 codeql|scorecard" >&2
        exit 2
        ;;
esac
