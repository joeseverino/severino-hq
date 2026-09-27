#!/bin/sh
# Mint a read-only Cloudflare observer token for HQ's cloudflare_api connection.
#
# Run on the operator's machine. The bootstrap token (one that may create API
# tokens: "API Tokens Write") comes from CLOUDFLARE_BOOTSTRAP_TOKEN, never from
# an argument. Permission names come from cloudflare-observer-permissions.txt
# and resolve to IDs through GET /user/tokens/permission_groups.
#
# Without --print-secret it resolves the permissions, prints the token it would
# create and where the secret belongs, and creates nothing: Cloudflare shows a
# secret once, so a token minted without printing it could never be used.
# With --print-secret it creates the token, reports its id and expiry on
# stderr, and writes only the secret to stdout for piping into a store.
# With --store op://<vault>/<item>/<field> it checks the item carries the field,
# creates the token, and writes the secret into that field through `op`; the
# secret reaches no argument, file or output.

set -eu

usage() {
    cat >&2 <<'EOF'
usage: mint-cloudflare-token.sh --account ACCOUNT_ID [options]

  --account ID        Cloudflare account the token reads (required)
  --name NAME         token name (default: hq-observer)
  --days N            days until expiry (default: 90)
  --expires DATE      expiry as YYYY-MM-DD; overrides --days
  --allow-ip CIDR     accept the token only from this range; repeatable
  --permissions FILE  permission list (default: beside this script)
  --list-groups       print permission group names and scopes, then exit
  --print-secret      create the token and write its secret to stdout
  --store REF         create the token and store it at op://<vault>/<item>/<field>

environment:
  CLOUDFLARE_BOOTSTRAP_TOKEN  token allowed to create API tokens (required)
  CLOUDFLARE_API_BASE         default https://api.cloudflare.com/client/v4
EOF
    exit 2
}

die() { echo "$*" >&2; exit 1; }

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
api="${CLOUDFLARE_API_BASE:-https://api.cloudflare.com/client/v4}"
account=""
name="hq-observer"
days=90
expires=""
ips=""
permissions="${script_dir}/cloudflare-observer-permissions.txt"
list_groups=0
print_secret=0
store=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --account) [ "$#" -ge 2 ] || usage; account="$2"; shift 2 ;;
        --name) [ "$#" -ge 2 ] || usage; name="$2"; shift 2 ;;
        --days) [ "$#" -ge 2 ] || usage; days="$2"; shift 2 ;;
        --expires) [ "$#" -ge 2 ] || usage; expires="$2"; shift 2 ;;
        --allow-ip) [ "$#" -ge 2 ] || usage; ips="${ips}$2
"; shift 2 ;;
        --permissions) [ "$#" -ge 2 ] || usage; permissions="$2"; shift 2 ;;
        --list-groups) list_groups=1; shift ;;
        --print-secret) print_secret=1; shift ;;
        --store) [ "$#" -ge 2 ] || usage; store="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
done

if [ -n "${store}" ]; then
    [ "${print_secret}" -eq 0 ] || die "--store and --print-secret are exclusive."
    # shellcheck source=scripts/lib/op-store.sh
    . "${script_dir}/lib/op-store.sh"
    op_store_parse "${store}"
fi

[ -n "${CLOUDFLARE_BOOTSTRAP_TOKEN:-}" ] \
    || die "Set CLOUDFLARE_BOOTSTRAP_TOKEN to a token that may create API tokens."
command -v jq >/dev/null || die "jq is required."
command -v curl >/dev/null || die "curl is required."

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM

# The bearer header travels over stdin, not argv.
cf() { # cf METHOD PATH [BODY_FILE]
    if [ "$#" -ge 3 ]; then
        printf 'Authorization: Bearer %s\n' "${CLOUDFLARE_BOOTSTRAP_TOKEN}" |
            curl -q --silent --show-error --proto '=https' --max-time 30 \
                --request "$1" --header @- \
                --header 'Content-Type: application/json' \
                --data-binary "@$3" "${api}$2"
    else
        printf 'Authorization: Bearer %s\n' "${CLOUDFLARE_BOOTSTRAP_TOKEN}" |
            curl -q --silent --show-error --proto '=https' --max-time 30 \
                --request "$1" --header @- "${api}$2"
    fi
}

# Exits with Cloudflare's own error messages when the last call did not succeed.
succeeded() { # succeeded WHAT
    jq -e 'type == "object" and .success == true' >/dev/null <"${work}/response" && return 0
    reason="$(jq -r '[.errors[]?.message] | join("; ")' <"${work}/response" 2>/dev/null || true)"
    die "Cloudflare refused $1: ${reason:-unreadable response}"
}

cf GET /user/tokens/permission_groups >"${work}/response"
succeeded "the permission group listing"

if [ "${list_groups}" -eq 1 ]; then
    jq -r '.result[] | "\(.name)\t\(.scopes | join(","))"' <"${work}/response" | sort
    exit 0
fi

[ -n "${account}" ] || usage
case "${account}" in *[!A-Za-z0-9]*) die "The account id is alphanumeric." ;; esac
[ -f "${permissions}" ] || die "Permission list not found: ${permissions}"

