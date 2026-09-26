#!/bin/sh
# Contract test for the credential minting scripts, against a stub curl.
#
# Nothing leaves the machine: curl on PATH is a fixture that answers from
# canned JSON and records its arguments, stdin and request body.

set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
fixture_dir="$(mktemp -d)"
readonly fixture_dir
trap 'rm -rf "${fixture_dir}"' EXIT HUP INT TERM

failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }
pass() { echo "ok   $1"; }

BOOTSTRAP="bootstrap-secret-0123456789"
MINTED="minted-secret-9876543210"

cat >"${fixture_dir}/curl" <<SH
#!/bin/sh
set -eu
log="${fixture_dir}/log"
printf '%s\n' "\$*" >>"\${log}.argv"
cat >>"\${log}.stdin"
body=""
method=GET
url=""
while [ "\$#" -gt 0 ]; do
    case "\$1" in
        --data-binary) case "\$2" in @-) ;; @*) body="\${2#@}" ;; esac; shift 2 ;;
        --request) method="\$2"; shift 2 ;;
        --header|--proto|--max-time) shift 2 ;;
        -*) shift ;;
        *) url="\$1"; shift ;;
    esac
done
[ -z "\${body}" ] || cat "\${body}" >>"\${log}.body"
case "\${method} \${url}" in
    "GET https://api.example.test/user/tokens/permission_groups")
        cat <<'JSON'
{"success": true, "result": [
  {"id": "g-acct-settings", "name": "Account Settings Read", "scopes": ["com.cloudflare.api.account"]},
  {"id": "g-acct-analytics", "name": "Account Analytics Read", "scopes": ["com.cloudflare.api.account"]},
  {"id": "g-registrar", "name": "Registrar Domains Read", "scopes": ["com.cloudflare.api.account"]},
  {"id": "g-zone-settings", "name": "Zone Settings Read", "scopes": ["com.cloudflare.api.account.zone"]},
  {"id": "g-zone-analytics", "name": "Analytics Read", "scopes": ["com.cloudflare.api.account.zone"]}
]}
JSON
        ;;
    "POST https://api.example.test/user/tokens")
        printf '%s\n' '{"success": true, "result": {"id": "tok-1", "expires_on": "2030-01-01T00:00:00Z", "value": "${MINTED}"}}'
        ;;
    "POST https://ts.example.test/oauth/token")
        printf '%s\n' '{"access_token": "exchanged-token-555"}'
        ;;
    "POST https://ts.example.test/tailnet/-/keys")
        printf '%s\n' '{"id": "client-1", "key": "${MINTED}", "keyType": "client"}'
        ;;
    *)
        printf '%s\n' '{"success": false, "errors": [{"message": "unexpected request"}]}'
        ;;
esac
SH
chmod 0700 "${fixture_dir}/curl"

reset_log() { rm -f "${fixture_dir}"/log.*; touch "${fixture_dir}/log.argv" "${fixture_dir}/log.stdin" "${fixture_dir}/log.body"; }

CF="${script_dir}/mint-cloudflare-token.sh"
TS="${script_dir}/mint-tailscale-client.sh"

# An assignment before a shell function persists in some shells, so the
# environment is passed to env as arguments and every bootstrap variable is
# cleared first.
run() { # run [VAR=value...] SCRIPT ARGS...; sets status, out, err
    reset_log
    set +e
    env -u CLOUDFLARE_BOOTSTRAP_TOKEN -u TAILSCALE_BOOTSTRAP_TOKEN \
        -u TAILSCALE_BOOTSTRAP_CLIENT_ID -u TAILSCALE_BOOTSTRAP_CLIENT_SECRET \
        PATH="${fixture_dir}:${PATH}" \
        CLOUDFLARE_API_BASE=https://api.example.test \
        TAILSCALE_API_BASE=https://ts.example.test \
        "$@" >"${fixture_dir}/out" 2>"${fixture_dir}/err"
    status=$?
    set -e
    out="$(cat "${fixture_dir}/out")"
    err="$(cat "${fixture_dir}/err")"
}

never_in_argv() { # never_in_argv NAME SECRET
    if grep -q "$2" "${fixture_dir}/log.argv"; then
        fail "$1: a secret reached curl's argv"
    fi
}

# --- Cloudflare -----------------------------------------------------------

# The script's mechanics, against groups the stub knows. The real list is
# checked against the readings by control_plane.test_credential_reads.
printf '%s\n' "Account Settings Read (account)" "Account Analytics Read (account)" \
    "Registrar Domains Read (account)" "Zone Settings Read (zone)" \
    >"${fixture_dir}/known.txt"

run CLOUDFLARE_BOOTSTRAP_TOKEN="" "${CF}" --account abc123
if [ "${status}" -ne 0 ] && printf '%s' "${err}" | grep -q CLOUDFLARE_BOOTSTRAP_TOKEN; then
    pass "cloudflare: no bootstrap token, no call"
else
    fail "cloudflare: ran without a bootstrap token"
fi

run CLOUDFLARE_BOOTSTRAP_TOKEN="${BOOTSTRAP}" "${CF}" --account abc123 \
    --permissions "${fixture_dir}/known.txt"
