#!/bin/sh
# Prove the audit exports the named project's lock (the host's by default) and
# audits that export with the host's own pip-audit.
set -eu

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/package"

# uv: an export writes the project it was given into the file it names; a run
# records its project and the file pip-audit reads.
cat >"$work/bin/uv" <<EOF
#!/bin/sh
command=\$1; shift
project=""; output=""; requirements=""
while [ "\$#" -gt 0 ]; do
    case "\$1" in
        --project) project=\$2; shift 2 ;;
        --output-file) output=\$2; shift 2 ;;
        -r) requirements=\$2; shift 2 ;;
        *) shift ;;
    esac
done
case "\$command" in
    export) echo "\$project" >"\$output" ;;
    run) echo "\$project \$(cat "\$requirements")" >"$work/audited" ;;
esac
EOF
chmod +x "$work/bin/uv"

fail() { echo "$1" >&2; exit 1; }

PATH="$work/bin:$PATH" "$repo_dir/tools/pipeline/audit.sh" "$work/package"
[ "$(cat "$work/audited")" = "$repo_dir $work/package" ] \
    || fail "an extension's lock was not audited by the host's pip-audit: $(cat "$work/audited")"

PATH="$work/bin:$PATH" "$repo_dir/tools/pipeline/audit.sh"
[ "$(cat "$work/audited")" = "$repo_dir $repo_dir" ] \
    || fail "with no project named, the host's own lock was not audited: $(cat "$work/audited")"
echo "audit tests passed"
