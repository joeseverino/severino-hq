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
asks Docker where HQ's volumes are. It snapshots the live database with
SQLite's `VACUUM INTO`, so a write in progress cannot tear it, and checks the
snapshot's integrity. It then archives the snapshot with the uploads, the
exports and the sealing seed as `severino.sqlite3`, `media/`, `exports/` and
`seal/`. A host whose controller has made no seed yet has no `seal/`. The
archive is encrypted as it is written, in process with
[age](https://age-encryption.org)'s own library, so no plaintext archive is
written. The archive is `severino-hq-<UTC timestamp>.tar.gz.age` in
`/srv/severino-hq/backups`, mode `0640` with the web group (the controller
reads it for the [off-site copy](#the-off-site-copy)), and appears whole or
not at all. The host needs `sqlite3`.

The seed travels in the same archive as the database, so a restore opens every
sealed value without anyone entering it again. An identity that opens an
archive therefore opens every secret HQ holds: keep it as you keep those
secrets.

`severino-hq-backup.timer` runs it nightly as root and keeps 30 days of
archives. Every archive is encrypted, and only to post-quantum hybrid
recipients (`age1pq1...`, ML-KEM-768 with X25519). A run with no recipient,
or with a classic `age1...` X25519 one, is refused before anything is
written. Make the key on the operator's machine with `age-keygen -pq` (age
1.3 or later) and keep the identity off the host. Set its recipient
(comma-separated for more than one) and the retention in
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
it with `tar`, queries the restored database and compares the files. CI
runs it on every pull request.

Back up the snapshot, never the live `.sqlite3` file.

## The off-site copy

A local archive is not an off-host backup. HQ copies each one to a
[Cloudflare R2](https://developers.cloudflare.com/r2/) bucket under a
retention lock, through the optional `r2` connection
([Connections](Connections.md#cloudflare-r2-backups)).

The backup unit, which holds the plaintext, has no network. Root makes the
archive readable by the web group before it appears, and lends the newest one,
read-only, to each applying controller pass (`lendArchive` in
`controller/host/controller.go`). The controller, which never sees plaintext,
lists the bucket. When the bucket lacks the archive and the connection
manages, it uploads it with `If-None-Match: *`, then lists the bucket again
(`controller/providers/r2.go`). That listing is the `r2.backup` reading: the
newest archive's key, size and upload time, and how many the bucket keeps,
shown on the connection's page and beside HQ's jobs on its own page. The
upload and the pages agree because they read the same listing. An archive
arrives on the first applying pass after the nightly run. The finding
`offsite-backup-late` raises once two of the backup timer's runs, each up to
its randomized delay late, pass with no newer copy, or with none at all once
the connection has been set up that long (`backup_findings`, reading the
timer's own `OnCalendar` and `RandomizedDelaySec`).

What holds:

- Only ciphertext leaves the host. The controller is lent one file whose name
  is an archive's, and refuses to upload any other.
- The web group reading an archive opens nothing: the identity stays off the
  host.
- The token on the host is an R2 Object Read & Write token scoped to the one
  bucket. It cannot read another bucket or change a bucket's configuration.
- The bucket lock refuses an overwrite or delete of an object until its
  retention passes, even for that token, and a blocked request fails with
  `409 ObjectLockedByBucketPolicy`. A lock takes precedence over lifecycle
  rules ([bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/)),
  so a stolen token cannot destroy what the bucket holds.
- The token that can change the lock (Admin Read & Write) is never on the
  host. Make the bucket with it on the operator's machine, then revoke it or
  keep it there.

Requests are signed with the AWS SDK's SigV4 signer alone
(`github.com/aws/aws-sdk-go-v2/aws/signer/v4`), which adds a fraction of the
weight its S3 client would. One `PUT` carries an archive, up to R2's 5 GiB
single-upload limit.

### Setting it up

Once, on the operator's machine, with
[Wrangler](https://developers.cloudflare.com/workers/wrangler/) signed in to
the account (`<bucket>` is a name of your choosing):

```sh
npx wrangler r2 bucket create <bucket> && npx wrangler r2 bucket lock add <bucket> keep --retention-days 90 --force && npx wrangler r2 bucket lifecycle add <bucket> expire --expire-days 91 --force
```

The lock keeps each copy for 90 days, and the lifecycle rule removes it the
day after. Then, in the dashboard under R2, Manage API tokens, create an
Object Read & Write token for that bucket alone. Store its Access Key ID,
Secret Access Key, the account's S3 endpoint
(`https://<account id>.r2.cloudflarestorage.com`) and the bucket as the item
[Connections](Connections.md#cloudflare-r2-backups) describes, with `manages`
set to `1`. The next controller pass probes the bucket, and the one after the
nightly backup uploads its archive.

To restore from the bucket, fetch an archive and follow [Restore](#restore):

```sh
npx wrangler r2 object get <bucket>/severino-hq-<timestamp>.tar.gz.age --file severino-hq-<timestamp>.tar.gz.age --remote
```

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
`0700` and each seed `0400`, owned by the same account. First remove any seed
a new host already made, since a pass refuses to choose between two. The next
controller pass writes the public key from it. Then:

```sh
sqlite3 <database> 'PRAGMA integrity_check;'
sudo systemctl start severino-hq-web.service
```

`integrity_check` answers `ok`. Sign in and confirm the dashboard and the
audit log's recent events. The container migrates on start.
