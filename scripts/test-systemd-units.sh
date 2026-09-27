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

# 2. The drift check, run as the host runs it, against a relocated host.
lib="${fixture}/lib"
etc="${fixture}/etc"
bin="${fixture}/bin"
sbin="${fixture}/sbin"
app="${fixture}/app"
mkdir -p "${lib}" "${etc}" "${bin}" "${sbin}" "${app}/.git"
cp -R scripts config deploy docker-compose.yml "${lib}/"
find "${lib}" -name __pycache__ -prune -exec rm -rf {} +
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

if [ "${failures}" -ne 0 ]; then
    echo "Systemd unit contracts failed (${failures})." >&2
    exit 1
fi
echo "Systemd unit contracts hold."
