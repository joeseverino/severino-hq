#!/bin/sh
# Regression: command output never crosses the public deployment boundary.

set -eu

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT HUP INT TERM

emitter="${work_dir}/emit-runtime-inventory"
cat >"${emitter}" <<'EOF'
#!/bin/sh
echo 'runtime-inventory-marker'
echo 'private diagnostic' >&2
exit "${PRIVATE_TEST_STATUS:-0}"
EOF
chmod +x "${emitter}"

success_log="${work_dir}/success.log"
success_output="$("${repo_dir}/scripts/run-private.sh" \
    'Production preflight' "${success_log}" "${emitter}" 2>&1)"
[ "${success_output}" = "Production preflight passed." ]
grep -q 'runtime-inventory-marker' "${success_log}"
[ "$(stat -f '%Lp' "${success_log}" 2>/dev/null || stat -c '%a' "${success_log}")" = 600 ]

failure_log="${work_dir}/failure.log"
set +e
failure_output="$(PRIVATE_TEST_STATUS=23 "${repo_dir}/scripts/run-private.sh" \
    'Production preflight' "${failure_log}" "${emitter}" 2>&1)"
failure_status=$?
set -e
[ "${failure_status}" -eq 23 ]
[ "${failure_output}" = "Production preflight failed; inspect the private host log." ]
grep -q 'runtime-inventory-marker' "${failure_log}"

case "${success_output}${failure_output}" in
    *runtime-inventory-marker*|*private\ diagnostic*)
        echo "Private command output reached the public stream." >&2
        exit 1
        ;;
esac

echo "private deployment output tests passed"