if [ -z "${expires}" ]; then
    case "${days}" in ''|*[!0-9]*) die "--days takes a whole number." ;; esac
    stamp=$(( $(date -u +%s) + days * 86400 ))
    expires="$(date -u -d "@${stamp}" +%Y-%m-%d 2>/dev/null \
        || date -u -r "${stamp}" +%Y-%m-%d)"
fi
case "${expires}" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *) die "--expires takes YYYY-MM-DD." ;;
esac

# Permission lines as "<name> (<account|zone>)"; comments and blanks skipped.
grep -v '^[[:space:]]*#' "${permissions}" | grep -v '^[[:space:]]*$' \
    | jq -R . | jq -s . >"${work}/wanted"
printf '%s' "${ips}" | grep -v '^$' | jq -R . | jq -s . >"${work}/ips"

jq --slurpfile wanted "${work}/wanted" --slurpfile ips "${work}/ips" \
    --arg account "${account}" --arg name "${name}" \
    --arg expires "${expires}T00:00:00Z" '
    def scope_of($level):
        if $level == "zone" then "com.cloudflare.api.account.zone"
        else "com.cloudflare.api.account" end;
    [ $wanted[0][]
      | capture("^(?<name>.+) \\((?<level>account|zone)\\)$") // {bad: .}
    ] as $asked
    | .result as $groups
    | [ $asked[] | select(.bad) | .bad ] as $malformed
    | [ $asked[] | select(.bad | not)
        | . as $want
        | { level: .level, line: "\(.name) (\(.level))",
            ids: [ $groups[] | select(.name == $want.name
                     and ((.scopes // []) | index(scope_of($want.level)))) | .id ] }
      ] as $resolved
    | [ $resolved[] | select(.ids | length != 1) | .line ] as $missing
    | if ($malformed | length) > 0 then
        {error: ("Malformed permission lines: " + ($malformed | join(", ")))}
      elif ($missing | length) > 0 then
        {error: ("Permission groups not found (or not unique): "
                 + ($missing | join(", "))
                 + ". Run with --list-groups to see the current names.")}
      else
        ( [ $resolved[] | select(.level == "account") | {id: .ids[0]} ] ) as $account_groups
        | ( [ $resolved[] | select(.level == "zone") | {id: .ids[0]} ] ) as $zone_groups
        | ("com.cloudflare.api.account." + $account) as $key
        | { name: $name,
            expires_on: $expires,
            policies: [
              (if ($account_groups | length) > 0 then
                 {effect: "allow", resources: {($key): "*"},
                  permission_groups: $account_groups} else empty end),
              (if ($zone_groups | length) > 0 then
                 {effect: "allow",
                  resources: {($key): {"com.cloudflare.api.account.zone.*": "*"}},
                  permission_groups: $zone_groups} else empty end)
            ] }
        + (if ($ips[0] | length) > 0
           then {condition: {"request.ip": {in: $ips[0]}}} else {} end)
      end
' <"${work}/response" >"${work}/body"

if jq -e 'has("error")' >/dev/null <"${work}/body"; then
    die "$(jq -r .error <"${work}/body")"
fi

if [ -n "${store}" ]; then
    op_store_check "${op_vault}" "${op_item}" "${op_field}"
elif [ "${print_secret}" -eq 0 ]; then
    jq -r '"Would create \(.name), expiring \(.expires_on), with "
        + "\([.policies[].permission_groups[]] | length) permission groups"
        + (if .condition then ", from \(.condition["request.ip"].in | join(", "))" else "" end)
        + "."' <"${work}/body" >&2
    echo "Nothing created. Rerun with --print-secret and pipe stdout into the" >&2
    echo "cloudflare_api connection's API_TOKEN field (docs/SECRETS.md)." >&2
    exit 0
fi

# The answer carries the secret, so it stays in memory rather than in ${work}.
created="$(cf POST /user/tokens "${work}/body")"
if ! printf '%s' "${created}" | jq -e 'type == "object" and .success == true' >/dev/null; then
    reason="$(printf '%s' "${created}" | jq -r '[.errors[]?.message] | join("; ")' 2>/dev/null || true)"
    die "Cloudflare refused the new token: ${reason:-unreadable response}"
fi

id="$(printf '%s' "${created}" | jq -r '.result.id // empty')"
[ -n "${id}" ] || die "Cloudflare returned no token id."
echo "Token id: ${id}" >&2
echo "Expires: $(printf '%s' "${created}" | jq -r '.result.expires_on // "never"')" >&2
if [ -n "${store}" ]; then
    printf '%s' "${created}" | jq -e --arg field "${op_field}" \
        '{($field): .result.value} | select(.[$field] | type == "string" and length > 0)' \
        | op_store_write "${op_vault}" "${op_item}" \
        || die "Token ${id} was created but not stored. Delete it under API Tokens and run this again."
    echo "Stored in ${op_item} (${op_vault}); the controller reads it on its next render." >&2
    exit 0
fi
printf '%s' "${created}" | jq -er '.result.value'
