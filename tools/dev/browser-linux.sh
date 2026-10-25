#!/bin/sh
# Inside the Playwright image, on the bind-mounted checkout: sets up as
# .github/actions/toolchain does (mise at the release mise.toml's min_version
# names, then `mise install`) and runs a browser gate or `mise run test`.
#   sh tools/dev/browser-linux.sh browser:<gate>
#   sh tools/dev/browser-linux.sh <label>... [manage.py test options]
# Run as linux/amd64 by mise-tasks/dev/linux-browser, which mounts a volume at
# HOME so the setup is downloaded once. A failed rule's screenshots are copied
# to var/browser-linux/, since the container's temporary directory goes with it.
set -eu

[ "$#" -gt 0 ] || { echo "usage: tools/dev/browser-linux.sh browser:<gate> | <label>..." >&2; exit 64; }

checkout="${PWD}"
version="$(sh .github/actions/toolchain/mise-version.sh mise.toml)"
bin="${HOME}/.local/bin"
installed="$("${bin}/mise" --version 2>/dev/null || true)"
if [ "${installed%% *}" != "${version}" ]; then
    asset="mise-v${version}-linux-x64"
    release="https://github.com/jdx/mise/releases/download/v${version}"
    download="$(mktemp -d)"
    curl -fsSL -o "${download}/${asset}" "${release}/${asset}"
    curl -fsSL -o "${download}/SHASUMS256.txt" "${release}/SHASUMS256.txt"
    (cd "${download}" && grep " \./${asset}\$\| ${asset}\$" SHASUMS256.txt | sha256sum -c -)
    mkdir -p "${bin}"
    install -m 0755 "${download}/${asset}" "${bin}/mise"
    rm -rf "${download}"
fi

# The runner's environment: CI set, the checkout's config trusted, and the
# machine's own mise.local.toml left out as a runner has none. The project's
# environment lives in the volume, since the checkout's .venv is the Mac's.
PATH="${bin}:${PATH}"
export PATH CI=true
export MISE_TRUSTED_CONFIG_PATHS="${checkout}"
export MISE_IGNORED_CONFIG_PATHS="${checkout}/mise.local.toml"
export UV_PROJECT_ENVIRONMENT="${HOME}/venv"
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'

mise install

status=0
case "$1" in
    browser:*) mise run "$@" || status=$? ;;
    *) mise run test "$@" || status=$? ;;
esac

for failure in "${TMPDIR:-/tmp}"/hq-browser-failure-*; do
    [ -d "${failure}" ] || continue
    mkdir -p var/browser-linux
    cp -R "${failure}" var/browser-linux/
    echo "Saved ${failure##*/} in var/browser-linux/."
done
exit "${status}"
