#!/bin/sh
# Connect's CLI integration cannot list items. Use its read-only REST endpoint.
set -eu
vault="${1:?usage: list-secret-items.sh VAULT}"
if [ -z "${OP_CONNECT_HOST:-}" ] && [ -z "${OP_CONNECT_TOKEN:-}" ]; then
    exec op item list --vault "${vault}" --format json
fi
if ! printf '%s\n' "${OP_CONNECT_HOST:-}" | grep -Eq '^http://127\.0\.0\.1:[0-9]{1,5}$'; then
    echo "Connect requires an explicit IPv4 loopback endpoint." >&2
    exit 1
fi
case "${OP_CONNECT_TOKEN:-}" in
    ''|*[!A-Za-z0-9_.-]*)
        echo "Connect token is missing or malformed." >&2
        exit 1 ;;
esac
connect_get() {
    # Ignore curlrc and proxies; never forward a bearer token on a redirect.
    # The header travels over stdin, not argv or a temporary file.
    printf 'Authorization: Bearer %s\n' "${OP_CONNECT_TOKEN}" |
        curl -q --silent --show-error --fail --noproxy '*' \
            --connect-timeout 3 --max-time 15 --proto '=http' \
            --header @- "${OP_CONNECT_HOST}$1"
}
vaults="$(connect_get /v1/vaults)"
vault_id="$(printf '%s' "${vaults}" | jq -er --arg vault "${vault}" '
    [.[] | select(.id == $vault or .name == $vault)]
    | if length == 1 then .[0].id else error("Vault must resolve uniquely") end
    | select(test("^[a-z0-9]{26}$"))
')"
items="$(connect_get "/v1/vaults/${vault_id}/items")"
printf '%s' "${items}" | jq -ce '
    if type == "array" and all(.[]; (.id | type == "string")
        and (.id | test("^[a-z0-9]{26}$"))) then .
    else error("Invalid Connect item listing") end
'
