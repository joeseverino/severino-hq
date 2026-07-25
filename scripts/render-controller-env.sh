#!/bin/sh
# Resolve provider Login items by their stable connection_ref field.
# Secret values are written only to stdout; callers must redirect to a
# root-owned 0600 temporary file.

set -eu

readonly vault="${1:?usage: render-controller-env.sh VAULT [REGISTRY]}"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
readonly registry="${2:-${script_dir}/../config/controller-connections.json}"

jq -e '
    .schema_version == 1
    and (.projections | type == "object")
    and (.connections | type == "object")
    and all(
        .connections[];
        (.env_prefix | test("^[A-Z][A-Z0-9_]*$"))
        and (.projection | type == "string")
    )
    and all(
        .projections[];
        type == "object"
        and all(
            to_entries[];
            (.key | test("^[A-Z][A-Z0-9_]*$"))
            and (.value.source | IN("connection_ref", "url", "field"))
        )
    )
    and (
        [.connections[].projection] - (.projections | keys)
        | length == 0
    )
' \
    "${registry}" >/dev/null

item_ids="$(op item list --vault "${vault}" --format json | jq -r '.[].id')"

for connection_ref in $(jq -r '.connections | keys[]' "${registry}"); do
    match=""
    match_count=0
    for item_id in ${item_ids}; do
        item="$(op item get "${item_id}" --vault "${vault}" --format json)"
        item_ref="$(
            printf %s "${item}" \
                | jq -r '[.fields[] | select(.label == "connection_ref")][0].value // ""'
        )"
        if [ "${item_ref}" = "${connection_ref}" ]; then
            match="${item}"
            match_count=$((match_count + 1))
        fi
    done

    if [ "${match_count}" -ne 1 ]; then
        echo "Expected exactly one 1Password item for connection_ref=${connection_ref}; found ${match_count}." >&2
        exit 1
    fi

    prefix="$(
        jq -r --arg ref "${connection_ref}" \
            '.connections[$ref].env_prefix' "${registry}"
    )"
    projection="$(
        jq -r --arg ref "${connection_ref}" \
            '.connections[$ref].projection' "${registry}"
    )"
    for env_name in $(
        jq -r --arg projection "${projection}" \
            '.projections[$projection] | keys[]' "${registry}"
    ); do
        source="$(
            jq -r --arg projection "${projection}" --arg name "${env_name}" \
                '.projections[$projection][$name].source' "${registry}"
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
                    printf %s "${match}" \
                        | jq -r --argjson index "${index}" \
                            '.urls[$index].href // empty | @json'
                )"
                ;;
            field)
                field_id="$(
                    jq -r --arg projection "${projection}" --arg name "${env_name}" \
                        '.projections[$projection][$name].id' "${registry}"
                )"
                value="$(
                    printf %s "${match}" \
                        | jq -r --arg id "${field_id}" \
                            '[.fields[] | select(.id == $id)][0].value // empty | @json'
                )"
                ;;
            *)
                echo "Unsupported projection source ${source}." >&2
                exit 1
                ;;
        esac
        if [ -z "${value}" ]; then
            echo "Connection ${connection_ref} is missing ${env_name}." >&2
            exit 1
        fi
        printf '%s_%s=%s\n' "${prefix}" "${env_name}" "${value}"
    done
done
