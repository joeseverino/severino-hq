#!/bin/sh
# The one command whose exit 0 means ready to release.
#
#   mise run preflight               # every gate, the deploy host included
#   mise run preflight --remote      # the heavy gates read from GitHub instead
#   mise run preflight --skip-host   # local gates only; never exits 0
#
# The task's usage declares the flags; mise hands them over as usage_remote
# and usage_skip_host.
#
# Runs, in order:
#   1. `mise run ci`: every gate the pipeline runs (one that cannot run fails);
#      or, with --remote, what GitHub ran on this exact commit, waited for and
#      required to pass: CI's newest run and every job its verdict needs, on
#      main the Compose it starts, and every other run and check. The same
#      gates, on GitHub's machines. The commit must be pushed.
#   2. `mise run suite:as-image`, the extensions required: the composed suite
#      over only the files the image ships, configured as the image is
#   3. scripts/preflight-host.sh on the deploy host over SSH: read-only checks
#      of the runner's sudo rule, the host against the release it runs
#      (`hq-host verify`, or the drift unit's run of it since the release was
#      installed), the installed units and free disk.
#
# The host is SEVERINO_HQ_DEPLOY_HOST, an SSH destination, set in the
# environment or in mise.local.toml (see scripts/mise.local.example.toml). The release is
# HEAD, which must be committed: a check of uncommitted files proves nothing
# about the commit that ships.
#
# Exit 0: ready. Exit 1: a gate failed. Exit 2: the host was skipped, or its
# release could not be verified, so whatever else passed, this is not a
# verdict on the release.

set -eu

repo_root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "${repo_root}"

# shellcheck source=scripts/lib/systemd-units.sh
. ./scripts/lib/systemd-units.sh

results=""
failed=0
record() { # record <status> <gate>
    results="${results}$(printf '  %-8s %s' "$1" "$2")
"
    [ "$1" = passed ] || failed=1
}
gate() { # gate <label> <command...>
    label="$1"
    shift
    echo "[preflight] ${label}"
    if "$@"; then record passed "${label}"; else record FAILED "${label}"; fi
}

if [ -n "$(git status --porcelain)" ]; then
    record FAILED "committed tree (uncommitted changes are not part of the release)"
else
    record passed "committed tree at $(git rev-parse --short HEAD)"
fi

# What GitHub ran on HEAD, once all of it has finished: CI's newest run, and on
# main the Compose run CI's Release starts; every other workflow run on the
# commit; and every check run. Deploy and the Production check are left out:
# they wait for an approval this gate comes before. Something not started yet
# is waiting, not passing, and the deadline turns waiting into a failure.
readonly tab='	'
readonly ci_path=.github/workflows/ci.yml
readonly compose_path=.github/workflows/compose.yml

