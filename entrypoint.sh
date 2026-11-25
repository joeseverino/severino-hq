#!/usr/bin/env sh
# The container's boot: migrations first, keeping the database where the host
# says so a failed release can put it back, then the image's CMD, with one
# Django setup before the server's own. A failure to migrate stops the boot;
# the delivery read `boot` asks for after it is best effort.

set -eu

echo "[severino-hq] applying migrations…"
python manage.py boot

echo "[severino-hq] starting: $*"
exec "$@"
