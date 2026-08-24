#!/bin/sh
# Keep production diagnostics on the host instead of inheriting a public CI log.

set -eu

label="${1:?usage: run-private.sh LABEL LOG_FILE COMMAND [ARGUMENT ...]}"
log_file="${2:?usage: run-private.sh LABEL LOG_FILE COMMAND [ARGUMENT ...]}"
shift 2
if [ "$#" -eq 0 ]; then
    echo "run-private.sh requires a command." >&2
    exit 2
fi

log_dir="$(dirname -- "${log_file}")"
if [ ! -d "${log_dir}" ]; then
    echo "${label} could not start because its private log directory is absent." >&2
    exit 2
fi

umask 077
temporary="$(mktemp "${log_file}.XXXXXX")"
trap 'rm -f "${temporary}"' EXIT HUP INT TERM

status=0
"$@" >"${temporary}" 2>&1 || status=$?
mv -f "${temporary}" "${log_file}"
chmod 0600 "${log_file}"
trap - EXIT HUP INT TERM

if [ "${status}" -eq 0 ]; then
    echo "${label} passed."
    exit 0
fi

echo "${label} failed; inspect the private host log." >&2
exit "${status}"
