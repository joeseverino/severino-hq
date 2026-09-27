# shellcheck shell=sh
# Store minted credentials in an existing 1Password item. Source this; do not
# execute it. Runs on the operator's machine, where `op` authenticates through
# the desktop app.
#
# Contract:
#   op_store_parse <op://vault/item/field>   sets op_vault, op_item, op_field
#   op_store_check <vault> <item> <field...> exits unless each field is on the item once
#   op_store_write <vault> <item>            stdin: a JSON object of field -> value
#
# Values travel through pipes only: never argv, a file, or a log line. The item
# is read as JSON and piped back to `op item edit`, the documented template
# round trip, with only the named fields changed.

# Field selectors match a field's id or its label. jq, not shell, expands it.
# shellcheck disable=SC2016
_op_store_fields='def named($name): .id == $name or .label == $name;'

op_store_parse() {
    case "$1" in
        op://*) ;;
        *) echo "A store reference is op://<vault>/<item>/<field>." >&2; exit 2 ;;
    esac
    _rest="${1#op://}"
    op_vault="${_rest%%/*}"
    _rest="${_rest#*/}"
    op_item="${_rest%%/*}"
    op_field="${_rest#*/}"
    case "${op_vault}/${op_item}/${op_field}" in
        //*|*//*|*/) echo "A store reference is op://<vault>/<item>/<field>." >&2; exit 2 ;;
    esac
    case "${op_field}" in
        */*) echo "A store reference names a field directly, not a section." >&2; exit 2 ;;
    esac
}

op_store_check() {
    _vault="$1"; _item="$2"; shift 2
    command -v op >/dev/null || { echo "The 1Password CLI (op) is required to store." >&2; exit 1; }
    _names="$(printf '%s\n' "$@" | jq -R . | jq -sc .)"
    op item get "${_item}" --vault "${_vault}" --format json </dev/null \
        | jq -e --argjson names "${_names}" "${_op_store_fields}"'
            . as $item
            | all($names[]; . as $name
                | [$item.fields[]? | select(named($name))] | length == 1)
        ' >/dev/null \
        || { echo "The item ${_item} in ${_vault} does not carry each of: $*." >&2; exit 1; }
}

op_store_write() {
    _vault="$1"; _item="$2"
    # stdin is the values object, then the item: one stream, slurped.
    { cat; op item get "${_item}" --vault "${_vault}" --format json --reveal </dev/null; } \
        | jq -s "${_op_store_fields}"'
            .[0] as $values | .[1]
            | .fields |= map(. as $field
                | reduce ($values | keys[]) as $name ($field;
                    if named($name) then .value = $values[$name] else . end))
        ' \
        | op item edit "${_item}" --vault "${_vault}" >/dev/null
}
