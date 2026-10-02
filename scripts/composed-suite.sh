#!/usr/bin/env bash
# Runs the test suite inside a composed image and prints what is safe to publish.
#   scripts/composed-suite.sh IMAGE
# Extension tests name their models, routes and fixtures, and this repository's
# Actions logs are public. The log gets the totals, the ids of failing host tests
# and a count of failing extension tests; the full output goes to WITHHELD_DIR,
# which Compose seals for the operator (scripts/seal-failure-logs.sh).
# Run from the host checkout: a test id is the host's when its package is here.
set -euo pipefail

image="${1:?usage: composed-suite.sh IMAGE}"
if [ -n "${WITHHELD_DIR:-}" ]; then
  mkdir -p "$WITHHELD_DIR"
  output="$WITHHELD_DIR/composed-suite.log"
else
  output="$(mktemp)"
  trap 'rm -f "$output"' EXIT
fi
full="the full output is in this run's sealed failure logs (scripts/failure-logs.sh ${GITHUB_RUN_ID:-RUN_ID})"

status=0
docker run --rm --entrypoint python \
  --env DJANGO_SECRET_KEY=ci-only-composition-key-0123456789abcdef0123456789abcdef \
  --env DJANGO_ALLOWED_HOSTS=localhost \
  --env SEVERINO_LOG_LEVEL=WARNING \
  "$image" manage.py test --verbosity 1 --parallel auto >"$output" 2>&1 || status=$?

grep -E '^(Ran [0-9]+ tests? in|OK|FAILED)' "$output" || true
[ "$status" -eq 0 ] && exit 0

extension=0
while read -r kind id; do
  if [ -d "${id%%.*}" ]; then
    echo "::error title=Composed suite::${kind}: ${id}"
  else
    extension=$((extension + 1))
  fi
done < <(sed -nE 's/^(FAIL|ERROR): [^ ]+ \(([^ )]+)\)$/\1 \2/p' "$output")

if [ "$extension" -gt 0 ]; then
  echo "::error title=Composed suite::${extension} extension test(s) failed; ${full}."
fi
if ! grep -qE '^Ran [0-9]+ tests? in' "$output"; then
  # Only the exception's type: its message can name an extension's app or model.
  raised="$(grep -E '^[A-Za-z_.]+(Error|Exception|Configured)\b' "$output" | tail -1 | cut -d: -f1 || true)"
  echo "::error title=Composed suite did not run::${raised:-The image did not start the suite}; ${full}."
fi
exit "$status"
