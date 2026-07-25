#!/bin/sh
# Contract test for stable-label and stable-ID 1Password projections.

set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
fixture_dir="$(mktemp -d)"
readonly fixture_dir
trap 'rm -rf "${fixture_dir}"' EXIT HUP INT TERM

cat >"${fixture_dir}/registry.json" <<'JSON'
{
  "schema_version": 1,
  "projections": {
    "api_token": {
      "CONNECTION_REF": {"source": "connection_ref"},
      "URL": {"source": "field", "label": "website"},
      "API_TOKEN": {"source": "field", "id": "credential"}
    }
  },
  "connections": {
    "example": {
      "provider": "example",
      "projection": "api_token",
      "env_prefix": "EXAMPLE"
    }
  }
}
JSON

cat >"${fixture_dir}/op" <<'SH'
#!/bin/sh
set -eu
if [ "$1 $2" = "item list" ]; then
    printf '%s\n' '[{"id":"item-1"}]'
    exit 0
fi
if [ "$1 $2" = "item get" ]; then
    cat <<'JSON'
{"fields":[
  {"id":"opaque-item-id","label":"connection_ref","value":"example"},
  {"id":"credential","label":"credential","value":"test-token"},
  {"id":"random-per-item-id","label":"website","value":"https://api.example.test"}
]}
JSON
    exit 0
fi
exit 1
SH
chmod 0700 "${fixture_dir}/op"

actual="$(
    PATH="${fixture_dir}:${PATH}" \
        "${script_dir}/render-controller-env.sh" \
        test-vault "${fixture_dir}/registry.json"
)"
expected='EXAMPLE_API_TOKEN="test-token"
EXAMPLE_CONNECTION_REF="example"
EXAMPLE_URL="https://api.example.test"'

if [ "${actual}" != "${expected}" ]; then
    echo "Controller projection contract produced unexpected output." >&2
    exit 1
fi

echo "Controller projection contract passed."
