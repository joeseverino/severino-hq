#!/bin/sh
# The composition's static assets: collected again with the extensions
# installed, then written over the host image's tree only where a file differs.
# The host image already holds the host's assets, so a layer above it that
# rewrote them all would ship a second copy of every one; this one holds only
# what the extensions add or change, and nothing when they bring no assets.
# Collecting signs nothing, so the settings' signing key exists for it alone.
# Run as root from /app, as the image's own build steps run.

set -eu

root="${DJANGO_STATIC_ROOT:?}"
fresh="$(mktemp -d)"
listing="$(mktemp)"
trap 'rm -rf "${fresh}" "${listing}"' EXIT

DJANGO_STATIC_ROOT="${fresh}" DJANGO_SECRET_KEY="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')" \
    python manage.py collectstatic --noinput --verbosity 0

# Asset names hold no newline: collectstatic's manifest is JSON keyed by them.
(cd "${fresh}" && find . -type f) >"${listing}"
while IFS= read -r path; do
    cmp -s "${fresh}/${path}" "${root}/${path}" && continue
    mkdir -p "${root}/$(dirname "${path}")"
    cp -p "${fresh}/${path}" "${root}/${path}"
done <"${listing}"
(cd "${root}" && find . -type f) >"${listing}"
while IFS= read -r path; do
    [ -e "${fresh}/${path}" ] || rm -f "${root}/${path}"
done <"${listing}"