if [ "${status}" -eq 0 ] && [ -z "${out}" ] \
    && printf '%s' "${err}" | grep -q "Nothing created" \
    && ! grep -q "POST" "${fixture_dir}/log.argv"; then
    pass "cloudflare: without --print-secret nothing is created"
else
    fail "cloudflare: plan mode (status ${status}): ${err}"
fi

run CLOUDFLARE_BOOTSTRAP_TOKEN="${BOOTSTRAP}" "${CF}" \
    --account abc123 --expires 2030-01-01 --allow-ip 192.0.2.0/24 --print-secret \
    --permissions "${fixture_dir}/known.txt"
if [ "${status}" -eq 0 ] && [ "${out}" = "${MINTED}" ] \
    && printf '%s' "${err}" | grep -q "Token id: tok-1" \
    && printf '%s' "${err}" | grep -q "Expires: 2030-01-01"; then
    pass "cloudflare: --print-secret writes only the secret to stdout"
else
    fail "cloudflare: print-secret (status ${status}): ${err}"
fi
never_in_argv cloudflare "${BOOTSTRAP}"
body="$(cat "${fixture_dir}/log.body")"
if printf '%s' "${body}" | jq -e '
    .expires_on == "2030-01-01T00:00:00Z"
    and .condition["request.ip"].in == ["192.0.2.0/24"]
    and ([.policies[] | select(.resources["com.cloudflare.api.account.abc123"] == "*")
          | .permission_groups[].id] | sort)
        == ["g-acct-analytics", "g-acct-settings", "g-registrar"]
    and ([.policies[] | select(.resources["com.cloudflare.api.account.abc123"]
          | type == "object") | .permission_groups[].id]) == ["g-zone-settings"]
' >/dev/null; then
    pass "cloudflare: permission names resolve to ids under the right resources"
else
    fail "cloudflare: token body was ${body}"
fi

printf '%s\n' "Account Settings Read (account)" "Workers Scripts Read (account)" \
    >"${fixture_dir}/permissions.txt"
run CLOUDFLARE_BOOTSTRAP_TOKEN="${BOOTSTRAP}" "${CF}" \
    --account abc123 --permissions "${fixture_dir}/permissions.txt" --print-secret
if [ "${status}" -ne 0 ] && [ -z "${out}" ] \
    && printf '%s' "${err}" | grep -q "Workers Scripts Read (account)"; then
    pass "cloudflare: an unknown permission group stops the mint and is named"
else
    fail "cloudflare: unknown group (status ${status}): ${err}"
fi

# --- Tailscale ------------------------------------------------------------

run "${TS}"
if [ "${status}" -eq 0 ] && [ -z "${out}" ] \
    && printf '%s' "${err}" | grep -q "devices:core:read" \
    && printf '%s' "${err}" | grep -q "Nothing created" \
    && [ ! -s "${fixture_dir}/log.argv" ]; then
    pass "tailscale: without --print-secret it prints the scopes and calls nothing"
else
    fail "tailscale: plan mode (status ${status}): ${err}"
fi

run TAILSCALE_BOOTSTRAP_TOKEN="${BOOTSTRAP}" "${TS}" --print-secret
if [ "${status}" -eq 0 ] && [ "${out}" = "${MINTED}" ] \
    && printf '%s' "${err}" | grep -q "Client id: client-1"; then
    pass "tailscale: --print-secret writes only the secret to stdout"
else
    fail "tailscale: print-secret (status ${status}): ${err}"
fi
never_in_argv tailscale "${BOOTSTRAP}"
if jq -e '.keyType == "client" and (.scopes | all(test(":read$")))' \
    <"${fixture_dir}/log.body" >/dev/null; then
    pass "tailscale: the client is created with read scopes only"
else
    fail "tailscale: client body was $(cat "${fixture_dir}/log.body")"
fi

run TAILSCALE_BOOTSTRAP_CLIENT_ID=boot-id TAILSCALE_BOOTSTRAP_CLIENT_SECRET="${BOOTSTRAP}" "${TS}" --print-secret
if [ "${status}" -eq 0 ] && [ "${out}" = "${MINTED}" ] \
    && grep -q "Bearer exchanged-token-555" "${fixture_dir}/log.stdin"; then
    pass "tailscale: a bootstrap client is exchanged for a token"
else
    fail "tailscale: client exchange (status ${status}): ${err}"
fi
never_in_argv tailscale-exchange "${BOOTSTRAP}"
never_in_argv tailscale-exchange "exchanged-token-555"

printf '%s\n' "devices:core:read" "policy_file" >"${fixture_dir}/scopes.txt"
run TAILSCALE_BOOTSTRAP_TOKEN="${BOOTSTRAP}" "${TS}" \
    --scopes "${fixture_dir}/scopes.txt" --print-secret
if [ "${status}" -ne 0 ] && printf '%s' "${err}" | grep -q "policy_file" \
    && [ ! -s "${fixture_dir}/log.argv" ]; then
    pass "tailscale: a write scope is refused before any call"
else
    fail "tailscale: write scope (status ${status}): ${err}"
fi

if [ "${failures}" -ne 0 ]; then
    echo "Minting contract failed (${failures})." >&2
    exit 1
fi
echo "Minting contract holds."
