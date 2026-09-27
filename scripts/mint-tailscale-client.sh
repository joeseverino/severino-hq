#!/bin/sh
# Mint a read-only Tailscale OAuth client for HQ's tailscale connection.
#
# Run on the operator's machine. Creates the client with
# POST /tailnet/{tailnet}/keys and keyType "client". The bootstrap credential
# needs the oauth_keys scope and comes from the environment, never an argument:
# either TAILSCALE_BOOTSTRAP_TOKEN (an API access token), or
# TAILSCALE_BOOTSTRAP_CLIENT_ID and TAILSCALE_BOOTSTRAP_CLIENT_SECRET (an OAuth
# client, exchanged here for an access token).
#
# Scopes come from tailscale-observer-scopes.txt. Without --print-secret it
# prints the scopes, which are also what to tick when creating the client in
# the admin console instead, and creates nothing: the secret is shown once.
# With --print-secret it creates the client, reports its id on stderr, and
# writes only the secret to stdout for piping into a store. With --store and
# --store-id (op://<vault>/<item>/<field> each, one item) it checks the item
# carries both fields, creates the client, and writes its secret and id there
# through `op`; the secret reaches no argument, file or output.

set -eu

usage() {
    cat >&2 <<'EOF'
usage: mint-tailscale-client.sh [options]

  --tailnet NAME      tailnet (default: -, the credential's own)
  --description TEXT  client description (default: hq-observer)
  --tag TAG           tag the client; repeatable
  --scopes FILE       scope list (default: beside this script)
  --print-secret      create the client and write its secret to stdout
  --store REF         create the client and store its secret at REF
  --store-id REF      with --store, store the client id at REF (same item)

environment:
  TAILSCALE_BOOTSTRAP_TOKEN          API access token with oauth_keys, or
  TAILSCALE_BOOTSTRAP_CLIENT_ID      OAuth client with oauth_keys, and
  TAILSCALE_BOOTSTRAP_CLIENT_SECRET  its secret
  TAILSCALE_API_BASE                 default https://api.tailscale.com/api/v2
EOF
    exit 2
}

die() { echo "$*" >&2; exit 1; }

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
api="${TAILSCALE_API_BASE:-https://api.tailscale.com/api/v2}"
tailnet="-"
description="hq-observer"
tags=""
scopes="${script_dir}/tailscale-observer-scopes.txt"
print_secret=0
store=""
store_id=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --tailnet) [ "$#" -ge 2 ] || usage; tailnet="$2"; shift 2 ;;
        --description) [ "$#" -ge 2 ] || usage; description="$2"; shift 2 ;;
        --tag) [ "$#" -ge 2 ] || usage; tags="${tags}$2
"; shift 2 ;;
        --scopes) [ "$#" -ge 2 ] || usage; scopes="$2"; shift 2 ;;
        --print-secret) print_secret=1; shift ;;
        --store) [ "$#" -ge 2 ] || usage; store="$2"; shift 2 ;;
        --store-id) [ "$#" -ge 2 ] || usage; store_id="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
done

command -v jq >/dev/null || die "jq is required."
command -v curl >/dev/null || die "curl is required."
if [ -n "${store}" ] || [ -n "${store_id}" ]; then
    [ -n "${store}" ] && [ -n "${store_id}" ] \
        || die "--store and --store-id go together: the client id and its secret."
    [ "${print_secret}" -eq 0 ] || die "--store and --print-secret are exclusive."
    # shellcheck source=scripts/lib/op-store.sh
    . "${script_dir}/lib/op-store.sh"
    op_store_parse "${store_id}"
    id_vault="${op_vault}"; id_item="${op_item}"; id_field="${op_field}"
    op_store_parse "${store}"
    [ "${id_vault}/${id_item}" = "${op_vault}/${op_item}" ] \
        || die "--store and --store-id name one item."
fi
[ -f "${scopes}" ] || die "Scope list not found: ${scopes}"
case "${tailnet}" in ''|*[!A-Za-z0-9._@-]*) die "The tailnet name is malformed." ;; esac
# Tailscale's own limit: 50 characters, alphanumerics, hyphens and spaces.
case "${description}" in ''|*[!A-Za-z0-9\ -]*) die "The description takes letters, digits, hyphens and spaces." ;; esac
[ "${#description}" -le 50 ] || die "The description is at most 50 characters."

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM

