#!/usr/bin/env bash
# Severino HQ backup script.
#
# - Uses SQLite "VACUUM INTO" for a consistent snapshot of the live DB.
# - Tars the snapshot + media + exports into a single archive.
# - Optionally encrypts the archive with `age` if SEVERINO_BACKUP_AGE_RECIPIENTS
#   is set (comma-separated age recipients).
#
# Env (with defaults matching the homelab docker compose layout):
#   SEVERINO_DATABASE_PATH      /srv/severino-hq/data/severino.sqlite3
#   SEVERINO_MEDIA_ROOT         /srv/severino-hq/media
#   SEVERINO_EXPORTS_ROOT       /srv/severino-hq/exports
#   SEVERINO_BACKUP_DIR         /srv/severino-hq/backups
#   SEVERINO_BACKUP_AGE_RECIPIENTS   (optional; e.g. "age1abc...,age1def...")
#
# Exit nonzero on any failure.

set -euo pipefail

DB_PATH="${SEVERINO_DATABASE_PATH:-/srv/severino-hq/data/severino.sqlite3}"
MEDIA_ROOT="${SEVERINO_MEDIA_ROOT:-/srv/severino-hq/media}"
EXPORTS_ROOT="${SEVERINO_EXPORTS_ROOT:-/srv/severino-hq/exports}"
BACKUP_DIR="${SEVERINO_BACKUP_DIR:-/srv/severino-hq/backups}"
AGE_RECIPIENTS="${SEVERINO_BACKUP_AGE_RECIPIENTS:-}"

if [[ ! -f "${DB_PATH}" ]]; then
  echo "Database not found at ${DB_PATH}" >&2
  exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 binary not found in PATH" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$(mktemp -d -t severino-backup-XXXXXX)"
trap 'rm -rf "${STAGE}"' EXIT

DB_SNAPSHOT="${STAGE}/severino.sqlite3"

echo "[1/3] Snapshotting SQLite database via VACUUM INTO…"
# VACUUM INTO needs an absolute path with single quotes inside the SQL.
sqlite3 "${DB_PATH}" "VACUUM INTO '${DB_SNAPSHOT}';"

# Integrity check to catch hardware-level corruption early.
INTEGRITY="$(sqlite3 "${DB_SNAPSHOT}" 'PRAGMA integrity_check;')"
if [[ "${INTEGRITY}" != "ok" ]]; then
  echo "Integrity check failed on snapshot: ${INTEGRITY}" >&2
  exit 1
fi

echo "[2/3] Assembling tarball…"
ARCHIVE="${BACKUP_DIR}/severino-hq-${STAMP}.tar.gz"
# Use -C to put each piece at a predictable top-level path inside the tar.
tar -czf "${ARCHIVE}" \
  -C "${STAGE}" "severino.sqlite3" \
  $( [[ -d "${MEDIA_ROOT}" ]]   && echo "-C $(dirname "${MEDIA_ROOT}") $(basename "${MEDIA_ROOT}")" ) \
  $( [[ -d "${EXPORTS_ROOT}" ]] && echo "-C $(dirname "${EXPORTS_ROOT}") $(basename "${EXPORTS_ROOT}")" )

chmod 600 "${ARCHIVE}"

if [[ -n "${AGE_RECIPIENTS}" ]]; then
  if ! command -v age >/dev/null 2>&1; then
    echo "AGE recipients set but \`age\` binary not in PATH" >&2
    exit 1
  fi
  echo "[3/3] Encrypting with age…"
  RECIPIENT_ARGS=()
  IFS=',' read -ra RECIPS <<< "${AGE_RECIPIENTS}"
  for r in "${RECIPS[@]}"; do
    RECIPIENT_ARGS+=(-r "${r}")
  done
  age "${RECIPIENT_ARGS[@]}" -o "${ARCHIVE}.age" "${ARCHIVE}"
  shred -u "${ARCHIVE}" 2>/dev/null || rm -f "${ARCHIVE}"
  chmod 600 "${ARCHIVE}.age"
  echo "Wrote ${ARCHIVE}.age"
else
  echo "[3/3] Encryption skipped (SEVERINO_BACKUP_AGE_RECIPIENTS not set)."
  echo "Wrote ${ARCHIVE}"
fi
