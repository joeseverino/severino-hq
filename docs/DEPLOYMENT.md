# Severino HQ — deployment

Severino HQ is designed for **private, Tailscale-only access** on either:

- a **homelab host running Docker** (recommended), or
- a **small Linux VPS** with systemd + Caddy/Nginx.

Both paths terminate TLS at a reverse proxy and bind the app to localhost or
the Tailscale interface. The public internet never reaches it.

---

## Option A — Docker on the homelab (recommended)

### A.1 Files

This repo ships a `Dockerfile`, `docker-compose.yml`, and `entrypoint.sh` at
the project root.

### A.2 Host preparation

```bash
# On the homelab host
sudo mkdir -p /srv/severino-hq/data /srv/severino-hq/media /srv/severino-hq/exports /srv/severino-hq/static
sudo chown -R 10001:10001 /srv/severino-hq    # matches the non-root UID in the image
```

### A.3 Environment

Copy `.env.example` to `.env` in the project directory and fill it in.
At minimum:

```
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=<long random string>
DJANGO_ALLOWED_HOSTS=severino-hq.<your-tailnet>.ts.net,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://severino-hq.<your-tailnet>.ts.net
DJANGO_BEHIND_TLS_PROXY=1
SEVERINO_DATABASE_PATH=/data/severino.sqlite3
SEVERINO_MEDIA_ROOT=/media
SEVERINO_EXPORTS_ROOT=/exports
DJANGO_STATIC_ROOT=/static
```

### A.4 Build & run

```bash
docker compose build
docker compose run --rm app python manage.py migrate
docker compose run --rm app python manage.py createsuperuser
docker compose up -d
```

The container binds gunicorn to `127.0.0.1:8000` on the host (compose maps
`"127.0.0.1:8000:8000"`). It is **not** reachable from the LAN until you put
something in front of it.

### A.5 Tailscale-only exposure — pick one

Two common patterns:

1. **Tailscale on the host, Caddy on the host** — install Tailscale on the
   homelab host, then run Caddy on the host listening on the host's Tailscale
   IP. Caddy proxies to `127.0.0.1:8000`. This is the simplest.

2. **Tailscale sidecar container** — run a `tailscale/tailscale` container in
   the same Compose project, set `TS_HOSTNAME=severino-hq`, share its network
   namespace with the app via `network_mode: "service:tailscale"`, and let
   Tailscale Serve handle TLS:

       tailscale serve --bg --https=443 http://127.0.0.1:8000

   Magic-DNS gives you `https://severino-hq.<tailnet>.ts.net` automatically.
   Provision the auth-key via `TS_AUTHKEY` (one-time, set up an ephemeral
   reusable key in the Tailscale admin).

Either pattern, the app itself never binds to a public interface.

### A.6 Updates

```bash
git pull
docker compose build
docker compose run --rm app python manage.py migrate
docker compose run --rm app python manage.py collectstatic --noinput
docker compose up -d
```

### A.7 Backups

See `docs/BACKUP.md`. Run `scripts/backup.sh` on the host (it works against the
mounted `/srv/severino-hq/...` directories).

---

## Option B — systemd + Caddy/Nginx on a VPS

### B.1 OS user, directories

```bash
sudo adduser --system --group --home /var/lib/severino-hq severino
sudo mkdir -p /var/lib/severino-hq/{media,exports,staticfiles}
sudo chown -R severino:severino /var/lib/severino-hq
sudo mkdir -p /opt/severino-hq
sudo chown severino:severino /opt/severino-hq
```

### B.2 Code + venv

```bash
sudo -u severino git clone <your-mirror> /opt/severino-hq
cd /opt/severino-hq
sudo -u severino python3 -m venv .venv
sudo -u severino .venv/bin/pip install -r requirements.txt
sudo -u severino cp .env.example /etc/severino-hq.env
sudoedit /etc/severino-hq.env   # fill in real values
```

### B.3 Migrate, create user, collect static

```bash
cd /opt/severino-hq
sudo -u severino bash -c 'set -a; source /etc/severino-hq.env; set +a; \
  .venv/bin/python manage.py migrate && \
  .venv/bin/python manage.py createsuperuser && \
  .venv/bin/python manage.py collectstatic --noinput'
```

### B.4 systemd unit

`/etc/systemd/system/severino-hq.service`:

```ini
[Unit]
Description=Severino HQ
After=network-online.target
Wants=network-online.target

[Service]
User=severino
Group=severino
WorkingDirectory=/opt/severino-hq
EnvironmentFile=/etc/severino-hq.env
ExecStart=/opt/severino-hq/.venv/bin/gunicorn config.wsgi:application \
  --bind 127.0.0.1:8000 --workers 3 --timeout 60
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/severino-hq
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now severino-hq
sudo systemctl status severino-hq
```

### B.5 Tailscale-only Caddy

Find your Tailscale IP (`tailscale ip -4`) or magic-DNS name. Bind Caddy to
the Tailscale interface only — for example `100.x.y.z:443`:

```caddy
severino-hq.<your-tailnet>.ts.net {
    bind 100.x.y.z
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "same-origin"
        X-Frame-Options "DENY"
    }
}
```

(With `tailscale serve` you can also let Tailscale terminate TLS directly; in
that case point it at `http://127.0.0.1:8000` and skip Caddy.)

### B.6 Nginx alternative

```nginx
server {
    listen 100.x.y.z:443 ssl http2;
    server_name severino-hq.<your-tailnet>.ts.net;

    ssl_certificate     /etc/letsencrypt/live/<host>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<host>/privkey.pem;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;
    add_header X-Frame-Options "DENY" always;

    client_max_body_size 16M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

---

## Verifying the deployment

```bash
# From the VPS / homelab host (NOT the public internet)
curl -I http://127.0.0.1:8000/accounts/login/

# From a device on the tailnet
open https://severino-hq.<your-tailnet>.ts.net/
```

The app should redirect every URL to `/accounts/login/` for unauthenticated
clients. After signing in, the dashboard loads and the audit log records the
event.

## Common gotchas

- **502 from Caddy/Nginx** — the app isn't running on `127.0.0.1:8000`.
  Check `systemctl status severino-hq` or `docker compose logs app`.
- **CSRF errors after sign-in** — your `DJANGO_CSRF_TRUSTED_ORIGINS` doesn't
  include the full origin (scheme + host).
- **`SECRET_KEY must be set`** — the env file isn't being read by the unit.
  Check `EnvironmentFile=` and that the file is readable by the service user.
- **Receipt downloads 404** — `SEVERINO_MEDIA_ROOT` doesn't match where the
  file was originally written. Make sure the value is stable across restarts.
