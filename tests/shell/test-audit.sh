#!/bin/sh
# Prove the audit runs uv's on the named project (the host's by default).
set -eu

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/package"

# uv: an audit records the project it was given.
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
[ "\$command" = audit ] && echo "\$project" >"$work/audited"
EOF
chmod +x "$work/bin/uv"

fail() { echo "$1" >&2; exit 1; }

PATH="$work/bin:$PATH" "$repo_dir/tools/pipeline/audit.sh" "$work/package"
[ "$(cat "$work/audited")" = "$work/package" ] \
    || fail "an extension's lock was not audited: $(cat "$work/audited")"

PATH="$work/bin:$PATH" "$repo_dir/tools/pipeline/audit.sh"
[ "$(cat "$work/audited")" = "$repo_dir" ] \
    || fail "with no project named, the host's own lock was not audited: $(cat "$work/audited")"
echo "audit tests passed"
