#!/usr/bin/env bash
# The extensions as a pull request meets them, and the wheels of those on a
# coordinated branch.
#   tools/pipeline/extension-branches.sh checkout SET COORDINATED
#   tools/pipeline/extension-branches.sh build SET COORDINATED INSTALLER
# SET is the composition set; COORDINATED is coordinated-branches.sh's
# repository=sha list.
#
# checkout clones each extension repository into extensions/<name> at its
# coordinated commit, else its default branch: what each is checked and
# admitted with once the pull request merges. Without .git, so no copy carries
# the reader token (GH_TOKEN).
#
# build turns each extension on a coordinated branch into a wheel in
# candidate-wheels/. Branch code is unreviewed and the job holds a registry
# credential and an OIDC identity, so it builds in INSTALLER, which holds no
# token, credential file or docker socket, from that copy with no .git.
#
# This runs in a public log: what a clone or a build prints goes to
# $WITHHELD_DIR, which is sealed.
set -euo pipefail

usage() { echo "usage: $0 checkout SET COORDINATED | build SET COORDINATED INSTALLER" >&2; exit 2; }
withheld="${WITHHELD_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "$withheld"

checkout() {
  local set_file=$1 coordinated=$2 repository dir sha
  mkdir -p extensions
  while read -r repository; do
    dir="extensions/$(basename "$repository")"
    if ! gh repo clone "$repository" "$dir" -- --quiet 2>"$withheld/clone-$(basename "$repository").log"; then
      echo "::error title=Extension not checked out::An extension repository could not be cloned; the reason is in this run's sealed failure logs."
      exit 1
    fi
    sha=$(awk -F= -v r="$repository" '$1 == r { print $2 }' "$coordinated")
    if [ -n "$sha" ]; then
      git -C "$dir" checkout --quiet --detach "$sha"
    fi
    rm -rf "$dir/.git"
    chmod -R a+rwX "$dir"
  done < <(jq -r '.extensions[].repository' "$set_file" | sort -u)
}

build() {
  local set_file=$1 coordinated=$2 installer=$3 repository dir id package declared
  mkdir -p candidate-wheels
  chmod 0777 candidate-wheels
  while IFS='=' read -r repository _; do
    [ -n "$repository" ] || continue
    dir="extensions/$(basename "$repository")"
    # A repository may hold several extensions: each one the set lists is
    # built from its path, which must declare that id, and nothing else in the
    # repository is. Read without running the branch's code.
    while IFS=$'\t' read -r id package; do
      if ! declared=$(python3 -m tools.pipeline.plugin_identity "$dir/$package" \
           2>"$withheld/candidate-identity-$id.log" | sed -n 's/^plugin-id=//p') ||
         [ "$declared" != "$id" ]; then
        echo "::error title=Coordinated branch did not build::A coordinated branch does not carry an extension the composition lists at its path."
        exit 1
      fi
      # The build lists every module and template it copies: withheld.
      if ! docker run --rm --network bridge --user 10001:10001 \
        --env HOME=/tmp --env UV_PYTHON_DOWNLOADS=never \
        --volume "$PWD/$dir:/src" --volume "$PWD/candidate-wheels:/out" \
        --entrypoint uv "$installer" \
        build --python /usr/local/bin/python --no-cache --wheel --out-dir /out "/src/$package" \
        >"$withheld/candidate-build-$id.log" 2>&1; then
        echo "::error title=Coordinated branch did not build::A coordinated branch did not build; its output is in this run's sealed failure logs."
        exit 1
      fi
    done < <(jq -r --arg repo "$repository" \
               '.extensions[] | select(.repository == $repo) | [.id, (.path // ".")] | @tsv' "$set_file")
  done <"$coordinated"
}

case "${1:-}" in
  checkout) [ "$#" -eq 3 ] || usage; checkout "$2" "$3" ;;
  build) [ "$#" -eq 4 ] || usage; build "$2" "$3" "$4" ;;
  *) usage ;;
esac
