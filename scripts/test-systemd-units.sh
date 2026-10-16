#!/bin/sh
# The systemd files the repository ships: the set the preflight expects, the
# drop-ins that would override a unit's command, and failure reporting.

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
# shellcheck source=scripts/lib/systemd-units.sh
. ./scripts/lib/systemd-units.sh
fixture="$(mktemp -d)"
trap 'rm -rf "${fixture}"' EXIT HUP INT TERM
failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

# 1. The walk yields what systemd would read, and nothing else.
#
# A template is a file a person copies into place after editing it; installing
# it verbatim would put example values into production. Stray files are not
# configuration either, and a checkout on a Mac grows them.
walk="${fixture}/walk"
mkdir -p "${walk}/a.service.d" "${walk}/nested/deeper"
for f in a.service a.timer b.path a.service.d/10-x.conf \
    a.service.d/20-y.conf.example connect.conf.example README .DS_Store \
    nested/deeper/c.service; do
    : >"${walk}/${f}"
done
expected="a.service
a.service.d/10-x.conf
a.timer
b.path"
[ "$(units_shipped "${walk}")" = "${expected}" ] ||
    fail "units_shipped returned: $(units_shipped "${walk}" | tr '\n' ' ')"
if units_shipped "${fixture}/absent" >/dev/null 2>&1; then
    fail "units_shipped accepted a directory that does not exist"
fi

# 2. A shipped drop-in that replaces ExecStart must replace it with what the
#    unit itself runs.
#
# The drop-in wins, so a change to the unit's ExecStart that the drop-in does
# not repeat is a change that never takes effect, so it is refused here.
exec_start() { sed -n 's/^ExecStart=\(..*\)/\1/p' "$1" | tail -1; }
for f in $(units_shipped deploy/systemd); do
    case "${f}" in *.d/*) ;; *) continue ;; esac
    replaced="$(exec_start "deploy/systemd/${f}")"
    [ -n "${replaced}" ] || continue
    unit="${f%%.d/*}"
    if [ "${replaced}" != "$(exec_start "deploy/systemd/${unit}")" ]; then
        fail "${f} runs '${replaced}', but ${unit} runs '$(exec_start "deploy/systemd/${unit}")'"
    fi
done

# 3. Every shipped unit that can fail tells HQ when it does, and nothing that
#    telling starts can start itself again.
#
# The drop-in is in the directory systemd reads for every unit name with this
# repository's prefix, so the test is that every shipped unit has that prefix
# and that nothing replaces the drop-in except where a loop would follow: the
# unit the failure starts, and each unit a shipped path starts, since the read
# it asks for rings the doorbell a path watches.
shipped="$(units_shipped deploy/systemd)"
on_failure="10-on-failure.conf"
handler() { sed -n 's/^OnFailure=//p' "deploy/systemd/$1" | tail -1; }
started="$(handler "severino-hq-.service.d/${on_failure}")"
case "${started}" in
    severino-hq-*@*.service) ;;
    *) fail "severino-hq-.service.d/${on_failure} starts '${started}' on failure" ;;
esac
printf '%s\n' "${shipped}" | grep -Fxq -- "${started%%@*}@.service" ||
    fail "the unit a failure starts, ${started}, is not an instance of a shipped template"
must_not_have="${started}"
for f in ${shipped}; do
    case "${f}" in
        */*) ;;
        *.path) must_not_have="${must_not_have} $(sed -n 's/^Unit=//p' "deploy/systemd/${f}")" ;;
    esac
done
for f in ${shipped}; do
    case "${f}" in */*) continue ;; esac
    type="${f##*.}"
    # A mount's name is its path, so no prefix drop-in can reach it. It fails
    # through the unit that requires it: that unit cannot start without it.
    if [ "${type}" = mount ]; then
        where="$(sed -n 's/^Where=//p' "deploy/systemd/${f}")"
        grep -lx "RequiresMountsFor=${where}" deploy/systemd/severino-hq-*.service >/dev/null ||
            fail "${f}: nothing that reports its failure requires ${where}"
        continue
    fi
    case "${type}" in service | timer | path) ;; *) fail "${f}: no failure drop-in covers a ${type}"; continue ;; esac
    case "${f}" in severino-hq-*) ;; *) fail "${f} is outside the prefix the failure drop-in covers"; continue ;; esac
    [ "$(handler "severino-hq-.${type}.d/${on_failure}")" = "${started}" ] ||
        fail "severino-hq-.${type}.d/${on_failure} does not start ${started}"
done
# What replaces the drop-in, by systemd's rule that a file of the same name
# further down the name wins. Each must start nothing, and be one of the units
# above; and each of those units must have one.
replaced=""
for f in ${shipped}; do
    case "${f}" in
        severino-hq-.*.d/*) continue ;;
        *.d/"${on_failure}") ;;
        *) continue ;;
    esac
    unit="${f%%.d/*}"
    replaced="${replaced} ${unit}"
    [ -z "$(handler "${f}")" ] || fail "${f} starts a unit on failure"
    case " ${must_not_have} " in
        *" ${unit} "*) ;;
        *) fail "${unit} is exempt from telling HQ it failed, and no loop requires that" ;;
    esac
done
for unit in ${must_not_have}; do
    case " ${replaced} " in
        *" ${unit} "*) ;;
        *) fail "${unit} would start again whenever it failed: it needs an empty ${unit}.d/${on_failure}" ;;
    esac
done
# No unit sets OnFailure of its own beside the shared drop-in: a second handler
# would be a second path to the doorbell that this test does not follow.
for f in ${shipped}; do
    case "${f}" in severino-hq-.*.d/"${on_failure}") continue ;; esac
    if grep -q '^OnFailure=..*' "deploy/systemd/${f}"; then
        fail "${f} sets OnFailure outside the shared drop-in"
    fi
done

if [ "${failures}" -ne 0 ]; then
    echo "Systemd unit contracts failed (${failures})." >&2
    exit 1
fi
echo "Systemd unit contracts hold."
