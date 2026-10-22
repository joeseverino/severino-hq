#!/bin/sh
# Installs the mise a checkout runs (mise-version.sh) on Linux, for the image
# builds: mise then installs every other build tool from mise.lock. The
# release's checksum list must carry mise's minisign signature and the binary
# must match it, or nothing is installed.
#
#   install-mise.sh <mise.toml> <destination>
set -eu

# mise's release key (minisign.pub in jdx/mise). It signs every release, so it
# stays put while the version moves.
key=RWTC3g8W3z4RZK3V3qv7fa1QY4JEWyBtqIHW+85QlJpZc5yG+uNYNBSZ

version="$("$(dirname "$0")/mise-version.sh" "$1")"
case "$(uname -m)" in
    x86_64) platform=linux-x64 ;;
    aarch64) platform=linux-arm64 ;;
    *) echo "no mise build for $(uname -m)" >&2; exit 1 ;;
esac
asset="mise-v${version}-${platform}"
release="https://github.com/jdx/mise/releases/download/v${version}"

work="$(mktemp -d)"
for file in SHASUMS256.txt SHASUMS256.txt.minisig "$asset"; do
    curl -fsSL --proto '=https' --retry 3 -o "$work/$file" "$release/$file"
done
minisign -Vq -m "$work/SHASUMS256.txt" -P "$key"
grep " \./${asset}\$" "$work/SHASUMS256.txt" > "$work/asset.sum"
(cd "$work" && sha256sum --check --strict --quiet asset.sum)
install -m 0755 "$work/$asset" "$2"
rm -rf "$work"
