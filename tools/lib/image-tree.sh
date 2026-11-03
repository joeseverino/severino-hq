# shellcheck shell=sh
# The checkout's copy of what the image's /app is built from: the paths
# Dockerfile.dockerignore admits, tracked or new, less what the image's copy of
# the checkout excludes from /app as another stage's input. The composed suite
# runs in it, and image:verify:tree holds it to the built image's /app, so a
# form either read below misses is a difference that check names.
#   image_tree DEST
# shellcheck source=tools/lib/export-tree.sh
. ./tools/lib/export-tree.sh

image_tree() {
    admitted="$(sed -n 's|^!\([^*]*[^/]\)/*$|\1|p' Dockerfile.dockerignore)"
    [ -n "${admitted}" ] || { echo "Dockerfile.dockerignore admits nothing." >&2; return 1; }
    # shellcheck disable=SC2086  # one pathspec per admitted path
    export_tree "$1" ${admitted}
    excluded="$(sed -nE 's|^COPY ((--exclude=[^ ]+ )+)\. /app$|\1|p' Dockerfile | tr ' ' '\n' | sed -n 's|^--exclude=||p')"
    for path in ${excluded}; do
        rm -rf "${1:?}/${path}"
    done
}
