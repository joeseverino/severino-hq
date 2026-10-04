#!/bin/sh
# The root-run tree: the manifest the image ships, the sync that must reproduce
# it, and the daily check that compares the host against it.

set -eu

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "${repo_dir}"
# shellcheck source=scripts/lib/checkout.sh
. ./scripts/lib/checkout.sh
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM
failures=0
fail() { echo "FAIL $1" >&2; failures=$((failures + 1)); }

# An image's /app: the repository's root tree and the manifest the build writes.
image="${work}/image"
make_image() {
    rm -rf "${image}"
    mkdir -p "${image}"
    tar --exclude=node_modules --exclude=__pycache__ \
        -cf "${work}/root-tree.tar" scripts hq/config deploy docker-compose.yml
    tar -xf "${work}/root-tree.tar" -C "${image}"
    # The renderer the image build compiles into the tree; no checkout has it.
    mkdir -p "${image}/deploy/bin"
    printf '#!/bin/sh\necho built by the image\n' >"${image}/deploy/bin/hq-secrets"
    sh scripts/root-tree-manifest.sh "${image}" >"${image}/root-tree.sha256"
}

# 1. The manifest: every shipped file, sorted, hashed; local tooling left out.
make_image
[ ! -d "${image}/scripts/openapi/node_modules" ] ||
    fail "the image fixture copied local Node dependencies"
manifest="$(sh scripts/root-tree-manifest.sh "${image}")"
mkdir -p "${image}/scripts/openapi/node_modules/example/nested"
printf 'local dependency\n' >"${image}/scripts/openapi/node_modules/example/nested/index.js"
[ "$(sh scripts/root-tree-manifest.sh "${image}")" = "${manifest}" ] ||
    fail "the manifest listed local Node dependencies"
mkdir -p "${image}/hq/config/__pycache__"
: >"${image}/hq/config/__pycache__/settings.cpython-312.pyc"
[ "$(sh scripts/root-tree-manifest.sh "${image}")" = "${manifest}" ] ||
    fail "the manifest listed a bytecode cache"
printf '%s\n' "${manifest}" | grep '  scripts/severino-hq-sync-scripts$' >/dev/null ||
    fail "the manifest does not list the sync program"
printf '%s\n' "${manifest}" | grep '  docker-compose.yml$' >/dev/null ||
    fail "the manifest does not list docker-compose.yml"
[ "$(printf '%s\n' "${manifest}" | sed 's/^[0-9a-f]*  //' | LC_ALL=C sort)" = \
    "$(printf '%s\n' "${manifest}" | sed 's/^[0-9a-f]*  //')" ] ||
    fail "the manifest is not sorted"
rm "${image}/docker-compose.yml"
if sh scripts/root-tree-manifest.sh "${image}" >/dev/null 2>&1; then
    fail "a tree without docker-compose.yml produced a manifest"
fi

# 2. The sync, as root runs it, against a fake docker serving the image.
bin="${work}/bin"
mkdir -p "${bin}" "${work}/usr-local-lib"
cp scripts/fixtures/systemctl "${bin}/systemctl"
TEST_ACCOUNT="$(id -un)"
export TEST_ACCOUNT
lib="${work}/usr-local-lib/severino-hq"
cat >"${bin}/docker" <<'EOF'
#!/bin/sh
case "$1" in
    inspect) echo sha256:example-image ;;
    create) echo example-container ;;
    cp) src="${TEST_IMAGE}/${2#example-container:/app/}"
        [ -e "${src}" ] || exit 1
        cp -R "${src}" "$3" ;;
    rm) ;;
esac
EOF
cat >"${bin}/id" <<'EOF'
#!/bin/sh
[ "$1" = -u ] && echo 0
EOF
cat >"${bin}/chown" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod 0755 "${bin}/docker" "${bin}/id" "${bin}/chown"

sync() {
    PATH="${bin}:${PATH}" TEST_IMAGE="${image}" SEVERINO_HQ_LIB_DIR="${lib}" \
        sh scripts/severino-hq-sync-scripts >"${work}/out" 2>"${work}/err"
}
refused() { # refused <why>
    printf 'untouched\n' >"${lib}/.source"
    if sync; then fail "sync accepted $1"; fi
    grep -qx untouched "${lib}/.source" || fail "a refused sync ($1) replaced the tree"
    grep -q 'sync aborted' "${work}/err" || fail "sync refused $1 without saying why"
}

make_image
if ! sync; then
    fail "sync refused the shipped tree: $(cat "${work}/err")"
fi
cmp -s "${lib}/root-tree.sha256" "${image}/root-tree.sha256" || fail "sync did not keep the manifest"
grep -qx 'image sha256:example-image' "${lib}/.source" || fail "sync did not record its source"

# A release that drops a root script and its units is synced: what must exist
# is what the image's manifest and units say, not a list kept here.
make_image
rm -r "${image}/deploy/systemd/severino-hq-backup.service" \
    "${image}/deploy/systemd/severino-hq-backup.service.d" \
    "${image}/deploy/systemd/severino-hq-backup.timer" "${image}/scripts/backup.sh"
sh scripts/root-tree-manifest.sh "${image}" >"${image}/root-tree.sha256"
sync || fail "sync required a file the release no longer ships: $(cat "${work}/err")"

make_image
printf '# changed after the build\n' >>"${image}/scripts/run-controller.sh"
refused "a file that differs from its manifest line"

make_image
: >"${image}/scripts/unlisted.sh"
refused "a file the manifest does not list"

