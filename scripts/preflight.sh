#!/bin/sh
# The one command whose exit 0 means ready to release.
#
#   scripts/preflight.sh               # every gate, the deploy host included
#   scripts/preflight.sh --skip-host   # local gates only; never exits 0
#
# Runs, in order:
#   1. scripts/ci-local.sh, with every gate required (a gate it cannot run fails)
#   2. scripts/check.sh, with the composed pass required (SEVERINO_HQ_PLUGINS set)
#   3. scripts/preflight-host.sh on the deploy host over SSH: read-only checks
#      of the checkout's ownership, the runner's sudo rule, the root-owned
#      programs against this commit, the installed units and free disk.
#
# The host is SEVERINO_HQ_DEPLOY_HOST, an SSH destination, set in the
# environment or in .env.dev (see scripts/dev.env.example). The release is
# HEAD, which must be committed: a check of uncommitted files proves nothing
# about the commit that ships.
#
# Exit 0: ready. Exit 1: a gate failed. Exit 2: the host was skipped, so
# whatever else passed, this is not a verdict on the release.

set -eu

repo_root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "${repo_root}"

skip_host=0
case "${1:-}" in
    "") ;;
    --skip-host) skip_host=1 ;;
    *) echo "usage: scripts/preflight.sh [--skip-host]" >&2; exit 2 ;;
esac

if [ -f .env.dev ]; then
    set -a
    # shellcheck disable=SC1091  # optional, developer-local
    . ./.env.dev
    set +a
fi
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

gate "ci-local, every gate required" env CI_LOCAL_REQUIRE_ALL=1 scripts/ci-local.sh
gate "check.sh, composed pass required" env CHECK_REQUIRE_COMPOSED=1 scripts/check.sh

# The host half's inputs, derived from this commit and from main.
host_payload() {
    work="$1"
    mkdir -p "${work}/release" "${work}/main"
    git archive HEAD scripts config deploy docker-compose.yml | tar -x -C "${work}/release"
    git archive "${PREFLIGHT_BASE_REF:-origin/main}" deploy/systemd | tar -x -C "${work}/main"
    units_shipped "${work}/release/deploy/systemd" >"${work}/release-units"
    # Shipped by main and by this release: already installed, if the host is current.
    established="$(units_shipped "${work}/main/deploy/systemd" \
        | grep -Fx -f "${work}/release-units" | tr '\n' ' ' || true)"
    manifest="$(sh "${work}/release/scripts/root-tree-manifest.sh" "${work}/release")"
    min_free="$(sed -n 's/.*available_kb}" -lt \([0-9][0-9]*\).*/\1/p' \
        "${work}/release/scripts/deploy-image.sh")"
    [ -n "${min_free}" ] || { echo "Could not read deploy-image.sh's free-space floor." >&2; return 1; }
    case "${manifest}${established}" in
        *"'"*) echo "A shipped path contains a quote." >&2; return 1 ;;
    esac
    printf "PREFLIGHT_MANIFEST='%s'\n" "${manifest}"
    printf "PREFLIGHT_UNITS='%s'\n" "${established}"
    printf 'PREFLIGHT_MIN_FREE_KB=%s\n' "${min_free}"
    cat "${work}/release/scripts/lib/checkout.sh" "${work}/release/scripts/preflight-host.sh"
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

if [ "${skip_host}" -eq 1 ]; then
    record SKIPPED "deploy host (--skip-host)"
elif [ -z "${SEVERINO_HQ_DEPLOY_HOST:-}" ]; then
    record FAILED "deploy host: SEVERINO_HQ_DEPLOY_HOST is not set"
else
    gate "deploy host ${SEVERINO_HQ_DEPLOY_HOST}, read-only" host_check
fi

echo
echo "[preflight] summary"
printf '%s' "${results}"
if [ "${failed}" -ne 0 ] && ! printf '%s' "${results}" | grep -q '^  FAILED'; then
    echo
    echo "[preflight] HOST NOT CHECKED. Local gates passed; this is not a release verdict."
    exit 2
fi
if [ "${failed}" -ne 0 ]; then
    echo "[preflight] NOT READY"
    exit 1
fi
echo "[preflight] READY"
