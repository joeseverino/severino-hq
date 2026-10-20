#!/bin/sh
# What an image carries instead of making at every start: its collected static
# assets and its bytecode. The build files are read as text, since no gate here
# builds an image. What the collected tree holds is image:verify:assets's.

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM
failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

# 1. A start collects nothing.
if grep -v '^[[:space:]]*#' entrypoint.sh | grep -q collectstatic; then
    fail "entrypoint.sh collects static files at container start"
fi

# 2. Every image build collects, as root, after the last thing it installs.
#    The last line of each build file that matters is the one that decides.
collects_last() {
    file="$1"
    label="$2"
    order="$(grep -nE 'collectstatic|^[[:space:]]*(COPY|USER)[[:space:]]' "${file}" |
        grep -vE 'COPY --chown=severino:severino entrypoint\.sh' || true)"
    collect="$(printf '%s\n' "${order}" | grep -n 'collectstatic' | tail -n 1 | cut -d: -f1)"
    [ -n "${collect}" ] || { fail "${label} never collects its static files"; return 0; }
    after="$(printf '%s\n' "${order}" | sed -n "$((collect + 1)),\$p")"
    before="$(printf '%s\n' "${order}" | sed -n "1,$((collect - 1))p")"
    if printf '%s\n' "${after}" | grep -q 'COPY'; then
        fail "${label} copies files in after it collects"
    fi
    if printf '%s\n' "${before}" | grep 'USER' | tail -n 1 | grep -q 'USER severino'; then
        fail "${label} collects as the serving account, which could then rewrite its assets"
    fi
    printf '%s\n' "${after}" | grep -q 'USER severino' ||
        fail "${label} does not return to the serving account after collecting"
}
# The host image's final stage.
sed -n '/ AS runtime$/,$p' Dockerfile >"${work}/host"
collects_last "${work}/host" Dockerfile
# The composition's final stage.
sed -n '/^FROM .{HQ_IMAGE}/,$p' composition/Dockerfile >"${work}/composition"
[ -s "${work}/composition" ] || fail "composition/Dockerfile has no final stage to read"
collects_last "${work}/composition" composition/Dockerfile
# The candidate composition a pull request builds inline.
sed -n '/FROM .{HQ_IMAGE} AS runtime/,/DOCKERFILE$/p' .github/workflows/compose.yml >"${work}/candidate"
[ -s "${work}/candidate" ] || fail "compose.yml has no candidate image to read"
collects_last "${work}/candidate" "compose.yml's candidate image"

# 3. The application's bytecode is compiled into the image, valid without a
#    timestamp, and every extension wheel is installed with its own.
grep -q 'compileall .*--invalidation-mode unchecked-hash' Dockerfile ||
    fail "Dockerfile does not compile the application with hash-based bytecode"
grep -q 'PYTHONHASHSEED=0 python -m compileall' Dockerfile ||
    fail "Dockerfile compiles under the random hash seed the image sets"
sed -n '/ AS installer-base$/,/^FROM /p' composition/Dockerfile | grep -q 'UV_COMPILE_BYTECODE=1' ||
    fail "composition/Dockerfile installs extension wheels without bytecode"

if [ "${failures}" -ne 0 ]; then
    echo "Image asset contracts failed (${failures})." >&2
    exit 1
fi
echo "Image asset contracts hold."
