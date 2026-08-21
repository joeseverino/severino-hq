#!/bin/sh
# Contract test for the controller environment projection.
#
# Covers both directions: an item that declares its own connection (the vault is
# the inventory) and one that does not (the registry still names it).

set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
fixture_dir="$(mktemp -d)"
readonly fixture_dir
trap 'rm -rf "${fixture_dir}"' EXIT HUP INT TERM

failures=0

# A registry with projections only -- no connections. This is the shape the
# repository should end up carrying.
cat >"${fixture_dir}/projections-only.json" <<'JSON'
{
  "schema_version": 1,
  "projections": {
    "api_token": {
      "CONNECTION_REF": {"source": "connection_ref"},
      "URL": {"source": "field", "label": "website"},
      "API_TOKEN": {"source": "field", "id": "credential"},
      "PROVIDER": {"source": "field", "label": "provider", "optional": true}
    }
  }
}
JSON

# $1 = fixture name, $2 = the JSON `op item get` should return
write_op() {
    cat >"${fixture_dir}/op" <<SH
#!/bin/sh
set -eu
if [ "\$1 \$2" = "item list" ]; then
    printf '%s\n' '${1}'
    exit 0
fi
if [ "\$1 \$2" = "item get" ]; then
    cat <<'ITEM_JSON'
${2}
ITEM_JSON
    exit 0
fi
exit 1
SH
    chmod 0700 "${fixture_dir}/op"
}

check() { # check <name> <registry> <expected-status> <expected-output-or-pattern>
    name="$1"; registry="$2"; want_status="$3"; want="$4"
    set +e
    actual="$(PATH="${fixture_dir}:${PATH}" \
        "${script_dir}/render-controller-env.sh" test-vault "${registry}" 2>&1)"
    status=$?
    set -e
    if [ "${status}" -ne "${want_status}" ]; then
        echo "FAIL ${name}: exit ${status}, wanted ${want_status}" >&2
        echo "${actual}" | sed 's/^/    /' >&2
        failures=$((failures + 1))
        return
    fi
    case "${actual}" in
        *"${want}"*) echo "ok   ${name}" ;;
        *)
            echo "FAIL ${name}: output did not contain '${want}'" >&2
            echo "${actual}" | sed 's/^/    /' >&2
            failures=$((failures + 1))
            ;;
    esac
}

# 1. The vault is the inventory: the item declares projection and env_prefix,
#    and the registry names no connections at all.
write_op '[{"id":"item-1"}]' '{"fields":[
  {"id":"a","label":"connection_ref","value":"example"},
  {"id":"b","label":"projection","value":"api_token"},
  {"id":"c","label":"env_prefix","value":"EXAMPLE"},
  {"id":"credential","label":"credential","value":"test-token"},
  {"id":"d","label":"website","value":"https://api.example.test"}
]}'
check "item declares its own connection" "${fixture_dir}/projections-only.json" 0 \
    'EXAMPLE_API_TOKEN="test-token"'

# 3. An item that declares no projection: a loud failure, not a silent omission.
#    The registry names no connections, so nothing can answer for it.
write_op '[{"id":"item-1"}]' '{"fields":[
  {"id":"a","label":"connection_ref","value":"example"},
  {"id":"credential","label":"credential","value":"test-token"},
  {"id":"d","label":"website","value":"https://api.example.test"}
]}'
check "undeclared connection fails" "${fixture_dir}/projections-only.json" 1 \
    "declares no projection or env_prefix"

# 4. A projection the registry does not define.
write_op '[{"id":"item-1"}]' '{"fields":[
  {"id":"a","label":"connection_ref","value":"example"},
  {"id":"b","label":"projection","value":"nonexistent"},
  {"id":"c","label":"env_prefix","value":"EXAMPLE"}
]}'
check "unknown projection fails" "${fixture_dir}/projections-only.json" 1 \
    "names unknown projection"

# 5. Two items claiming the same connection is ambiguous, never last-wins.
write_op '[{"id":"item-1"},{"id":"item-2"}]' '{"fields":[
  {"id":"a","label":"connection_ref","value":"example"},
  {"id":"b","label":"projection","value":"api_token"},
  {"id":"c","label":"env_prefix","value":"EXAMPLE"},
  {"id":"credential","label":"credential","value":"test-token"},
  {"id":"d","label":"website","value":"https://api.example.test"}
]}'
check "duplicate connection_ref fails" "${fixture_dir}/projections-only.json" 1 \
    "More than one 1Password item declares"

# 6. An optional value the item does carry is rendered.
write_op '[{"id":"item-1"}]' '{"fields":[
  {"id":"a","label":"connection_ref","value":"example"},
  {"id":"b","label":"projection","value":"api_token"},
  {"id":"c","label":"env_prefix","value":"EXAMPLE"},
  {"id":"e","label":"provider","value":"portainer"},
  {"id":"credential","label":"credential","value":"test-token"},
  {"id":"d","label":"website","value":"https://api.example.test"}
]}'
check "an optional field is rendered when present" "${fixture_dir}/projections-only.json" 0 \
    'EXAMPLE_PROVIDER="portainer"'

# 7. The same item without it renders the rest rather than failing. This is what
#    lets a connection be classified one item at a time instead of all at once.
write_op '[{"id":"item-1"}]' '{"fields":[
  {"id":"a","label":"connection_ref","value":"example"},
  {"id":"b","label":"projection","value":"api_token"},
  {"id":"c","label":"env_prefix","value":"EXAMPLE"},
  {"id":"credential","label":"credential","value":"test-token"},
  {"id":"d","label":"website","value":"https://api.example.test"}
]}'
check "an absent optional field is not fatal" "${fixture_dir}/projections-only.json" 0 \
    'EXAMPLE_API_TOKEN="test-token"'

# 8. Items that are not provider connections are ignored, not fatal.
write_op '[{"id":"item-1"}]' '{"fields":[
  {"id":"a","label":"username","value":"someone"}
]}'
check "unrelated vault item is skipped" "${fixture_dir}/projections-only.json" 0 ""

# 9. No script may still read a registry key the registry no longer has. The
#    renderer is covered by the cases above; every other script that reads this
#    file is not, and one of them recomputing from `.connections` failed at
#    deploy time rather than here.
for script in "${script_dir}"/*.sh; do
    if grep -q '\.connections\[' "${script}"; then
        echo "FAIL ${script##*/} reads .connections; the vault is the inventory." >&2
        failures=$((failures + 1))
    fi
done
present="$(jq -r '[.projections | keys[]] | join(",")' \
    "${script_dir}/../config/controller-connections.json")"
case "${present}" in
    *acme*|*api_token*|*login*|*ssh_transport*) echo "ok   registry declares projections" ;;
    *) echo "FAIL registry declares no projections" >&2; failures=$((failures + 1)) ;;
esac

if [ "${failures}" -ne 0 ]; then
    echo "Controller projection contract failed (${failures})." >&2
    exit 1
fi
echo "Controller projection contract passed."