# One row per workflow, its newest run: path, created, id, status, conclusion, event.
latest_runs() {
    all="$(command gh api --paginate "repos/${repository}/actions/runs?head_sha=${sha}&per_page=100" \
        --jq '.workflow_runs[] | [(.path | sub("@.*"; "")), .created_at, .id, .status,
            (.conclusion // "-"), .event] | @tsv')" || return 1
    printf '%s\n' "${all}" | grep -v "^.github/workflows/deploy.yml${tab}" \
        | sort -t "${tab}" -k1,1 -k2,2r | awk -F'\t' 'NF && !seen[$1]++'
}

check_runs() { # status, conclusion, name
    command gh api --paginate "repos/${repository}/commits/${sha}/check-runs?filter=latest&per_page=100" \
        --jq '.check_runs[] | select(.name != "Severino HQ · Production")
            | [.status, (.conclusion // "-"), .name] | @tsv'
}

# Prints what is still missing or running, and the id of a run to watch.
unsettled() {
    ci="$(printf '%s\n' "${runs}" | awk -F'\t' -v p="${ci_path}" '$1 == p')"
    if [ -z "${ci}" ]; then
        echo "no CI run on this commit"
    elif [ "$(printf '%s' "${ci}" | cut -f6)" = push ] &&
        ! printf '%s\n' "${runs}" | cut -f1 | grep -qxF "${compose_path}"; then
        echo "CI's Release has not started Compose"
    fi
    printf '%s\n' "${runs}" | awk -F'\t' 'NF && $4 != "completed" { print "watch " $3; print $1 " is " $4 }'
    printf '%s\n' "${checks}" | awk -F'\t' 'NF && $1 != "completed" { print "check " $3 " is " $1 }'
}

# The jobs CI's verdict job needs (scripts/hq-verdict.sh), by the name GitHub
# shows, from ci.yml at this commit; each marked when ci.yml skips it on a
# push whose pull request already proved the tree.
required_jobs() { # required_jobs <verdict job id>
    # shellcheck disable=SC2016  # $j, $verdict and $id are yq's own variables
    git show "${sha}:${ci_path}" | VERDICT_JOB="$1" yq -r '.jobs as $j
        | $j[strenv(VERDICT_JOB)] as $verdict | ($verdict.name, ($verdict.needs[]
        | select(. != "proven") | . as $id
        | [(($j[$id].name // $id) | split(" (") | .[0]),
           (($j[$id].if // "") | test("needs.proven.outputs.digest =="))] | @tsv))'
}

# Every required job of CI's latest attempt ran and passed, or was skipped
# because the image its pull request proved was promoted.
ci_jobs() { # ci_jobs <run id> <event>
    case "$2" in
        pull_request) verdict_job=ready ;;
        push) verdict_job=release ;;
        *) echo "  CI ran on ${2}, which has no verdict job"; return 1 ;;
    esac
    promote="$(git show "${sha}:${ci_path}" | yq -r '.jobs.image.steps[] | select(.id == "promote") | .name')"
    required_jobs "${verdict_job}" >"${work}/required" && [ -s "${work}/required" ] || {
        echo "  could not read the jobs CI's ${verdict_job} needs from ${ci_path}"
        return 1
    }
    command gh api --paginate "repos/${repository}/actions/runs/$1/jobs?filter=latest&per_page=100" \
        --jq '.jobs[] | [(.conclusion // .status), .name,
            (if any(.steps[]?; .name == "'"${promote}"'" and .conclusion == "success")
             then "promoted" else "-" end)] | @tsv' >"${work}/jobs"
    awk -F'\t' '
        FNR == NR { if (NF == 1) { $2 = "false" } want[$1] = $2; next }
        { job[FNR] = $2; result[FNR] = $1; if ($3 == "promoted") promoted = 1; jobs = FNR }
        END {
            for (name in want) {
                seen = 0
                for (i = 1; i <= jobs; i++) {
                    if (job[i] != name && index(job[i], name " (") != 1 && index(job[i], name " / ") != 1) continue
                    seen = 1
                    if (result[i] == "success" || (result[i] == "skipped" && want[name] == "true" && promoted)) continue
                    print "  required job " job[i] ": " result[i]
                    failed = 1
                }
                if (!seen) { print "  required job " name ": did not run"; failed = 1 }
            }
            exit failed
        }' "${work}/required" "${work}/jobs"
}

verdict() {
    status=0
    printf '%s\n' "${runs}" | awk -F'\t' 'NF { print "  " $5 "  " $1 " (" $6 ", run " $3 ")" }'
    printf '%s\n' "${checks}" | awk -F'\t' 'NF { print "  " $2 "  " $3 }'
    if printf '%s\n' "${runs}" | awk -F'\t' -v ci="${ci_path}" -v compose="${compose_path}" '
        NF && (($1 == ci || $1 == compose) ? $5 != "success" : $5 !~ /^(success|skipped|neutral)$/)' | grep -q .; then
        echo "  a workflow run did not pass"
        status=1
    fi
    if printf '%s\n' "${checks}" | awk -F'\t' 'NF && $2 !~ /^(success|skipped|neutral)$/' | grep -q .; then
        echo "  a check did not pass"
        status=1
    fi
    ci="$(printf '%s\n' "${runs}" | awk -F'\t' -v p="${ci_path}" '$1 == p')"
    ci_jobs "$(printf '%s' "${ci}" | cut -f3)" "$(printf '%s' "${ci}" | cut -f6)" || status=1
    return "${status}"
}

github_gates() {
    sha="$(git rev-parse HEAD)"
    if [ -z "$(git branch -r --contains "${sha}" 2>/dev/null)" ]; then
        echo "HEAD is not pushed, so GitHub has checked nothing of it." >&2
        return 1
    fi
    repository="$(command gh repo view --json nameWithOwner --jq .nameWithOwner)"
    deadline=$(($(date +%s) + ${PREFLIGHT_REMOTE_TIMEOUT:-3600}))
    poll="${PREFLIGHT_REMOTE_POLL:-30}"
    while :; do
        runs="$(latest_runs)" || return 1
        checks="$(check_runs)" || return 1
        waiting="$(unsettled)"
        [ -n "${waiting}" ] || break
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            echo "GitHub did not finish with this commit in time:" >&2
            printf '%s\n' "${waiting}" | grep -v '^watch ' | sed 's/^/  /' >&2
            return 1
        fi
        running="$(printf '%s\n' "${waiting}" | sed -n 's/^watch //p' | head -n 1)"
        if [ -n "${running}" ]; then
            command gh run watch "${running}" -R "${repository}" --interval "${poll}" >/dev/null 2>&1 || :
        else
            sleep "${poll}"
        fi
    done
    work="$(mktemp -d)"
    status=0
    verdict || status=$?
    rm -rf "${work}"
    return "${status}"
}

if [ -n "${usage_remote:-}" ]; then
    gate "every GitHub check on this commit" github_gates
else
    gate "every gate the pipeline runs" mise run -c ci
fi
gate "the composed suite, as the image runs it" env REQUIRE_COMPOSED=1 mise run suite:as-image

# The host half's inputs, derived from this commit and from main.
host_payload() {
    work="$1"
    mkdir -p "${work}/release" "${work}/main"
    git archive HEAD scripts/preflight-host.sh deploy/systemd controller/cmd/hq-host/main.go | tar -x -C "${work}/release"
    git archive "${PREFLIGHT_BASE_REF:-origin/main}" deploy/systemd | tar -x -C "${work}/main"
    units_shipped "${work}/release/deploy/systemd" >"${work}/release-units"
    # Shipped by main and by this release: already installed, if the host is current.
    established="$(units_shipped "${work}/main/deploy/systemd" \
        | grep -Fx -f "${work}/release-units" | tr '\n' ' ' || true)"
    min_free_mib="$(sed -n 's/.*FreeBytes: \([0-9][0-9]*\) << 20,.*/\1/p' \
        "${work}/release/controller/cmd/hq-host/main.go")"
    [ -n "${min_free_mib}" ] || { echo "Could not read hq-host's free-space floor." >&2; return 1; }
    min_free=$((min_free_mib * 1024))
    case "${established}" in
        *"'"*) echo "A shipped unit name contains a quote." >&2; return 1 ;;
    esac
    printf "PREFLIGHT_UNITS='%s'\n" "${established}"
    printf 'PREFLIGHT_MIN_FREE_KB=%s\n' "${min_free}"
    cat "${work}/release/scripts/preflight-host.sh"
}

host_check() {
    work="$(mktemp -d)"
    status=0
    if host_payload "${work}" >"${work}/payload"; then
        ssh -o BatchMode=yes "${SEVERINO_HQ_DEPLOY_HOST}" sh -s <"${work}/payload" || status=$?
    else
        status=1
    fi
    rm -rf "${work}"
    return "${status}"
}

if [ -n "${usage_skip_host:-}" ]; then
    record SKIPPED "deploy host (--skip-host)"
elif [ -z "${SEVERINO_HQ_DEPLOY_HOST:-}" ]; then
    record FAILED "deploy host: SEVERINO_HQ_DEPLOY_HOST is not set"
else
    label="deploy host ${SEVERINO_HQ_DEPLOY_HOST}, read-only"
    echo "[preflight] ${label}"
    status=0
    host_check || status=$?
    # 3: the host half found nothing wrong but could not verify the release.
    case "${status}" in
        0) record passed "${label}" ;;
        3) record UNKNOWN "${label}: the release it runs is unverified" ;;
        *) record FAILED "${label}" ;;
    esac
fi

echo
echo "[preflight] summary"
printf '%s' "${results}"
if [ "${failed}" -ne 0 ] && ! printf '%s' "${results}" | grep -q '^  FAILED'; then
    echo
    echo "[preflight] HOST NOT VERIFIED. Every other gate passed; this is not a release verdict."
    exit 2
fi
if [ "${failed}" -ne 0 ]; then
    echo "[preflight] NOT READY"
    exit 1
fi
echo "[preflight] READY"
