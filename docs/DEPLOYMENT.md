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
SEVERINO_MCP_TOKEN_FILE_HOST=<root-only validator token file provisioned from 1Password>
SEVERINO_MCP_ALLOWED_HOSTS=<direct Tailscale IP>,<MagicDNS hostname>
```

Production refreshes the validator token AND the full app environment from
the dedicated 1Password vault with `severino-hq-secrets.service`
(`scripts/refresh-secrets.sh`). The app env renders from the `severino-hq env`
item into a root-owned file the entrypoint sources — compose has no
`env_file`, and the on-host `.env` holds only the two non-secret
`*_FILE_HOST` interpolation paths. The service-account token is a host-bound
encrypted systemd credential, not an environment-file value. The hourly timer
keeps rotations current and retains the last-known-good values if 1Password
is temporarily unavailable. To change a prod env var: edit the 1Password
item, then `systemctl start severino-hq-secrets.service` (or wait for the
timer; the container restarts only when something actually changed).

Provider credentials are separate from the app environment. Login items in the
same vault declare a stable `connection_ref`; `scripts/render-controller-env.sh`
discovers them through that field and renders
`secrets/severino_controller_env`. The controller service reads that root-owned
file directly. `scripts/run-controller.sh` forwards the derived variables only
to a short-lived `docker exec` process running from the exact deployed HQ image.
The file is never mounted into the HQ web container, provider variables do not
enter the long-running web process or container configuration, and provider
passwords are never copied into the `severino-hq env` item.

Connection projections are declared once in
`config/controller-connections.json`. Both secret rendering and runtime
forwarding derive their variable names from that registry; a new credential
shape is added as a projection profile instead of duplicated shell logic.
Built-in 1Password fields may be selected by stable ID. Custom fields must be
selected by their stable, unique label because 1Password assigns an opaque ID
per item; the renderer rejects missing or duplicate matches.

The controller trusts internal provider TLS through the host trust store or a
deployment-provided `SEVERINO_CONTROLLER_CA_FILE`. The internal CA certificate
is not stored in this public repository. Never disable TLS verification.

The topology inventory is also absent from this public repository. `hq sync`
asks the Vault MCP for its complete validated inventory projection and streams
it directly over SSH into `infrastructure_topology` on the trusted HQ
container. HQ validates it again, checksums it, and replaces the singleton
snapshot transactionally. No intermediate payload is written on
homelab-server.

The gated `main` deployment runs `scripts/install-controller.sh` after the new
application image is healthy. The installer refreshes controller-only
credentials, validates the systemd units, authenticates read-only to every
declared provider in plan mode, and only then enables the apply timer. Missing
credentials, untrusted TLS, and API failures stop activation. The HQ web
container never receives the provider environment.

The controller claims only kind/action pairs marked `apply` in
`config/controller-capabilities.json`. Its persistent systemd timer runs after
boot and every minute. Each run derives work from HQ's verified state: it queues
renewal inside the configured window and reconciliation for new topology
generations or drift. TLS reconciliation redistributes the existing lineage;
it does not issue. The NPM adapter discovers every enabled proxy host whose
name is covered by the certificate, replaces their single managed certificate
binding, reloads them, and live-verifies the shared fingerprint. Transactional
renewal is active; public-DNS reconciliation remains locked.

Do not use the web application's `CLOUDFLARE_API_TOKEN` for DNS-01. That token
belongs exclusively to the D1 contact-submission path. DNS-01 uses the separate
`Cloudflare DNS - HQ Controller` API Credential item in the `Severino HQ
Production` vault. Its stable `connection_ref` is
`cloudflare-dns-jseverino`; the controller resolves that reference through
`config/controller-connections.json`. The token is restricted to Zone Read and
DNS Edit for `jseverino.com`, `jseverino.net`, `jseverino.org`, and
`joeseverino.com`. Controller activation verifies the token and proves all four
zones are readable without performing a DNS mutation.

Deployment identities are machine-specific SSH keys generated on
`homelab-server` by `scripts/provision-controller-ssh.sh`. They never enter
1Password, the repository, the web container, or Joe's Mac keychain. The same
connection registry emits each target's host, port, remote user, and pinned
Ed25519 host key; `scripts/controller-ssh.sh` derives strict, batch-only,
operation-allowlisted SSH invocations from it. It does not accept arbitrary
remote commands. Authorize each generated `.pub` key with the narrowest
remote account or forced command available. Renewal stays locked until both
deployment paths pass non-mutating preflight, deployment, live-certificate
verification, and rollback tests. Renewal runs in a disposable container from
the exact deployed image. It alone receives the controller-only ACME lineage,
controller credentials, and deployment keys; none are mounted into the web
container. It runs without Linux capabilities as the application-data UID;
the systemd launcher removes its short-lived secret projections on exit.
Before issuance it snapshots the known-good Caddy artifact. Any
consumer failure triggers compensating deployment of that artifact to every
consumer, and success is reported only after all live verification names serve
the new SHA-256 fingerprint.

The reviewed receivers are versioned in `deploy/targets/`. Install the edge
controller and dispatcher root-owned, force the edge key to the dispatcher,
and allow that account to sudo only the controller. Install the cPanel
controller as the cPanel account and force its key directly to that script.
Both scripts reject every operation outside their explicit allowlist. This is
an administrator bootstrap boundary; application deployment cannot rewrite its
own remote authorization policy.

Pull requests run checks only; a push to `main` runs the image build, scan,
homelab deployment, health verification, and controller activation.
`scripts/deploy-image.sh` stops reconciliation, records the currently running
image, and restores it automatically if the exact SHA-tagged replacement does
not become healthy or its controller cannot pass activation. After rollback,
the controller remains stopped for explicit operator review.

### A.4 Build & run

```bash
docker compose build
docker compose run --rm app python manage.py migrate
docker compose run --rm app python manage.py createsuperuser
docker compose up -d
```

The container uses host networking and binds Uvicorn to port `8000`. Host
networking is required so `/mcp/` sees the real Tailscale peer address rather
than Docker's bridge gateway. The bridge-networked reverse proxy reaches the
browser UI through the host LAN address. The UI remains protected by Django
authentication; `/mcp/` independently requires a direct Tailscale peer, an
allowed Host header, and the MCP bearer token.

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

The live homelab updates through the gated CI/CD pipeline (a push to `main`
builds a Trivy-scanned GHCR image that a self-hosted runner pulls — see the
README's *How changes reach HQ*). Migrations and `collectstatic` run on
container boot via `entrypoint.sh`. The equivalent **manual** steps, for a
standalone or first-time deploy, are:

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
ExecStart=/opt/severino-hq/.venv/bin/uvicorn config.asgi:application \
  --host 127.0.0.1 --port 8000 --no-proxy-headers
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

For internal provider HTTPS, set `SEVERINO_CONTROLLER_CA_FILE_HOST` to the
host's public homelab root certificate. Compose mounts it read-only; provider
requests retain normal public trust and add this CA instead of disabling TLS
verification.

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
