#!/bin/sh
# Resolve provider connections from 1Password into controller environment
# variables. Secret values are written only to stdout; callers must redirect to
# a root-owned 0600 temporary file.
#
# The vault is the inventory. An item declares what it is -- `connection_ref`,
# `projection`, `env_prefix` -- and this reads them, so adding a provider
# connection is creating an item and touches no file here. The registry holds
# only the projections: the shapes a connection can take, which are generic.
#
# The registry holds only the projections: the shapes a connection can take,
# which are generic. It names no connection, so an item that declares neither a
# projection nor an env_prefix is an error here rather than something a second
# list could answer for.

set -eu

readonly vault="${1:?usage: render-controller-env.sh VAULT [REGISTRY]}"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
readonly registry="${2:-${script_dir}/../config/controller-connections.json}"

jq -e '
    .schema_version == 1
    and (.projections | type == "object")
    and all(
        .projections[];
        type == "object"
        and all(
            to_entries[];
            (.key | test("^[A-Z][A-Z0-9_]*$"))
            and (.value.source | IN("connection_ref", "url", "field"))
            and (
                if .value.source == "field" then
                    ([.value.id, .value.label]
                     | map(select(type == "string" and length > 0))
                     | length) == 1
                else true
                end
            )
        )
    )
' \
    "${registry}" >/dev/null

# One field off an item, by label, or empty.
item_field() {
    printf %s "$1" | jq -r --arg label "$2" \
        '[.fields[] | select(.label == $label)][0].value // ""'
}

emitted_refs=""
rendered=""

for item_id in $(op item list --vault "${vault}" --format json | jq -r '.[].id'); do
    item="$(op item get "${item_id}" --vault "${vault}" --format json)"
    connection_ref="$(item_field "${item}" connection_ref)"
    # Items without a connection_ref are not provider connections.
    [ -n "${connection_ref}" ] || continue

    case " ${emitted_refs} " in
        *" ${connection_ref} "*)
            echo "More than one 1Password item declares connection_ref=${connection_ref}." >&2
            exit 1
            ;;
    esac
    emitted_refs="${emitted_refs} ${connection_ref}"

    projection="$(item_field "${item}" projection)"
    prefix="$(item_field "${item}" env_prefix)"
    if [ -z "${projection}" ] || [ -z "${prefix}" ]; then
        echo "Connection ${connection_ref} declares no projection or env_prefix." >&2
        exit 1
    fi
    case "${prefix}" in
        [A-Z]*) ;;
        *)
            echo "Connection ${connection_ref} has an invalid env_prefix ${prefix}." >&2
            exit 1
            ;;
    esac
    if ! jq -e --arg projection "${projection}" \
        '.projections | has($projection)' "${registry}" >/dev/null; then
        echo "Connection ${connection_ref} names unknown projection ${projection}." >&2
        exit 1
    fi

    for env_name in $(
        jq -r --arg projection "${projection}" \
            '.projections[$projection] | keys[]' "${registry}"
    ); do
        source="$(
            jq -r --arg projection "${projection}" --arg name "${env_name}" \
                '.projections[$projection][$name].source' "${registry}"
        )"
        # A value a connection may or may not carry. `provider` is the case: it
        # classifies a connection so HQ can offer it for the right thing, and an
        # item that predates the field is still a connection the controller can
        # open. Required, adding the field would be a flag day across every item
        # at once.
        optional="$(
            jq -r --arg projection "${projection}" --arg name "${env_name}" \
                '.projections[$projection][$name].optional // false' "${registry}"
        )"
        case "${source}" in
            connection_ref)
                value="$(printf %s "${connection_ref}" | jq -Rr '@json')"
                ;;
            url)
                index="$(
                    jq -r --arg projection "${projection}" --arg name "${env_name}" \
                        '.projections[$projection][$name].index' "${registry}"
                )"
                value="$(
                    printf %s "${item}" \
                        | jq -r --argjson index "${index}" \
                            '.urls[$index].href // empty | @json'
                )"
                ;;
            field)
                selector_type="$(
                    jq -r --arg projection "${projection}" --arg name "${env_name}" \
                        '.projections[$projection][$name]
                         | if has("id") then "id" else "label" end' \
                        "${registry}"
                )"
                selector="$(
                    jq -r --arg projection "${projection}" --arg name "${env_name}" \
                        '.projections[$projection][$name]
                         | if has("id") then .id else .label end' \
                        "${registry}"
                )"
                field_count="$(
                    printf %s "${item}" \
                        | jq -r --arg selector_type "${selector_type}" --arg selector "${selector}" \
                            '[.fields[] | select(.[$selector_type] == $selector)] | length'
                )"
                if [ "${field_count}" -eq 0 ] && [ "${optional}" = "true" ]; then
                    continue
                fi
                if [ "${field_count}" -ne 1 ]; then
                    echo "Connection ${connection_ref} must contain exactly one field ${selector_type}=${selector}; found ${field_count}." >&2
                    exit 1
                fi
                value="$(
                    printf %s "${item}" \
                        | jq -r --arg selector_type "${selector_type}" --arg selector "${selector}" \
                            '[.fields[] | select(.[$selector_type] == $selector)][0].value // empty | @json'
                )"
                ;;
            *)
                echo "Unsupported projection source ${source}." >&2
                exit 1
                ;;
        esac
        if [ -z "${value}" ]; then
            if [ "${optional}" = "true" ]; then
                continue
            fi
            echo "Connection ${connection_ref} is missing ${env_name}." >&2
            exit 1
        fi
        rendered="${rendered}${prefix}_${env_name}=${value}
"
    done
done

# Sorted so the rendered file is byte-identical when nothing has changed, which
# is what lets the caller decide whether a restart is warranted.
printf '%s' "${rendered}" | sort
