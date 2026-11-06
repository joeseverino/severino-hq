#!/bin/sh
# A task that needs Docker starts colima's VM when no daemon answers, as a
# dependency or from its body, and stops it when it ends, passed or failed; a
# task run inside another keeps the VM up between them; a VM up before the
# run, one another run still holds, or one a container runs in stays up, and
# the last holder to end stops it; with no colima, a daemon that answers is
# used as it is and one that does not is named. Every task that reaches the
# daemon declares the stop.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
shell="${SHELL_UNDER_TEST:-/bin/sh}"
work="$(mktemp -d)"
cleanup() {
    rm -rf "${work}"
}
trap cleanup EXIT

fail() {
    echo "test-docker-daemon: $*" >&2
    exit 1
}

# Fakes for colima and docker: the VM is up while ${work}/up exists, and
# docker ps lists what ${work}/containers holds. colima writes each command
# it is given to ${work}/colima.log.
mkdir -p "${work}/bin" "${work}/plain" "${work}/project" "${work}/colima/_lima/colima"
cat >"${work}/bin/colima" <<SH
#!/bin/sh
echo "\$*" >>"${work}/colima.log"
echo "\${MISE_TASK_LOCKS:-}" >>"${work}/colima.locks"
perl -e 'for my \$lock (glob shift) { my @l = stat \$lock or next; for (3 .. 63) { open my \$f, "<&=", \$_ or next; my @s = stat \$f; print "fd \$_\n" if \$s[0] == \$l[0] && \$s[1] == \$l[1] } }' \
    "${work}/mise-state/task-locks/*" >>"${work}/leaks"
case "\$1" in
    start) : >"${work}/up"; echo "\$\$" >"${work}/colima/_lima/colima/ha.pid" ;;
    stop) rm -f "${work}/up" "${work}/colima/_lima/colima/ha.pid" ;;
esac
SH
cat >"${work}/bin/docker" <<SH
#!/bin/sh
[ -e "${work}/up" ] || exit 1
case "\$1" in
    ps) cat "${work}/containers" 2>/dev/null || true ;;
esac
SH
cp "${work}/bin/docker" "${work}/plain/docker"
chmod +x "${work}/bin/colima" "${work}/bin/docker" "${work}/plain/docker"
mise_bin="$(command -v mise)"
ln -s "${mise_bin}" "${work}/bin/mise"
ln -s "${mise_bin}" "${work}/plain/mise"

task="${repo}/mise-tasks/image/docker"
# The task's own lock, as its header declares it, since the shell under test
# runs the file here and mise reads no header of a file it does not run.
lock="$(sed -n 's/^#MISE lock=//p' "${task}")"
[ -n "${lock}" ] || fail "image:docker declares no lock to take turns under"
lock_name="$(echo "${lock}" | tr -d '"')"
cat >"${work}/project/mise.toml" <<TOML
[tasks."image:docker"]
usage = 'arg "<action>"'
lock = ${lock}
run = '${shell} ${task}'

[tasks.work]
depends = ["image:docker start"]
depends_post = ["image:docker stop"]
run = 'docker info; [ ! -e leaves ] || echo 0123456789ab >"${work}/containers"; [ ! -e failing ]'

[tasks.outer]
depends = ["image:docker start"]
depends_post = ["image:docker stop"]
run = '${mise_bin} run work && test -e "${work}/up" && ${mise_bin} run work'

[tasks.body]
depends_post = ["image:docker stop"]
run = '${mise_bin} run image:docker start && docker info && ${mise_bin} run work && test -e "${work}/up"'

[tasks.heavy]
lock = "heavy"
depends_post = ["image:docker stop"]
run = '${mise_bin} run image:docker start && docker info'

[tasks.hold]
usage = 'arg "<name>"'
depends = ["image:docker start"]
depends_post = ["image:docker stop"]
run = 'docker info; echo up >"${work}/\$usage_name.ready"; read -r _ <"${work}/\$usage_name.go"; docker info'
TOML

XDG_CACHE_HOME="${work}/cache"
COLIMA_HOME="${work}/colima"
export XDG_CACHE_HOME COLIMA_HOME
# Listed while the repository's config is still trusted: the project's trust
# below keeps its state apart from the Mac's, where that trust is recorded.
mise -C "${repo}" tasks ls --hidden --json >"${work}/tasks.json"
# shellcheck source=tests/shell/mise-state.sh
. "${repo}/tests/shell/mise-state.sh"
mise_trust "${work}/project" "${work}/mise-state"
fake_path="${work}/bin:/usr/bin:/bin:/usr/sbin:/sbin"

run() {
    PATH="${fake_path}" mise -C "${work}/project" run -q "$@" >"${work}/out" 2>&1
}

starts() { grep -c '^start' "${work}/colima.log" 2>/dev/null || true; }
stops() { grep -c '^stop' "${work}/colima.log" 2>/dev/null || true; }

fresh() {
    rm -rf "${work}/up" "${work}/colima.log" "${work}/colima.locks" "${work}/containers" "${work}/cache" "${work}/project/failing"
}

# From a stopped VM to a stopped one, once each, with Rosetta on.
fresh
run work || fail "a task with no daemon running failed: $(cat "${work}/out")"
[ ! -e "${work}/up" ] || fail "the VM a task started is still up after it"
[ "$(starts) $(stops)" = "1 1" ] || fail "a task started the VM $(starts) times and stopped it $(stops), not once each"
grep -q -- '--vz-rosetta' "${work}/colima.log" || fail "the VM starts without Rosetta, which browser --linux needs"
# colima runs under the lock, and what it leaves running would hold the lock,
# so every later start and stop would wait on it for good.
grep -qv "${lock_name}" "${work}/colima.locks" && fail "colima ran outside the lock: $(cat "${work}/colima.locks")"
[ ! -s "${work}/leaks" ] || fail "colima runs holding the lock: $(cat "${work}/leaks")"

