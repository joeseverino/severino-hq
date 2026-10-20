# Backup

HQ's state is three things:

1. The SQLite database (`SEVERINO_DATABASE_PATH`).
2. Uploaded receipt files (`SEVERINO_MEDIA_ROOT`).
3. Generated exports (`SEVERINO_EXPORTS_ROOT`), which can be regenerated from
   the database.

## The backup

`hq-host backup` ([`controller/host/backup.go`](../controller/host/backup.go))
asks docker where HQ's volumes are, snapshots the live database with SQLite's
`VACUUM INTO`, so a write in progress cannot tear it, checks the snapshot's
integrity, and archives it with the uploads and exports as `severino.sqlite3`,
`media/` and `exports/`. With recipients set the archive is streamed through
[age](https://age-encryption.org), so no plaintext archive is written. The
archive is `severino-hq-<UTC timestamp>.tar.gz` or `.tar.gz.age` in
`SEVERINO_BACKUP_DIR` (`/srv/severino-hq/backups` by default), mode `0600`,
and appears whole or not at all. The host needs `sqlite3`, and `age` when
encrypting.

`severino-hq-backup.timer` runs it nightly as root. The unit requires
encryption and keeps 30 days of archives. Set the recipients (comma-separated,
anything `age -r` takes), and any other override, in
`/etc/severino-hq/backup.env`, root-owned, mode `0600`:

```sh
SEVERINO_BACKUP_AGE_RECIPIENTS=age1...
SEVERINO_BACKUP_RETENTION_DAYS=30
```

A missing database fails the run instead of skipping it.

`controller/host/backup_test.go` is the restore drill: it builds a database
with media and exports, runs the backup, extracts the archive with `tar`,
queries the restored database and compares the files. CI runs it on every
pull request.

A local archive is not an off-host backup. Replicate `SEVERINO_BACKUP_DIR` to
separate storage (restic, or another reviewed mechanism) and watch the
nightly unit's result. Back up the snapshot, never the live `.sqlite3` file.

## Restore

```sh
sudo systemctl stop severino-hq-web.service
age -d -i severino-backup.key -o restore.tar.gz severino-hq-<timestamp>.tar.gz.age
mkdir restore && tar -xzf restore.tar.gz -C restore
```

Copy `severino.sqlite3`, `media/` and `exports/` into the volumes
(`docker volume inspect --format '{{.Mountpoint}}' severino-hq_severino_data`,
and `_media`, `_exports`), owned by the image's account (`10001:10001`), then:

```sh
sqlite3 <database> 'PRAGMA integrity_check;'
sudo systemctl start severino-hq-web.service
```

`integrity_check` answers `ok`. Sign in and confirm the dashboard and the
audit log's recent events. The container migrates on start.
