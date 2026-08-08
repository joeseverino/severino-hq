#!/bin/sh
# Prove a backup can be restored into a fresh SQLite database.

set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
readonly script_dir
fixture_dir="$(mktemp -d)"
readonly fixture_dir
trap 'rm -rf "${fixture_dir}"' EXIT HUP INT TERM

mkdir -p \
    "${fixture_dir}/source/media" \
    "${fixture_dir}/source/exports" \
    "${fixture_dir}/backups" \
    "${fixture_dir}/restore"
sqlite3 "${fixture_dir}/source/severino.sqlite3" \
    "CREATE TABLE proof (value TEXT NOT NULL); INSERT INTO proof VALUES ('restorable');"
printf '%s\n' 'receipt-proof' >"${fixture_dir}/source/media/receipt.txt"
printf '%s\n' 'export-proof' >"${fixture_dir}/source/exports/report.txt"

if SEVERINO_DATABASE_PATH="${fixture_dir}/source/severino.sqlite3" \
    SEVERINO_BACKUP_DIR="${fixture_dir}/backups" \
    SEVERINO_BACKUP_REQUIRE_ENCRYPTION=1 \
    "${script_dir}/backup.sh" >/dev/null 2>&1; then
  echo "Encryption-required backup succeeded without recipients." >&2
  exit 1
fi

SEVERINO_DATABASE_PATH="${fixture_dir}/source/severino.sqlite3" \
SEVERINO_MEDIA_ROOT="${fixture_dir}/source/media" \
SEVERINO_EXPORTS_ROOT="${fixture_dir}/source/exports" \
SEVERINO_BACKUP_DIR="${fixture_dir}/backups" \
    "${script_dir}/backup.sh"

archive="$(find "${fixture_dir}/backups" -type f -name '*.tar.gz' -print -quit)"
test -n "${archive}"
tar -xzf "${archive}" -C "${fixture_dir}/restore"
test "$(sqlite3 "${fixture_dir}/restore/severino.sqlite3" 'SELECT value FROM proof;')" = "restorable"
test "$(cat "${fixture_dir}/restore/media/receipt.txt")" = "receipt-proof"
test "$(cat "${fixture_dir}/restore/exports/report.txt")" = "export-proof"

echo "Backup restore drill passed."
