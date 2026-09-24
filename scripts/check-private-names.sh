#!/bin/sh
# Fails when a private extension's identifier appears in something public.
#
# The extensions are private repositories and this one is public. Their
# inventory lives in the COMPOSITION_EXTENSIONS secret rather than in the tree,
# so this file never has to name what it protects. Three things here are
# public and each has carried a name before: tracked files, commit messages,
# and a pull request's title and description.
#
#   check-private-names.sh tree            every tracked file
#   check-private-names.sh text LABEL      standard input, described as LABEL
#
# Matched on the repository and identifier forms (`severino-x`, `severino.x`,
# `severino_x`), case-insensitively, and never on the bare word: bare words are
# ordinary English, and a check with false positives gets switched off. A match
# is reported by where, never by what -- a CI log on a public repository is
# public too.
set -eu

mode="${1:-}"
if [ -z "${COMPOSITION_EXTENSIONS:-}" ]; then
    echo "COMPOSITION_EXTENSIONS is unset; nothing to check against"
    exit 0
fi

forms() {
    printf '%s' "${COMPOSITION_EXTENSIONS}" \
        | jq -r '[.extensions[] | (.repository | split("/") | last), .id] | unique | .[]' \
        | sed 's|.*/||' | grep -v '^$' \
        | while IFS= read -r stem; do
            printf '%s\n' "${stem}"
            printf '%s\n' "${stem}" | tr '-' '.'
            printf '%s\n' "${stem}" | tr '-' '_'
        done | LC_ALL=C sort -u
}

patterns="$(mktemp)"
trap 'rm -f "${patterns}"' EXIT HUP INT TERM
forms >"${patterns}"

case "${mode}" in
    tree)
        hits="$(git grep -Iil -F -f "${patterns}" -- . ':!LICENSE' || true)"
        if [ -n "${hits}" ]; then
            echo "::error::a private extension identifier appears in tracked files"
            printf '%s\n' "${hits}" | sed 's/^/  /'
            exit 1
        fi
        echo "no private extension identifiers in the tracked tree"
        ;;
    text)
        label="${2:?text mode needs a label}"
        if grep -qiF -f "${patterns}"; then
            echo "::error::a private extension identifier appears in ${label}"
            exit 1
        fi
        echo "no private extension identifiers in ${label}"
        ;;
    *)
        echo "usage: $0 tree | text LABEL" >&2
        exit 2
        ;;
esac
