#!/bin/sh
# Collect the static assets into the image: every asset under its content-hashed
# name with a gzip copy beside it, and the manifest that maps the names.
#
#   scripts/collect-assets.sh
#
# An image build runs it as root, once for the host and again in a composition
# after its extensions are installed, since they bring assets of their own. A
# container start never collects: /static is the image's and read-only.

set -eu
# Root collects; another account serves.
umask 022

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"

# The settings refuse to load without a signing key. Collecting signs nothing,
# and this one exists for this process alone.
key="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
DJANGO_SECRET_KEY="${key}" python manage.py collectstatic --noinput --clear --verbosity 0

root="${DJANGO_STATIC_ROOT:?DJANGO_STATIC_ROOT is unset}"
# Without the manifest a page falls back to plain names.
[ -s "${root}/staticfiles.json" ] || {
    echo "collectstatic wrote no manifest in ${root}." >&2
    exit 1
}
unreadable="$(find "${root}" ! -perm -004 | head -n 5)"
if [ -n "${unreadable}" ]; then
    printf '%s\n' "${unreadable}" >&2
    echo "Collected assets the serving account cannot read." >&2
    exit 1
fi
