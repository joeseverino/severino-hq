#!/bin/sh
# What a deploy from before hq-host runs once it has synced the tree.
set -eu
exec /usr/local/lib/severino-hq/deploy/bin/hq-host install
