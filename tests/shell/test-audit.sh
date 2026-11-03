#!/bin/sh
# Prove the audit runs uv's on the named project alone, and on the host runs
# uv's on its project and osv-scanner on every lockfile git tracks, failing
# when either finds something.
set -eu

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
shell="${SHELL_UNDER_TEST:-/bin/sh}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/package"

# uv: an audit records the project it was given, and fails when UV_FINDS is set.
cat >"$work/bin/uv" <<EOF
#!/bin/sh
command=\$1; shift
project=""
while [ "\$#" -gt 0 ]; do
    case "\$1" in
        --project) project=\$2; shift 2 ;;
        *) shift ;;
    esac
done
[ "\$command" = audit ] || exit 2
echo "\$project" >"$work/audited"
[ -z "\${UV_FINDS:-}" ]
EOF
# osv-scanner: a scan records each lockfile, one a line, and fails when
# OSV_FINDS is set.
cat >"$work/bin/osv-scanner" <<EOF
#!/bin/sh
[ "\$1" = scan ] || exit 2
shift
: >"$work/scanned"
for arg in "\$@"; do
    case "\$arg" in
        --lockfile=*) echo "\${arg#--lockfile=}" >>"$work/scanned" ;;
    esac
done
[ -z "\${OSV_FINDS:-}" ]
EOF
chmod +x "$work/bin/uv" "$work/bin/osv-scanner"

fail() { echo "$1" >&2; exit 1; }
audit() { PATH="$work/bin:$PATH" "$shell" "$repo_dir/tools/pipeline/audit.sh" "$@"; }

audit "$work/package"
[ "$(cat "$work/audited")" = "$work/package" ] \
    || fail "an extension's lock was not audited: $(cat "$work/audited")"
[ ! -e "$work/scanned" ] || fail "an extension's audit ran osv-scanner, which its admission has no mise to install"

audit
[ "$(cat "$work/audited")" = "$repo_dir" ] \
    || fail "with no project named, the host's own lock was not audited: $(cat "$work/audited")"
git -C "$repo_dir" ls-files -z -- ':(glob)**/uv.lock' ':(glob)**/package-lock.json' ':(glob)**/go.mod' >"$work/tracked.z"
tr '\0' '\n' <"$work/tracked.z" >"$work/tracked"
for lock in uv.lock tools/lint/package-lock.json controller/go.mod; do
    grep -qx "$lock" "$work/tracked" || fail "$lock is not a tracked lockfile, so this test no longer reaches it"
done
while IFS= read -r lock; do
    grep -qx "$repo_dir/$lock" "$work/scanned" || fail "osv-scanner did not scan $lock"
done <"$work/tracked"

rm "$work/scanned"
if UV_FINDS=1 audit; then fail "the host's audit passed when uv found a vulnerability"; fi
[ -s "$work/scanned" ] || fail "a finding by uv kept osv-scanner from running"
if OSV_FINDS=1 audit; then fail "the host's audit passed when osv-scanner found a vulnerability"; fi
echo "audit tests passed"
