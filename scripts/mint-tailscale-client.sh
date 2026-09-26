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
# writes only the secret to stdout for piping into a store.

set -eu

usage() {
    cat >&2 <<'EOF'
usage: mint-tailscale-client.sh [options]

  --tailnet NAME      tailnet (default: -, the credential's own)
  --description TEXT  client description (default: hq-observer)
  --tag TAG           tag the client; repeatable
  --scopes FILE       scope list (default: beside this script)
  --print-secret      create the client and write its secret to stdout

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

while [ "$#" -gt 0 ]; do
    case "$1" in
        --tailnet) [ "$#" -ge 2 ] || usage; tailnet="$2"; shift 2 ;;
        --description) [ "$#" -ge 2 ] || usage; description="$2"; shift 2 ;;
        --tag) [ "$#" -ge 2 ] || usage; tags="${tags}$2
"; shift 2 ;;
        --scopes) [ "$#" -ge 2 ] || usage; scopes="$2"; shift 2 ;;
        --print-secret) print_secret=1; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
done

command -v jq >/dev/null || die "jq is required."
command -v curl >/dev/null || die "curl is required."
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

if [ "${print_secret}" -eq 0 ]; then
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
    printf 'client_id=%s&client_secret=%s&scope=oauth_keys' \
        "${TAILSCALE_BOOTSTRAP_CLIENT_ID}" "${TAILSCALE_BOOTSTRAP_CLIENT_SECRET}" |
        curl -q --silent --show-error --proto '=https' --max-time 30 \
            --request POST --data-binary @- \
            --header 'Content-Type: application/x-www-form-urlencoded' \
            "${api}/oauth/token" >"${work}/response"
    token="$(jq -r '.access_token // empty' <"${work}/response" 2>/dev/null || true)"
    [ -n "${token}" ] || die "Tailscale did not exchange the bootstrap client for a token."
else
    die "Set TAILSCALE_BOOTSTRAP_TOKEN, or TAILSCALE_BOOTSTRAP_CLIENT_ID and TAILSCALE_BOOTSTRAP_CLIENT_SECRET."
fi

# The bearer header travels over stdin, not argv.
printf 'Authorization: Bearer %s\n' "${token}" |
    curl -q --silent --show-error --proto '=https' --max-time 30 \
        --request POST --header @- \
        --header 'Content-Type: application/json' \
        --data-binary "@${work}/body" \
        "${api}/tailnet/${tailnet}/keys" >"${work}/response"

id="$(jq -r 'if type == "object" then .id // empty else empty end' <"${work}/response" 2>/dev/null || true)"
if [ -z "${id}" ]; then
    reason="$(jq -r '.message // empty' <"${work}/response" 2>/dev/null || true)"
    die "Tailscale refused the new client: ${reason:-unreadable response}"
fi
echo "Client id: ${id}" >&2
echo "Expires: never; OAuth clients stay valid until revoked." >&2
jq -er '.key' <"${work}/response"