grep -v '^[[:space:]]*#' "${scopes}" | grep -v '^[[:space:]]*$' \
    | jq -R . | jq -s . >"${work}/scopes"
printf '%s' "${tags}" | grep -v '^$' | jq -R . | jq -s . >"${work}/tags"

if jq -e 'any(.[]; test(":read$") | not)' >/dev/null <"${work}/scopes"; then
    die "Every scope must be a read scope: $(jq -r '[.[] | select(test(":read$") | not)] | join(", ")' <"${work}/scopes")"
fi
jq -e 'length > 0' >/dev/null <"${work}/scopes" || die "The scope list is empty."

jq -n --slurpfile scopes "${work}/scopes" --slurpfile tags "${work}/tags" \
    --arg description "${description}" '
    {keyType: "client", description: $description, scopes: $scopes[0]}
    + (if ($tags[0] | length) > 0 then {tags: $tags[0]} else {} end)
' >"${work}/body"

if [ -n "${store}" ]; then
    op_store_check "${op_vault}" "${op_item}" "${op_field}" "${id_field}"
elif [ "${print_secret}" -eq 0 ]; then
    echo "Would create OAuth client ${description} with scopes:" >&2
    jq -r '.[] | "  " + .' <"${work}/scopes" >&2
    echo "Nothing created. Rerun with --print-secret and pipe stdout into the" >&2
    echo "tailscale connection's CLIENT_SECRET field (docs/SECRETS.md), or create" >&2
    echo "the client under Trust credentials in the admin console with these scopes." >&2
    exit 0
fi

if [ -n "${TAILSCALE_BOOTSTRAP_TOKEN:-}" ]; then
    token="${TAILSCALE_BOOTSTRAP_TOKEN}"
elif [ -n "${TAILSCALE_BOOTSTRAP_CLIENT_ID:-}" ] && [ -n "${TAILSCALE_BOOTSTRAP_CLIENT_SECRET:-}" ]; then
    # The form body carries the secret, so it travels over stdin.
    exchanged="$(printf 'client_id=%s&client_secret=%s&scope=oauth_keys' \
        "${TAILSCALE_BOOTSTRAP_CLIENT_ID}" "${TAILSCALE_BOOTSTRAP_CLIENT_SECRET}" |
        curl -q --silent --show-error --proto '=https' --max-time 30 \
            --request POST --data-binary @- \
            --header 'Content-Type: application/x-www-form-urlencoded' \
            "${api}/oauth/token")"
    token="$(printf '%s' "${exchanged}" | jq -r '.access_token // empty' 2>/dev/null || true)"
    [ -n "${token}" ] || die "Tailscale did not exchange the bootstrap client for a token."
else
    die "Set TAILSCALE_BOOTSTRAP_TOKEN, or TAILSCALE_BOOTSTRAP_CLIENT_ID and TAILSCALE_BOOTSTRAP_CLIENT_SECRET."
fi

# The bearer header travels over stdin, not argv. The answer carries the
# secret, so it stays in memory rather than in ${work}.
created="$(printf 'Authorization: Bearer %s\n' "${token}" |
    curl -q --silent --show-error --proto '=https' --max-time 30 \
        --request POST --header @- \
        --header 'Content-Type: application/json' \
        --data-binary "@${work}/body" \
        "${api}/tailnet/${tailnet}/keys")"

id="$(printf '%s' "${created}" | jq -r 'if type == "object" then .id // empty else empty end' 2>/dev/null || true)"
if [ -z "${id}" ]; then
    reason="$(printf '%s' "${created}" | jq -r '.message // empty' 2>/dev/null || true)"
    die "Tailscale refused the new client: ${reason:-unreadable response}"
fi
echo "Client id: ${id}" >&2
echo "Expires: never; OAuth clients stay valid until revoked." >&2
if [ -n "${store}" ]; then
    printf '%s' "${created}" | jq -e --arg secret "${op_field}" --arg id "${id_field}" \
        '{($secret): .key, ($id): .id} | select(.[$secret] | type == "string" and length > 0)' \
        | op_store_write "${op_vault}" "${op_item}" \
        || die "Client ${id} was created but not stored. Revoke it under Trust credentials and run this again."
    echo "Stored in ${op_item} (${op_vault}); the controller reads it on its next render." >&2
    exit 0
fi
printf '%s' "${created}" | jq -er '.key'
