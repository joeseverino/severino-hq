#!/bin/sh
# The systemd files the repository ships, and the check that the host still has
# them. These cases hold the set to one derivation and the check to byte
# equality, so neither half can narrow.

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

# What an earlier release left: this repository's prefix, a unit suffix, top
# level, and not shipped. Another owner's unit and a host's drop-in are not.
left="${fixture}/left"
mkdir -p "${left}/a.service.d" "${left}/severino-hq-old.service.d"
for f in a.service a.timer other.service severino-hq-old.service severino-hq-old.path \
    severino-hq-notes.txt a.service.d/10-x.conf severino-hq-old.service.d/10-host.conf; do
    : >"${left}/${f}"
done
[ "$(units_retired "${walk}" "${left}" | tr '\n' ' ')" = "severino-hq-old.path severino-hq-old.service " ] ||
    fail "units_retired returned: $(units_retired "${walk}" "${left}" | tr '\n' ' ')"

# 2. The drift check, run as the host runs it, against a relocated host.
lib="${fixture}/lib"
etc="${fixture}/etc"
bin="${fixture}/bin"
sbin="${fixture}/sbin"
app="${fixture}/app"
mkdir -p "${lib}" "${etc}" "${bin}" "${sbin}" "${app}/.git"
tar --exclude=node_modules --exclude=__pycache__ \
    -cf "${fixture}/root-tree.tar" scripts hq/config deploy docker-compose.yml
tar -xf "${fixture}/root-tree.tar" -C "${lib}"
sh scripts/root-tree-manifest.sh "${lib}" >"${lib}/root-tree.sha256"
cp scripts/severino-hq-sync-scripts "${sbin}/"
# Relocate fixed host paths in the test copy, not in the production interface.
checker="${fixture}/severino-hq-check-scripts"
sed -e "s|/usr/local/lib/severino-hq|${lib}|g" \
    -e "s|/etc/systemd/system|${etc}|g" \
    -e "s|/usr/local/sbin|${sbin}|g" \
    -e "s|/opt/apps/severino-hq|${app}|g" \
    scripts/severino-hq-check-scripts >"${checker}"
printf 'image sha256:test\n' >"${lib}/.source"
printf '#!/bin/sh\necho sha256:test\n' >"${bin}/docker"
chmod 0755 "${bin}/docker"
cp scripts/fixtures/systemctl "${bin}/systemctl"
TEST_ACCOUNT="$(id -un)"
export TEST_ACCOUNT

check() {
    PATH="${bin}:${PATH}" sh "${checker}" \
        >"${fixture}/out" 2>"${fixture}/err"
}

# A host as a correct deploy leaves it, plus the files it owns by design:
# templates were never installed, and a drop-in for this host's naming or its
# credential mount lives beside the shipped ones without being drift.
(cd deploy/systemd && find . -type f ! -name '*.example') | while IFS= read -r f; do
    mkdir -p "${etc}/$(dirname "${f}")"
    cp "deploy/systemd/${f}" "${etc}/${f}"
done
for extra in 10-estate.conf 20-connect.conf 20-credential-mount.conf; do
    printf '[Service]\n' >"${etc}/severino-hq-secrets.service.d/${extra}"
done
if ! check; then
    fail "a current host reported drift: $(cat "${fixture}/err")"
fi

# A changed unit, a changed drop-in, and a unit that never arrived are each
# named: by path, since "something differs" sends a person to diff eleven files.
printf '# edited on the host\n' >>"${etc}/severino-hq-audit-prune.timer"
printf '# edited on the host\n' >>"${etc}/severino-hq-backup.service.d/10-root-owned-exec.conf"
rm "${etc}/severino-hq-script-drift.timer"
if check; then
    fail "a drifted host passed the check"
fi
for drifted in severino-hq-audit-prune.timer \
    severino-hq-backup.service.d/10-root-owned-exec.conf \
    severino-hq-script-drift.timer; do
    grep -qF "${etc}/${drifted}" "${fixture}/err" || fail "drift in ${drifted} was not reported"
