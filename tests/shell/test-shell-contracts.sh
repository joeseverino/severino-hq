#!/bin/sh
# Properties every shell file in this repository must hold. Each catches a
# class of defect rather than one instance of it.

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
# Derived from the tree, so no shell file can be outside these checks.
sources="$(tools/gate/shell-sources.sh)"
failures=0
failure_marker="$(mktemp)"
trap 'rm -f "${failure_marker}"' EXIT HUP INT TERM

fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

# 1. A function defined in the shared library must have a caller.
#
# An engine function with no call site is not a safety net, it is a comment that
# looks like code, and a validator that nothing calls reads exactly like one
# that does.
for lib in tools/lib/*.sh; do
    [ -f "${lib}" ] || continue
    # A program run by path keeps its own helpers; only a sourced library has
    # callers elsewhere.
    head -n 1 "${lib}" | grep -q '^#!' && continue
    sed -n 's/^\([a-z_][a-z_0-9]*\)() *{.*/\1/p' "${lib}" | while IFS= read -r fn; do
        # shellcheck disable=SC2086
        callers="$(grep -l "${fn}" ${sources} 2>/dev/null | grep -cv "^${lib}$" || true)"
        if [ "${callers}" -eq 0 ]; then
            echo "FAIL ${lib##*/}: ${fn}() has no caller in any shell file" >&2
            echo x >>"${failure_marker}"
        fi
    done
done

# 2. `set -o pipefail` may not appear in a /bin/sh script.
#
# dash rejects it outright, so a guard written this way fails on the host rather
# than here, and only for the hosts that use dash.
for f in ${sources}; do
    [ -f "${f}" ] || continue
    # Not itself: this file names the option in the pattern it searches for.
    case "${f##*/}" in test-shell-contracts.sh) continue ;; esac
    head -1 "${f}" | grep -q '^#!/bin/sh' || continue
    # An executed line, not a comment explaining why it is absent.
    if grep -qE '^[[:space:]]*set +-o +pipefail' "${f}"; then
        fail "${f}: set -o pipefail under #!/bin/sh (dash rejects it)"
    fi
done

# 3. A listing of a run's jobs reads every page.
#
# A matrix grows a run past one page, and the jobs on the next page are the
# ones a verdict or a failure report then never sees.
for f in ${sources} .github/workflows/*.yml .github/actions/*/action.yml; do
    [ -f "${f}" ] || continue
    case "${f##*/}" in test-*.sh) continue ;; esac
    grep -n 'gh api' "${f}" | grep '/jobs' | grep -v -- '--paginate' | while IFS= read -r line; do
        echo "FAIL ${f}:${line%%:*}: a run's jobs listed without --paginate" >&2
        echo x >>"${failure_marker}"
    done
done

failures=$((failures + $(wc -l <"${failure_marker}" | tr -d ' ')))
if [ "${failures}" -ne 0 ]; then
    echo "Shell contracts failed (${failures})." >&2
    exit 1
fi
echo "Shell contracts hold."
