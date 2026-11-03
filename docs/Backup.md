# Backup

HQ's state is four things:

1. The SQLite database (`SEVERINO_DATABASE_PATH`).
2. Uploaded receipt files (`SEVERINO_MEDIA_ROOT`).
3. Generated exports (`SEVERINO_EXPORTS_ROOT`), which can be regenerated from
   the database.
4. The controller's sealing seed (`/opt/apps/severino-hq/secrets/seal/`),
   without which no value HQ sealed to the controller can be opened
   ([Secrets](Secrets.md#sealing-to-the-controller)).

## The backup

`hq-host backup` ([`controller/host/backup.go`](../controller/host/backup.go))
asks docker where HQ's volumes are, snapshots the live database with SQLite's
`VACUUM INTO`, so a write in progress cannot tear it, checks the snapshot's
integrity, and archives it with the uploads, the exports and the sealing seed
as `severino.sqlite3`, `media/`, `exports/` and `seal/`. A host whose
controller has made no seed yet has no `seal/`. The archive is encrypted as it is written, in process
with [age](https://age-encryption.org)'s own library, so no plaintext archive
is written. The archive is `severino-hq-<UTC timestamp>.tar.gz.age` in
`SEVERINO_BACKUP_DIR` (`/srv/severino-hq/backups` by default), mode `0600`,
and appears whole or not at all. The host needs `sqlite3`.

`severino-hq-backup.timer` runs it nightly as root and keeps 30 days of
archives. Every archive is encrypted, and only to post-quantum hybrid
recipients (`age1pq1...`, ML-KEM-768 with X25519): a run with no recipient,
or with a classic `age1...` X25519 one, is refused before anything is
written. Make the key on the operator's machine with `age-keygen -pq` (age
1.3 or later), keep the identity off the host, and set its recipient
(comma-separated for more than one), and any other override, in
`/etc/severino-hq/backup.env`, root-owned, mode `0600`:

```sh
SEVERINO_BACKUP_AGE_RECIPIENTS=age1pq1...
SEVERINO_BACKUP_RETENTION_DAYS=30
```

When the key is replaced, keep the previous identity until the last archive
encrypted to it ages out, `SEVERINO_BACKUP_RETENTION_DAYS` after the change.

A missing database fails the run instead of skipping it.

`controller/host/backup_test.go` is the restore drill. It builds a database
with media, exports and a sealing seed, and runs the backup for a freshly
generated post-quantum identity. It then decrypts the archive with it, extracts
it with `tar`, queries the restored database and compares the files. CI runs it on every
pull request.

A local archive is not an off-host backup. Replicate `SEVERINO_BACKUP_DIR` to
separate storage (restic, or another reviewed mechanism) and watch the
nightly unit's result. Back up the snapshot, never the live `.sqlite3` file.

## Restore

Decrypting needs age 1.3 or later, which reads post-quantum identities:

```sh
sudo systemctl stop severino-hq-web.service
age -d -i severino-backup.key -o restore.tar.gz severino-hq-<timestamp>.tar.gz.age
mkdir restore && tar -xzf restore.tar.gz -C restore
```

Copy `severino.sqlite3`, `media/` and `exports/` into the volumes
(`docker volume inspect --format '{{.Mountpoint}}' severino-hq_severino_data`,
and `_media`, `_exports`), owned by the image's account (`10001:10001`).
Copy `seal/` to `/opt/apps/severino-hq/secrets/seal/`, the directory mode
`0700` and each seed `0400`, owned by the same account, first removing any
seed a new host already made: a pass refuses to choose between two. The next
controller pass writes the public key from it. Then:

```sh
sqlite3 <database> 'PRAGMA integrity_check;'
sudo systemctl start severino-hq-web.service
```

`integrity_check` answers `ok`. Sign in and confirm the dashboard and the
audit log's recent events. The container migrates on start.
