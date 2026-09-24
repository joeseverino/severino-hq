#!/bin/sh
# check-private-names.sh refuses a private identifier in every public place it
# guards, in each of the three forms, and never prints the identifier itself.
set -eu

here="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
check="${here}/check-private-names.sh"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM
failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

COMPOSITION_EXTENSIONS="$(cat <<'JSON'
{"extensions": [{"repository": "owner/severino-hidden-ext", "id": "severino_hidden_ext"}]}
JSON
)"
export COMPOSITION_EXTENSIONS

for form in severino-hidden-ext severino.hidden.ext SEVERINO_HIDDEN_EXT; do
    if out="$(printf 'A title\nmentions %s here\n' "${form}" | sh "${check}" text "the description" 2>&1)"; then
        fail "text mode passed text containing ${form}"
    fi
    case "${out}" in *idden*) fail "text mode printed the identifier" ;; esac
done
printf 'nothing private here, only severino-hq\n' | sh "${check}" text "clean text" >/dev/null \
    || fail "text mode refused clean text"

git -C "${work}" init -q
printf 'clean\n' >"${work}/a.md"
git -C "${work}" add a.md
(cd "${work}" && sh "${check}" tree >/dev/null) || fail "tree mode refused a clean tree"
printf 'see severino.hidden.ext\n' >"${work}/b.md"
git -C "${work}" add b.md
if out="$(cd "${work}" && sh "${check}" tree 2>&1)"; then
    fail "tree mode passed a tracked file naming the extension"
fi
case "${out}" in *b.md*) ;; *) fail "tree mode did not say which file" ;; esac
case "${out}" in *idden.ext*) fail "tree mode printed the identifier" ;; esac

COMPOSITION_EXTENSIONS='' sh "${check}" text "anything" </dev/null >/dev/null \
    || fail "an unset inventory must not fail the build"

[ "${failures}" -eq 0 ] || exit 1
echo "private-name check: tree, commit and description text refused in every form"
