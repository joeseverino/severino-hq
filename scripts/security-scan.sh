#!/bin/sh
# The security gates CI and code scanning run, before pushing rather than after
# merging.
#
# Each fails once `main` already has the commit, which is the expensive place to
# find out. Their settings are copied from the workflows rather than chosen
# here, so a local pass means what CI's does:
#
#   codeql     .github/workflows/codeql.yml     security-and-quality, less the
#              .github/codeql/codeql-config.yml one rule that config filters
#   scorecard  .github/workflows/scorecard.yml  every check local mode can
#                                               answer, at 10 (scripts/scorecard_report.py)
#   trivy      .github/workflows/ci.yml         HIGH,CRITICAL, fixable only
#
# CodeQL and Scorecard are the versions scripts/toolchain.env pins, fetched on
# first use by scripts/install-scan-tools.sh. Both read an export of the files
# git would push (tracked plus untracked, less ignored), so a local .venv or
# stray build output is not scanned.
#
# Usage:
#   ./scripts/security-scan.sh                   # CodeQL and Scorecard
#   ./scripts/security-scan.sh codeql            # one gate
#   ./scripts/security-scan.sh scorecard
#   ./scripts/security-scan.sh --image REF       # also Trivy, against REF
#   ./scripts/security-scan.sh --build           # also Trivy, building the host image
set -eu
unset CDPATH

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
# shellcheck source=scripts/toolchain.env
. ./scripts/toolchain.env

image=""
build=0
gates=""
while [ $# -gt 0 ]; do
    case "$1" in
        --image) image=${2:?--image needs a reference}; shift 2 ;;
        --build) build=1; shift ;;
        codeql | scorecard) gates="$gates $1"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
gates=${gates:-" codeql scorecard"}

python=${CHECK_PYTHON:-python3}
# Keyed by checkout, so worktrees do not overwrite each other's database.
key=$(printf '%s' "$repo_root" | cksum | cut -d' ' -f1)
work="${XDG_CACHE_HOME:-$HOME/.cache}/severino-hq/scan-$key"
src="$work/src"

echo "[security] Exporting the tree to scan"
rm -rf "$src"
mkdir -p "$src"
git -c core.quotepath=off ls-files --cached --others --exclude-standard \
    | while IFS= read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done \
    | tar -cf - -T - | tar -xf - -C "$src"

for gate in $gates; do
    case "$gate" in
    codeql)
        codeql=$(scripts/install-scan-tools.sh codeql)
        echo "[security] CodeQL $("$codeql" version --format=terse) database"
        # `--build-mode none` is what the Action uses for Python: nothing is
        # compiled, so the extractor reads the tree directly. The Action's own
        # config file gives both scans the same paths-ignore and query filters.
        "$codeql" database create "$work/codeql-db" \
            --language=python \
            --build-mode=none \
            --source-root="$src" \
            --codescanning-config="$src/.github/codeql/codeql-config.yml" \
            --threads=0 --quiet \
            --overwrite >/dev/null

        echo "[security] CodeQL analysis (security-and-quality)"
        "$codeql" database analyze "$work/codeql-db" \
            --format=sarif-latest \
            --output="$work/codeql.sarif" \
            --sarif-category=/language:python \
            --threads=0 --quiet \
            codeql/python-queries:codeql-suites/python-security-and-quality.qls >/dev/null

        # The rule the config filters is filtered here too, so local and CI
        # agree on what counts as a finding. See .github/codeql/codeql-config.yml.
        "$python" scripts/codeql_report.py "$work/codeql.sarif"
        echo "[security] CodeQL clean"
        ;;
    scorecard)
        scorecard=$(scripts/install-scan-tools.sh scorecard)
        echo "[security] Scorecard $SCORECARD_VERSION (local checks)"
        # No token: local mode reads files, and a token would let it reach
        # GitHub on this machine's credentials.
        env -u GITHUB_AUTH_TOKEN -u GITHUB_TOKEN -u GH_TOKEN -u GH_AUTH_TOKEN \
            "$scorecard" --local "$src" \
            --checks "$("$python" scripts/scorecard_report.py --checks)" \
            --show-details --format json --output "$work/scorecard.json" \
            2>"$work/scorecard.log" \
            || { cat "$work/scorecard.log" >&2; exit 1; }
        "$python" scripts/scorecard_report.py "$work/scorecard.json" "$src"
        echo "[security] Scorecard clean"
        ;;
    esac
done

if [ "$build" = "1" ] && [ -z "$image" ]; then
    image="severino-hq:security-scan"
    echo "[security] Building $image"
    docker build -t "$image" . >/dev/null
fi

if [ -z "$image" ]; then
    echo "[security] No image given; skipping Trivy. Pass --image REF or --build."
    exit 0
fi

if ! command -v trivy >/dev/null 2>&1; then
    echo "Trivy is not installed. brew install trivy" >&2
    exit 2
fi

echo "[security] Trivy scan of $image"
# The workflow's flags: fixable HIGH/CRITICAL only, non-zero on a hit.
trivy image \
    --severity HIGH,CRITICAL \
    --ignore-unfixed \
    --exit-code 1 \
    --format table \
    "$image"

echo "[security] all security checks passed"
