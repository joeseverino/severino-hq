#!/bin/sh
# Properties every shell file in this repository must hold that shellcheck
# cannot see. Each catches a class of defect rather than one instance of it.

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
# Derived from the tree, so no shell file can be outside these checks.
sources="$(tools/gate/shell-sources.sh)"
failure_marker="$(mktemp)"
trap 'rm -f "${failure_marker}"' EXIT HUP INT TERM

# A listing of a run's jobs reads every page.
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

failures="$(wc -l <"${failure_marker}" | tr -d ' ')"
if [ "${failures}" -ne 0 ]; then
    echo "Shell contracts failed (${failures})." >&2
    exit 1
fi
echo "Shell contracts hold."
