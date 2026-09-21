#!/bin/sh
# Properties every shell file in this repository must hold.
#
# Each one exists because a specific defect shipped, and each is written to
# catch the *class* rather than the instance -- a fix for one occurrence is
# worth much less than a gate that refuses the next one.

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
# shellcheck source=scripts/toolchain.env
. ./scripts/toolchain.env
failures=0
failure_marker="$(mktemp)"
trap 'rm -f "${failure_marker}"' EXIT HUP INT TERM

fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

# 1. A function defined in the shared library must have a caller.
#
# An engine function with no call site is not a safety net, it is a comment that
# looks like code -- and a validator that nothing calls reads exactly like one
# that does.
for lib in scripts/lib/*.sh; do
    [ -f "${lib}" ] || continue
    sed -n 's/^\([a-z_][a-z_0-9]*\)() *{.*/\1/p' "${lib}" | while IFS= read -r fn; do
        # shellcheck disable=SC2086
        callers="$(grep -l "${fn}" $SHELL_SOURCES 2>/dev/null | grep -cv "^${lib}$")"
        if [ "${callers}" -eq 0 ]; then
            echo "FAIL ${lib##*/}: ${fn}() has no caller in SHELL_SOURCES" >&2
            echo x >>"${failure_marker}"
        fi
    done
done

# 2. Every shell file in the repository must be in SHELL_SOURCES.
#
# A file outside the list is neither syntax-checked nor shellchecked, and the
# ones that drift out are the ones nobody is thinking about -- which included a
# script executed as root on every deploy.
# SHELL_SOURCES is newline-separated; normalise before matching.
listed=" $(printf '%s' "${SHELL_SOURCES}" | tr '\n' ' ') "
for f in $(git ls-files '*.sh' deploy/targets 2>/dev/null); do
    [ -f "${f}" ] || continue
    case "${listed}" in
        *" ${f} "*) ;;
        *) fail "${f} is not in SHELL_SOURCES" ;;
    esac
done

# 3. `set -o pipefail` may not appear in a /bin/sh script.
#
# dash rejects it outright, so a guard written this way fails on the host rather
# than here, and only for the hosts that use dash.
for f in ${SHELL_SOURCES}; do
    [ -f "${f}" ] || continue
    # Not itself: this file names the option in the pattern it searches for.
    case "${f##*/}" in test-shell-contracts.sh) continue ;; esac
    head -1 "${f}" | grep -q '^#!/bin/sh' || continue
    # An executed line, not a comment explaining why it is absent.
    if grep -qE '^[[:space:]]*set +-o +pipefail' "${f}"; then
        fail "${f}: set -o pipefail under #!/bin/sh (dash rejects it)"
    fi
done

failures=$((failures + $(wc -l <"${failure_marker}" | tr -d ' ')))
if [ "${failures}" -ne 0 ]; then
    echo "Shell contracts failed (${failures})." >&2
    exit 1
fi
echo "Shell contracts hold."
