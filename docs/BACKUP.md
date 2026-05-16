# Severino HQ — backup & restore

What needs to be backed up:

1. **The SQLite database** (`SEVERINO_DATABASE_PATH`).
2. **Uploaded receipt files** (`SEVERINO_MEDIA_ROOT`).
3. **Exports directory** (`SEVERINO_EXPORTS_ROOT`) — optional; can be
   regenerated from #1.

Pulling these from a tarball is enough to fully restore the app on a new host.

## Backup approach

The repo ships `scripts/backup.sh`. It:

1. Uses SQLite's `VACUUM INTO` to produce a **consistent snapshot** of the
   live database while the app is still running.
2. Tars together that snapshot, the media directory, and the exports dir.
3. Optionally encrypts the tarball with [`age`](https://age-encryption.org) if
   you have one or more recipient keys.

Set environment variables to point at your paths. Defaults match the
homelab compose layout (`/srv/severino-hq/...`).

```bash
sudo SEVERINO_BACKUP_AGE_RECIPIENTS="age1abc...,age1def..." \
     /opt/severino-hq/scripts/backup.sh
```

A timestamped archive (`severino-hq-<UTC timestamp>.tar.gz` or
`.tar.gz.age`) is dropped in `${SEVERINO_BACKUP_DIR:-/srv/severino-hq/backups}`.

### Schedule it

`/etc/systemd/system/severino-hq-backup.service`:

```ini
[Unit]
Description=Severino HQ backup
After=severino-hq.service

[Service]
Type=oneshot
EnvironmentFile=/etc/severino-hq.env
Environment=SEVERINO_BACKUP_AGE_RECIPIENTS=age1abc...
ExecStart=/opt/severino-hq/scripts/backup.sh
```

`/etc/systemd/system/severino-hq-backup.timer`:

```ini
[Unit]
Description=Severino HQ nightly backup

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

Then `sudo systemctl enable --now severino-hq-backup.timer`.

## Restore

```bash
# 1. Stop the app
sudo systemctl stop severino-hq
# or: docker compose down

# 2. Decrypt if needed
age -d -i ~/.age/severino.key \
    -o severino-hq-restore.tar.gz \
    severino-hq-2026-05-16-0300.tar.gz.age

# 3. Extract somewhere safe
mkdir -p /tmp/severino-restore && cd /tmp/severino-restore
tar -xzf /path/to/severino-hq-restore.tar.gz

# 4. Move the pieces into place
sudo cp severino.sqlite3 /srv/severino-hq/data/severino.sqlite3
sudo rsync -a media/ /srv/severino-hq/media/
sudo rsync -a exports/ /srv/severino-hq/exports/

# 5. Fix ownership
sudo chown -R 10001:10001 /srv/severino-hq      # docker layout
# or: sudo chown -R severino:severino /var/lib/severino-hq

# 6. Sanity-check the DB
sqlite3 /srv/severino-hq/data/severino.sqlite3 'PRAGMA integrity_check;'
# Expected: "ok"

# 7. Restart
sudo systemctl start severino-hq
# or: docker compose up -d

# 8. Sign in, confirm dashboard renders, audit log has the recent events.
```

## Alternative: restic

If you already run `restic` for the rest of your homelab, point it at the
data directories directly. The trick with SQLite is to **snapshot via
`VACUUM INTO` first**, then let restic pick up the snapshot — naive backups of
the live `.sqlite3` file can be torn between writes. The `backup.sh` script
makes that snapshot for you; you can have restic back up
`${SEVERINO_BACKUP_DIR}` afterwards.