make_image
rm "${image}/scripts/lib/checkout.sh"
refused "a tree missing a file its manifest lists"

# The renderer is in the manifest and the unit names it: an image without it,
# or with another binary in its place, is not synced.
make_image
[ -x "${lib}/deploy/bin/hq-secrets" ] || fail "sync did not leave the renderer executable"
grep -q '  deploy/bin/hq-secrets$' "${image}/root-tree.sha256" ||
    fail "the manifest does not list the renderer"
printf 'replaced after the build\n' >"${image}/deploy/bin/hq-secrets"
refused "a renderer that differs from its manifest line"
make_image
rm -r "${image}/deploy/bin"
sh scripts/root-tree-manifest.sh "${image}" >"${image}/root-tree.sha256"
refused "a unit naming a renderer the tree does not ship"

# From a checkout, which has scripts but no built renderer: the renderer still
# comes from the running image, and a checkout that brings its own is not used.
make_image
checkout="${work}/checkout"
rm -rf "${checkout}"
cp -R "${image}" "${checkout}"
rm "${checkout}/root-tree.sha256"
printf 'planted in the checkout\n' >"${checkout}/deploy/bin/hq-secrets"
sync_checkout() {
    PATH="${bin}:${PATH}" TEST_IMAGE="${image}" SEVERINO_HQ_LIB_DIR="${lib}" \
        SEVERINO_HQ_APP_DIR="${checkout}" \
        sh scripts/severino-hq-sync-scripts --from-checkout >"${work}/out" 2>"${work}/err"
}
sync_checkout || fail "a checkout sync was refused: $(cat "${work}/err")"
cmp -s "${lib}/deploy/bin/hq-secrets" "${image}/deploy/bin/hq-secrets" ||
    fail "a checkout sync took the renderer from the checkout"
rm -r "${image}/deploy/bin"
printf 'untouched\n' >"${lib}/.source"
if sync_checkout; then fail "a checkout sync accepted an image with no renderer"; fi
grep -qx untouched "${lib}/.source" || fail "a refused checkout sync replaced the tree"

make_image
rm "${image}/root-tree.sha256"
refused "an image with no manifest"

make_image
rm "${image}/scripts/run-controller.sh"
sh scripts/root-tree-manifest.sh "${image}" >"${image}/root-tree.sha256"
refused "a unit naming a script the tree does not ship"

# 3. The daily check, relocated, against a host a correct deploy leaves.
make_image
sync || fail "sync refused the shipped tree"
etc="${work}/etc"
sbin="${work}/sbin"
app="${work}/app"
mkdir -p "${etc}" "${sbin}" "${app}/.git/objects"
: >"${app}/.git/HEAD"
cp scripts/severino-hq-sync-scripts "${sbin}/"
(cd deploy/systemd && find . -type f ! -name '*.example') | while IFS= read -r f; do
    mkdir -p "${etc}/$(dirname "${f}")"
    cp "deploy/systemd/${f}" "${etc}/${f}"
done
checker="${work}/severino-hq-check-scripts"
sed -e "s|/usr/local/lib/severino-hq|${lib}|g" \
    -e "s|/etc/systemd/system|${etc}|g" \
    -e "s|/usr/local/sbin|${sbin}|g" \
    -e "s|/opt/apps/severino-hq|${app}|g" \
    scripts/severino-hq-check-scripts >"${checker}"
check() {
    PATH="${bin}:${PATH}" sh "${checker}" >"${work}/out" 2>"${work}/err"
}
drift() { # drift <why> <expected message>
    if check; then fail "the check passed $1"; fi
    grep -qF "$2" "${work}/err" || fail "$1 was not named: $(cat "${work}/err")"
}

check || fail "a current host reported drift: $(cat "${work}/err")"

printf '# edited on the host\n' >>"${lib}/scripts/run-controller.sh"
drift "an edited root script" "no longer reproduces root-tree.sha256"
sync

: >"${lib}/scripts/added-on-the-host.sh"
drift "a file added to the root tree" "no longer reproduces root-tree.sha256"
sync

printf '# a hand-installed copy\n' >>"${sbin}/severino-hq-sync-scripts"
drift "a stale sync program" "${sbin}/severino-hq-sync-scripts does not match"
cp scripts/severino-hq-sync-scripts "${sbin}/"

# Ownership, against the account the Actions runner runs as: every file under
# .git is foreign to another account.
TEST_ACCOUNT=nobody
drift "a .git owned by another account than the runner's" "not owned by nobody"
grep -qF "$(checkout_ownership_fix "${app}" nobody)" "${work}/err" ||
    fail "the ownership drift did not name the fix"
TEST_ACCOUNT=root
drift "a runner that runs as root" "The Actions runner runs as 'root'"
TEST_ACCOUNT=no-such-account-example
drift "an account find cannot resolve" "Could not check ownership"
TEST_ACCOUNT="$(id -un)"
TEST_RUNNER_UNITS=""
export TEST_RUNNER_UNITS
drift "no Actions runner unit" "Expected one Actions runner unit, found: none"
TEST_RUNNER_UNITS="actions.runner.example-a.host.service actions.runner.example-b.host.service"
drift "two Actions runner units" "Expected one Actions runner unit"
unset TEST_RUNNER_UNITS
rm -r "${app}/.git"
drift "a checkout with no .git" "No git checkout at ${app}"

if [ "${failures}" -ne 0 ]; then
    echo "Root tree contracts failed (${failures})." >&2
    exit 1
fi
echo "Root tree contracts hold."
