#!/bin/sh
# What an image carries instead of making at every start: its collected static
# assets and its bytecode. The build files are read as text, since no gate here
# builds an image; scripts/collect-assets.sh is run against a stand-in python.

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM
failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

# 1. A start collects nothing, and nothing is mounted over the image's assets.
if grep -v '^[[:space:]]*#' entrypoint.sh | grep -q collectstatic; then
    fail "entrypoint.sh collects static files at container start"
fi
if grep -v '^[[:space:]]*#' docker-compose.yml | grep -q '/static'; then
    fail "docker-compose.yml mounts or names /static; the image's assets must not be covered"
fi

# 2. Every image build collects, as root, after the last thing it installs.
#    The last line of each build file that matters is the one that decides.
collects_last() {
    file="$1"
    label="$2"
    order="$(grep -nE 'collect-assets\.sh|^[[:space:]]*(COPY|USER)[[:space:]]' "${file}" |
        grep -vE 'COPY --chown=severino:severino entrypoint\.sh' || true)"
    collect="$(printf '%s\n' "${order}" | grep -n 'collect-assets\.sh' | tail -n 1 | cut -d: -f1)"
    [ -n "${collect}" ] || { fail "${label} never runs scripts/collect-assets.sh"; return 0; }
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

# 4. collect-assets.sh refuses a tree it could not serve.
bin="${work}/bin"
mkdir -p "${bin}"
cat >"${bin}/python" <<'EOF'
#!/bin/sh
# Stands in for `python manage.py collectstatic`.
mkdir -p "${DJANGO_STATIC_ROOT}/css"
case "${TEST_COLLECT}" in
    nothing) ;;
    private)
        echo '{}' >"${DJANGO_STATIC_ROOT}/staticfiles.json"
        echo body >"${DJANGO_STATIC_ROOT}/css/app.css"
        chmod 0640 "${DJANGO_STATIC_ROOT}/css/app.css" ;;
    good)
        echo '{}' >"${DJANGO_STATIC_ROOT}/staticfiles.json"
        echo body >"${DJANGO_STATIC_ROOT}/css/app.css" ;;
esac
[ -n "${DJANGO_SECRET_KEY}" ] || exit 9
EOF
chmod +x "${bin}/python"
collect() {
    TEST_COLLECT="$1" DJANGO_STATIC_ROOT="${work}/static-$1" PATH="${bin}:${PATH}" \
        sh scripts/collect-assets.sh >/dev/null 2>&1
}
collect good || fail "collect-assets.sh refused a readable tree with a manifest"
if collect nothing; then fail "collect-assets.sh accepted a tree with no manifest"; fi
if collect private; then fail "collect-assets.sh accepted an asset the serving account cannot read"; fi
# Under a private umask the result is still readable by another account.
(umask 077 && collect good) || fail "collect-assets.sh failed under a private umask"
[ -z "$(find "${work}/static-good" ! -perm -004)" ] ||
    fail "collect-assets.sh left an asset private under a private umask"
if DJANGO_STATIC_ROOT='' PATH="${bin}:${PATH}" TEST_COLLECT=good sh scripts/collect-assets.sh >/dev/null 2>&1; then
    fail "collect-assets.sh ran with no DJANGO_STATIC_ROOT"
fi

if [ "${failures}" -ne 0 ]; then
    echo "Image asset contracts failed (${failures})." >&2
    exit 1
fi
echo "Image asset contracts hold."
