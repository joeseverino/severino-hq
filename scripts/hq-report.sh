#!/usr/bin/env bash
# Where a deploy ended up, said on its commit as HQ's app: live, or not
# deployed, why and what fixes it. Run by Deploy's Report job, whatever the
# deploy did. On success it also comments once on the merged pull request.
#
# The diagnosis comes from scripts/diagnose.py, which prints only the
# catalog's own words, never a line of the deploy's log.
#
# Environment:
#   GH_TOKEN      this run's token (actions: read), to read the deploy's log
#   APP_TOKEN     HQ's app token (checks and pull requests: write)
#   COMMIT        the commit deployed
#   IMAGE         the composition, by digest
#   RESULT        the Deploy job's result
#   THIS_RUN      this run's id
#   RUN_URL       this run's page
#   COMPOSE_RUN   the Compose run that published it, when it started this
set -euo pipefail

readonly check_name="Severino HQ · Production"
readonly repo="${GITHUB_REPOSITORY:?}"
readonly marker="<!-- severino-hq-delivery:${COMMIT:?} -->"

case "${RESULT:?}" in
  success)
    conclusion=success
    title="Live in production"
    summary="Production runs this commit, and it passed its health check." ;;
  cancelled)
    conclusion=cancelled
    title="Not deployed: cancelled"
    summary="The deploy was cancelled. Production still runs the previous image." ;;
  *)
    # Not approved, or approved and failed. GitHub records which: asked, not
    # inferred from a log that may simply not be downloadable yet.
    approved="$(gh api "repos/${repo}/actions/runs/${THIS_RUN}/approvals" \
      --jq 'map(select(.state == "approved")) | length' 2>/dev/null || echo 0)"
    if [ "${approved:-0}" = "0" ]; then
      conclusion=neutral
      title="Not deployed: not approved"
      summary="The deploy did not run. Production still runs the previous image. Run Deploy again to deploy this commit."
    else
      # A finished job's log can take a few seconds to be served.
      job="$(gh api "repos/${repo}/actions/runs/${THIS_RUN}/jobs?per_page=100" \
        --jq '[.jobs[] | select(.name == "Deploy")] | first | .id // empty')"
      logs=""
      for _ in 1 2 3 4 5 6; do
        [ -n "${job}" ] && logs="$(scripts/job-log.sh "${job}" 2>/dev/null || true)"
        [ -n "${logs}" ] && break
        sleep 5
      done
      diagnosis="$(printf '%s' "${logs}" | python3 scripts/diagnose.py)"
      conclusion=failure
      title="Not deployed: $(printf '%s' "${diagnosis}" | jq -r .title)"
      summary="$(printf '%s' "${diagnosis}" | jq -r .fix)"
    fi ;;
esac

# The details, from GitHub's own record of this delivery: every value here is
# already public on this repository.
server="${GITHUB_SERVER_URL:-https://github.com}"
headline="$(gh api "repos/${repo}/commits/${COMMIT}" --jq '.commit.message | split("\n") | first' 2>/dev/null || true)"
approval="$(gh api "repos/${repo}/actions/runs/${THIS_RUN}/approvals" \
  --jq 'first | select(. != null) | "@\(.user.login)"' 2>/dev/null || true)"
took="$(gh api "repos/${repo}/actions/runs/${THIS_RUN}/jobs?per_page=100" \
  --jq '[.jobs[] | select(.name == "Deploy" and .started_at and .completed_at)] | first
        | select(. != null) | ((.completed_at | fromdate) - (.started_at | fromdate))
        | "\(. / 60 | floor)m \(. % 60)s"' 2>/dev/null || true)"
ci_url="$(gh api "repos/${repo}/actions/runs?head_sha=${COMMIT}&per_page=30" \
  --jq '[.workflow_runs[] | select(.name == "CI")] | first | .html_url // empty' 2>/dev/null || true)"
digest="${IMAGE##*@}"
image_name="${IMAGE%@*}"
pipeline="[Deploy](${RUN_URL})"
[[ "${COMPOSE_RUN:-}" =~ ^[0-9]+$ ]] && pipeline="[Compose](${server}/${repo}/actions/runs/${COMPOSE_RUN}) → ${pipeline}"
[ -n "${ci_url}" ] && pipeline="[CI](${ci_url}) → ${pipeline}"
case "${conclusion}" in
  success) outcome="Healthy${took:+ in ${took}}" ;;
  *) outcome="${title#Not deployed: }" ;;
esac
text="$(cat <<TABLE
| | |
| --- | --- |
| **Change** | [\`${COMMIT:0:7}\`](${server}/${repo}/commit/${COMMIT}) ${headline} |
| **Image** | \`${image_name##*/}@${digest:0:19}…\` |
| **Approved** | ${approval:-not recorded} |
| **Deploy** | ${outcome} |
| **Pipeline** | ${pipeline} |

<details><summary>Full image reference</summary>

\`\`\`
${IMAGE}
\`\`\`

</details>
TABLE
)"

# The check this delivery opened on the commit, moved to where it ended up.
CHECK_TEXT="${text}" CHECK_URL="${RUN_URL}" GH_TOKEN="${APP_TOKEN}" \
  scripts/hq-check.sh "${check_name}" "${COMMIT}" completed "${title}" "${summary}" "${conclusion}"
echo "${title}"

[ "${conclusion}" = success ] || exit 0
# One comment on the merged pull request, the first time its commit is live;
# the marker is the one HQ's controller uses on an extension's.
as_app() { GH_TOKEN="${APP_TOKEN:?}" gh api "$@"; }
pull="$(as_app "repos/${repo}/commits/${COMMIT}/pulls" --jq '[.[] | select(.merged_at)] | first | .number // empty')"
[ -n "${pull}" ] || exit 0
if as_app "repos/${repo}/issues/${pull}/comments?per_page=100" --jq '.[].body' | grep -qF "${marker}"; then
  exit 0
fi
jq -n --arg body "${marker}
**Live in production.** \`${IMAGE}\` passed its health check." '{body: $body}' \
  | as_app -X POST "repos/${repo}/issues/${pull}/comments" --input - >/dev/null