done
# A unit this release dropped is named too: it would keep running.
: >"${etc}/severino-hq-retired.service"
if check; then
    fail "a unit no release ships passed the check"
fi
grep -qF "${etc}/severino-hq-retired.service is not shipped" "${fixture}/err" ||
    fail "a retired unit was not reported"
for ignored in 10-estate.conf 20-connect.conf 20-credential-mount.conf .example; do
    if grep -qF "${ignored}" "${fixture}/err"; then
        fail "${ignored} was reported as drift"
    fi
done

# 3. A shipped drop-in that replaces ExecStart must replace it with what the
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

# 4. What systemd is asked about: every shipped unit that has a state of its
#    own, and each instance of a shipped template that a shipped file starts.
#
# A template has no state, an instance of a template this repository does not
# ship is another owner's unit, and a name that is not a unit name never
# reaches systemctl's arguments.
asked="${fixture}/asked"
mkdir -p "${asked}/a-.service.d"
: >"${asked}/a.service"
: >"${asked}/job@.service"
printf '[Timer]\nUnit=job@one.service\n' >"${asked}/a.timer"
printf '[Timer]\nUnit=other@one.service\n' >"${asked}/b.timer"
printf '[Path]\nUnit=a.service\n' >"${asked}/a.path"
# shellcheck disable=SC2016  # the substitution is the text under test
printf '[Unit]\nOnFailure=job@two.service  job@.service job@$(id).service a.service\n' \
    >"${asked}/a-.service.d/10-failed.conf"
expected="a.path
a.service
a.timer
b.timer
job@one.service
job@two.service"
[ "$(units_reported "${asked}")" = "${expected}" ] ||
    fail "units_reported returned: $(units_reported "${asked}" | tr '\n' ' ')"
if units_reported "${fixture}/absent" >/dev/null 2>&1; then
    fail "units_reported accepted a directory that does not exist"
fi

# The state is one block per reported unit, in systemd's own words. A unit
# that is not installed is a block too, and no answer at all is a failure,
# never an empty reading.
state="${fixture}/state"
mkdir -p "${state}"
printf 'LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\n' >"${state}/a.service"
answer="$(PATH="${bin}:${PATH}" TEST_UNIT_STATE="${state}" \
    TEST_SYSTEMCTL_LOG="${fixture}/systemctl-log" units_state "${asked}")" ||
    fail "units_state failed against a systemd that answers"
[ "$(printf '%s\n' "${answer}" | grep -c '^Id=')" -eq 6 ] ||
    fail "units_state answered for $(printf '%s\n' "${answer}" | grep -c '^Id=') units, not 6"
printf '%s\n' "${answer}" | grep -A2 '^Id=a.service$' | grep -qx 'ActiveState=failed' ||
    fail "units_state did not carry a unit's state"
printf '%s\n' "${answer}" | grep -A1 '^Id=job@two.service$' | grep -qx 'LoadState=not-found' ||
    fail "units_state did not answer for a unit that is not installed"
grep -qx 'TZ=UTC' "${fixture}/systemctl-log" || fail "units_state did not ask in UTC"
grep -qx -- "--property=${units_properties}" "${fixture}/systemctl-log" ||
    fail "units_state asked for other than the declared properties"
if PATH="${bin}:${PATH}" units_state "${asked}" >/dev/null 2>&1; then
    fail "units_state succeeded against a systemd that did not answer"
fi
# No property asked for is one that carries what a unit runs or is given.
for property in $(printf '%s' "${units_properties}" | tr ',' ' '); do
    case "${property}" in
        ExecMainCode | ExecMainStatus | UnitFileState) ;;
        *Exec* | *Environment* | *Credential* | *Path* | *Director* | *File* | *Passphrase*)
            fail "units_properties asks for ${property}" ;;
    esac
done

# 5. Every shipped unit that can fail tells HQ when it does, and nothing that
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
