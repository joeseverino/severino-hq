#!/bin/sh
# The sudoers rule names this path. The deploy is hq-host's: it verifies,
# pulls and hands over to the release being installed.
set -eu
exec /usr/local/lib/severino-hq/deploy/bin/hq-host deploy "$@"