# Run from the body of a task holding the heavy turn, it takes its lock after
# the one it inherits.
fresh
run heavy || fail "a task holding the heavy turn could not start the VM from its body: $(cat "${work}/out")"
[ "$(starts) $(stops)" = "1 1" ] || fail "a heavy task started the VM $(starts) times and stopped it $(stops), not once each"

# Failed, it stops the VM all the same.
fresh
: >"${work}/project/failing"
if run work; then fail "a failing task passed"; fi
[ ! -e "${work}/up" ] || fail "the VM a failed task started is still up after it"

# Tasks run inside a task share its VM: one start, one stop, after both.
fresh
run outer || fail "a task running two others failed: $(cat "${work}/out")"
[ "$(starts) $(stops)" = "1 1" ] || fail "a task running two others started the VM $(starts) times and stopped it $(stops), not once each"
[ ! -e "${work}/up" ] || fail "the VM is still up after the outermost task"

# Started from a task's body, the VM is the task's, kept for a task it runs.
fresh
run body || fail "a task starting the VM from its body failed: $(cat "${work}/out")"
[ "$(starts) $(stops)" = "1 1" ] || fail "a task starting the VM from its body started it $(starts) times and stopped it $(stops), not once each"
[ ! -e "${work}/up" ] || fail "the VM a task's body started is still up after it"

# A VM up before the run is left up.
fresh
PATH="${fake_path}" colima start
run work || fail "a task with a daemon already running failed: $(cat "${work}/out")"
[ -e "${work}/up" ] && [ "$(stops)" = 0 ] || fail "a task stopped a VM it did not start"

# A VM a container runs in stays up.
fresh
: >"${work}/project/leaves"
run work || fail "a task that leaves a container running failed: $(cat "${work}/out")"
[ -e "${work}/up" ] || fail "a task stopped the VM a container still runs in"
rm -f "${work}/project/leaves"

# Run A starts the VM, run B joins it, and A ends first: the VM stays up for
# B, and goes when B ends. Each run says when it holds the VM and waits to be
# let go, through fifos.
fresh
for name in a b; do
    rm -f "${work}/${name}.ready" "${work}/${name}.go"
    mkfifo "${work}/${name}.ready" "${work}/${name}.go"
done
PATH="${fake_path}" "${mise_bin}" -C "${work}/project" run -q hold a >"${work}/a.out" 2>&1 &
first=$!
read -r _ <"${work}/a.ready"
PATH="${fake_path}" "${mise_bin}" -C "${work}/project" run -q hold b >"${work}/b.out" 2>&1 &
second=$!
read -r _ <"${work}/b.ready"
echo go >"${work}/a.go"
wait "${first}" || fail "run A failed: $(cat "${work}/a.out")"
[ -e "${work}/up" ] || fail "run A, which started the VM, stopped it under run B"
echo go >"${work}/b.go"
wait "${second}" || fail "run B, which joined the VM, failed once run A ended: $(cat "${work}/b.out")"
[ ! -e "${work}/up" ] || fail "the last run to hold the VM left it up"
[ "$(starts) $(stops)" = "1 1" ] || fail "two runs sharing the VM started it $(starts) times and stopped it $(stops), not once each"

# With no colima (CI), a daemon that answers is used and nothing is written;
# one that does not is named.
fresh
: >"${work}/up"
PATH="${work}/plain:/usr/bin:/bin:/usr/sbin:/sbin" "${mise_bin}" -C "${work}/project" run -q work >"${work}/out" 2>&1 \
    || fail "with no colima, a task failed where Docker answers: $(cat "${work}/out")"
[ ! -e "${work}/cache/severino" ] || fail "with no colima, a task wrote state where Docker answers"
rm -f "${work}/up"
status=0
PATH="${work}/plain:/usr/bin:/bin:/usr/sbin:/sbin" "${mise_bin}" -C "${work}/project" run -q work >"${work}/out" 2>&1 || status=$?
[ "${status}" -ne 0 ] || fail "with no colima and no daemon, a task passed"
grep -q 'No Docker daemon answers' "${work}/out" || fail "with no colima and no daemon, the failure does not say so: $(cat "${work}/out")"

# Every task that reaches the daemon, by a dependency or by running one from
# its body, stops the VM after, which it may otherwise leave running.
# A dependency is a string, a list of words or a table with a task and its args.
jq -r 'def words: if type == "object" then [.task] + (.args // []) elif type == "array" then . else split(" ") end;
    .[] | select(.name != "image:docker") | [
        .name,
        (.file // ""),
        (.depends | map(words[0]) | any(. == "image:docker" or . == "image:build")),
        (.run | tostring | test("image:(build|docker start)")),
        (.depends_post | map(words | join(" ")) | any(. == "image:docker stop"))
    ] | map(tostring) | join("|")' "${work}/tasks.json" >"${work}/tasks"
[ -s "${work}/tasks" ] || fail "mise tasks ls listed no task"
missing=""
while IFS="|" read -r name file by_depends by_run stops; do
    reaches="${by_depends}${by_run}"
    if [ -n "${file}" ] && grep -Eq 'mise[[:space:]]+run image:(build|docker start)' "${file}"; then
        reaches=true
    fi
    case "${reaches}:${stops}" in
        *true*:false) missing="${missing} ${name}" ;;
    esac
done <"${work}/tasks"
[ -z "${missing}" ] || fail "tasks that reach the Docker daemon and never stop the VM they may start:${missing}"
