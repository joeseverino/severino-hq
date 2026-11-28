#!/usr/bin/env bash
# Whether the host already checked the tree an extension's push to main
# carries, on the pull request that merged it.
#   tools/pipeline/checked-on-host.sh HOST [COMMIT] >> "$GITHUB_OUTPUT"
# Run in the extension's repository; COMMIT is the pushed commit, GITHUB_SHA by
# default. Prints `pr=` and `gates=passed` when the merged pull request's head
# is this exact tree and HOST's last Check extensions run of that head
# (HOST's .github/workflows/extensions.yml), dispatched on HOST's main,
# passed. Otherwise it prints nothing and says why on stderr, and the
# admission checks in full. A run on main is the only one that counts: a
# dispatch can name any commit and only ever checks it, and a host branch's
# copy of the workflow proves nothing. Needs GH_TOKEN able to read the
# repository's pull requests; HOST's runs are public.
set -euo pipefail

host=${1:?usage: $0 HOST [COMMIT]}
commit="${2:-$GITHUB_SHA}"
# shellcheck source=tools/pipeline/merged-pr.sh
. "$(dirname -- "$0")/merged-pr.sh"

checked="$(gh api "repos/$repo/git/commits/$head" --jq .tree.sha 2>/dev/null || true)"
[ "$checked" = "$tree" ] || not_proven "#$pr's head is not this commit's tree"
last="$(gh api "repos/$host/actions/workflows/extensions.yml/runs?event=workflow_dispatch&branch=main&status=completed&per_page=100" \
  --jq "[.workflow_runs[] | select(.path == \".github/workflows/extensions.yml\" and .display_title == \"Check extensions at $head\")][0].conclusion // \"\"" \
  2>/dev/null || true)"
[ "$last" = success ] || not_proven "the host's last check of #$pr's head did not pass"
echo "pr=$pr"
echo "gates=passed"
