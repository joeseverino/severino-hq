#!/bin/sh
# The mise a checkout runs: the min_version its mise.toml ($1) declares. Both
# the toolchain and the uv action read it here; a missing key fails rather
# than letting mise-action install whatever release is newest.
set -eu

python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["min_version"])' "$1"
