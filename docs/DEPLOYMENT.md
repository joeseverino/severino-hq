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

### `SEVERINO_SECRET_STORE_KEY`

One field on the `severino-hq env` item, and the only secret HQ holds rather
than reads. It seals a certificate the operator generated against the offline CA
and asked HQ to install — the leaf and its key, the same pair that would
otherwise be pasted into a provider's web form by hand. Provider credentials are
unaffected and stay outside the web container.

Any value of 32 characters or more works; the Fernet key is derived from it, so
the entry is an ordinary long secret rather than something with a format to get
right. Generate it with the 1Password app's own generator so the value never
reaches a shell history.

Unset, HQ refuses to accept a private key and says so on the page. It never
falls back to storing one in the clear.

Rotating it makes anything already sealed unreadable, and unsealing reports that
rather than returning an empty secret. Rotate only when you are willing to
re-upload every stored certificate.

Connection projections are declared once in
`config/controller-connections.json`. Both secret rendering and runtime
forwarding derive their variable names from that registry; a new credential
shape is added as a projection profile instead of duplicated shell logic.
Built-in 1Password fields may be selected by stable ID. Custom fields must be
selected by their stable, unique label because 1Password assigns an opaque ID
per item; the renderer rejects missing or duplicate matches.

#### Adding a connection

Create the item. Nothing else. `connection_ref`, `projection` and `env_prefix`
on the item are what make it one, and the renderer reads them, so no file in
this repository names any connection.

What kind of thing it is comes from the env prefix — `ADGUARD_*` is AdGuard,
`PORTAINER_*` is Portainer — unless the item carries a `provider` field, which
overrides it. That field is what lets two of a kind coexist: `PORTAINER_HOME`
and `PORTAINER_CLOUD` are both `portainer`, and each resource says which it
uses. It is optional, so an existing vault keeps working untouched.

On each pass the controller probes every connection it was handed and reports
what answered and what that thing can act on — the machines behind a Portainer,
the zones a DNS token may edit. HQ stores the report, not the credential, and
`/infrastructure/connections/` is that report. Every menu asking "which machine"
or "which domain" is derived from it, so registering a new VPS with Portainer is
the whole of making it a place HQ can deploy to.

OAuth probes exchange the injected client credential for a short-lived access
token, discard that token immediately, and report only safe connection health.
Neither the client secret nor the access token crosses the controller boundary.

The controller trusts internal provider TLS through the host trust store or a
deployment-provided `HQ_CONTROLLER_CA_FILE`. The internal CA certificate
is not stored in this public repository. Never disable TLS verification.

`hq sync` asks the Vault MCP for its complete validated manifest, then submits
it in one authenticated `hq.sync` Streamable HTTP MCP call over Tailscale. HQ
validates and commits it in one transaction. No intermediate payload is written
on homelab-server, and routine synchronization requires no SSH access. What HQ
holds about the infrastructure itself is not synchronized from anywhere: it is
swept, or declared in HQ.

The gated `main` deployment runs `scripts/install-controller.sh` after the new
application image is healthy. The installer refreshes controller-only
credentials, validates the systemd units, authenticates read-only to every
declared provider in plan mode, and only then enables the apply timer. Missing
credentials, untrusted TLS, and API failures stop activation. The HQ web
container never receives the provider environment.

The same activation gate performs an authenticated pull of the live
`jseverino.com` content index before installing and enabling its persistent
daily timer. Cloudflare Access credentials come from uppercase fields on the
existing `severino-hq env` item through the normal app-environment projection;
there is no second credential registry. A restart cannot lose the schedule:
systemd owns it, catches up missed runs, and the deployment revalidates the
pull before declaring the release healthy.

The controller claims only kind/action pairs a provider marks `apply`. Each
provider declares what may be done to it, and which of those may run
unprompted, beside its own definition in `control_plane/providers.py`; the
contract handed to the worker is assembled from those. A test cross-checks the
claim against the handler table, so a kind cannot declare `apply` with no code
behind it, or `locked` while quietly having some. Its persistent systemd
timer runs after boot and every five minutes. Each run drains infrastructure
work and derives
new work from HQ's verified state: it queues
renewal inside the configured window and reconciliation for new generations or
drift. TLS reconciliation redistributes the existing lineage;
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

Pull requests run application checks, build the production image, boot it to
readiness, and scan it with Trivy. A push to `main` publishes and scans the
image but does **not** deploy it.

Deployment is the composition workflow's job, and it is the only path to
production. It waits for the host workflow to finish, rebuilds every admitted
extension onto the new host image, and deploys that. Two deploy paths existed
once — the host's and each extension's — and whichever ran last won, so a host
release silently dropped every extension out of production.
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
than Docker's bridge gateway. A co-located reverse proxy should forward the
browser UI to `127.0.0.1:8000`; its socket address is then the only entry in
`SEVERINO_TRUSTED_PROXIES`. The browser's WireGuard peering terminates at the
host's Tailscale daemon, Nginx preserves the real Tailnet caller in its standard
forwarding headers, and the loopback hop into HQ is not misrepresented as a
second policed Tailnet crossing. The UI remains protected by Django
authentication; `/mcp/` independently requires a direct Tailscale peer, an
allowed Host header, and the MCP bearer token.

For Nginx Proxy Manager, attach an access list whose client rules allow exactly
the Tailscale IPv4 and IPv6 ranges in `SEVERINO_TRUSTED_NETWORKS`, in that
order. Do not copy its loopback entries: they describe the local proxy-to-HQ
hop, not a caller Nginx should admit.

NPM generates the final `deny all` whenever client rules exist. Its editor
shows that generated row disabled. Adding another editable deny is harmless
but redundant; HQ's provider projection records the implicit default so the
effective Tailnet-only policy is derived without duplicating configuration.
Keep `satisfy_any` and proxy authorization disabled. As defense in depth, limit
host ingress for 443 and direct MCP port 8000 to `tailscale0` (plus loopback
where needed), and ensure no router forwards either port publicly.

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

The live homelab updates through the gated CI/CD pipeline. A push to `main`
builds and scans the **host** image; the **Compose and deploy extensions**
workflow then builds one image from that host plus every admitted extension, and
a self-hosted runner deploys it health-gated with rollback. Production runs the
composed image (`…/composition:…`), never the host image on its own. Migrations
and `collectstatic` run on container boot via `entrypoint.sh`.

An extension merge deploys too, without anything being run by hand: the
composition workflow runs on a schedule and rebuilds when the extension wheel
digests change. See [`PLUGINS.md`](PLUGINS.md#composition). To deploy an
extension immediately, run that workflow by hand (`workflow_dispatch`).

> **`hq deploy` is legacy — do not run it.** It predates composition and
> deploys the *host-only* image, which takes every extension off production
> until the next composition. To rebuild by hand, run **Compose and deploy
> extensions**; to roll back, re-run it at the commit you want.

The equivalent **manual** steps, for a standalone or first-time deploy, are:

```bash
git pull
docker compose build
docker compose run --rm app python manage.py migrate
docker compose run --rm app python manage.py collectstatic --noinput
docker compose up -d
```

### A.7 Backups

See `docs/BACKUP.md`. The deployment installer enables the committed nightly
backup timer, and CI proves the produced archive can restore the database,
media, and exports. Off-host replication remains an explicit operator duty.

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
