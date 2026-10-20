#!/bin/sh
# The mise a checkout runs: the min_version its mise.toml ($1) declares. Both
# the toolchain and the uv action read it here; an empty answer would let
# mise-action install whatever release is newest.
set -eu

version="$(sed -n 's/^min_version = "\(.*\)"$/\1/p' "$1")"
if [ -z "$version" ]; then
    echo "$1 declares no min_version" >&2
    exit 1
fi
echo "$version"
