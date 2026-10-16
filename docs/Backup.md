# Backup

HQ's state is three things:

1. The SQLite database (`SEVERINO_DATABASE_PATH`).
2. Uploaded receipt files (`SEVERINO_MEDIA_ROOT`).
3. Generated exports (`SEVERINO_EXPORTS_ROOT`), which can be regenerated from
   the database.

## The backup

[`scripts/backup.sh`](../scripts/backup.sh) snapshots the live database with
SQLite's `VACUUM INTO`, so a write in progress cannot tear it, tars the
snapshot with the media and exports directories, and encrypts the archive with
[age](https://age-encryption.org) when recipients are set. The archive is
`severino-hq-<UTC timestamp>.tar.gz` or `.tar.gz.age` in
`SEVERINO_BACKUP_DIR`. Each path is an environment variable; the defaults are
`/srv/severino-hq/backups` for the archives.

`severino-hq-backup.timer` runs `hq-host backup` nightly as root, which asks
docker where HQ's volumes are and runs the script over them, so the host
arranges no path for the backup to find the data. The unit requires encryption and keeps
30 days of archives. Set the recipients, and any other override, in
`/etc/severino-hq/backup.env`, root-owned, mode `0600`:

```sh
SEVERINO_BACKUP_AGE_RECIPIENTS=age1...
SEVERINO_BACKUP_RETENTION_DAYS=30
```

A missing database fails the run instead of skipping it.

`scripts/test-backup.sh` is the restore drill: it builds a database with media
and exports, runs the real script, extracts the archive, queries the restored
database and compares the files. CI runs it on every pull request.

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
