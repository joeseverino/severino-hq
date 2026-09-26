# Severino HQ: deployment

Severino HQ is designed for **private, Tailscale-only access** on either:

- a **homelab host running Docker** (recommended), or
- a **small Linux VPS** with systemd + Caddy/Nginx.

Both paths terminate TLS at a reverse proxy and bind the app to localhost or
the Tailscale interface. The public internet never reaches it.

---

## Option A: Docker on the homelab (recommended)

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
SEVERINO_MCP_ALLOWED_HOSTS=<direct Tailscale IP>,<MagicDNS hostname>
```

For Connect bootstrap, isolation, and cutover requirements, see
[Secret delivery and Connect migration](SECRETS.md). Connect is opt-in during
migration; production authentication does not change merely by updating code.

Production refreshes the validator token AND the full app environment from
the dedicated 1Password vault with `severino-hq-secrets.service`
(`scripts/refresh-secrets.sh`). The app env renders from the app-environment
item into a root-owned file the entrypoint sources: compose has no
`env_file`, and the on-host `.env` holds only the two non-secret
`*_FILE_HOST` interpolation paths. The renderer's authentication token is a
host-bound encrypted systemd credential, not an environment-file value. Select
the backend and credential explicitly in a host-owned unit drop-in; see
[Secret delivery](SECRETS.md). The hourly timer
keeps rotations current and retains the last-known-good values if 1Password
is temporarily unavailable. To change a prod env var: edit the 1Password
item, then `systemctl start severino-hq-secrets.service` (or wait for the
timer; the container restarts only when something actually changed).

Provider credentials are separate from the app environment. Login items in the
same vault declare a stable `connection_ref`; `scripts/render-controller-env.sh`
discovers them through that field and renders
`/run/severino-hq-secrets/severino_controller_env` on tmpfs. The controller service
requires a root-owned 0700 directory and a root-owned 0400 file, separate from
the web-writable doorbell directory. `scripts/run-controller.sh` forwards the derived variables only
to a short-lived controller container running from the exact deployed HQ image.
The file is never mounted into the HQ web container. Provider variables enter
the controller container configuration and are visible to Docker administrators;
they do not enter the long-running web process. Provider
passwords are never copied into the app-environment item.

### `SEVERINO_SECRET_STORE_KEY`

One field on the app-environment item, and the only secret HQ holds rather
than reads. It seals a certificate the operator generated against the offline CA
and asked HQ to install: the leaf and its key, the same pair that would
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

What kind of thing it is comes from the env prefix: `ADGUARD_*` is AdGuard,
`PORTAINER_*` is Portainer, unless the item carries a `provider` field, which
overrides it. That field is what lets two of a kind coexist: `PORTAINER_HOME`
and `PORTAINER_CLOUD` are both `portainer`, and each resource says which it
uses. It is optional, so an existing vault keeps working untouched.

A connection only observes unless the item carries a `manages` field set to
`1`. HQ adopts what a sweep finds only through a connection that manages, so a
deployment that relies on sweeps adopting zones and records sets it on the
production Cloudflare DNS connection, and on any other connection it adopts
through. See `docs/DERIVED_FACTS.md`, Adoption.

On each pass the controller probes every connection it was handed and reports
what answered and what that thing can act on: the machines behind a Portainer,
the zones a DNS token may edit. HQ stores the report, not the credential, and
`/infrastructure/connections/` is that report. Every menu asking "which machine"
or "which domain" is derived from it, so registering a new VPS with Portainer is
the whole of making it a place HQ can deploy to.

OAuth probes exchange the injected client credential for a short-lived access
token, discard that token immediately, and report only safe connection health.
Neither the client secret nor the access token crosses the controller boundary.

#### Minting observer credentials

Two scripts mint read-only credentials on the operator's machine. Both take the
bootstrap credential from the environment, never from an argument. Without
`--print-secret` they print what they would create and create nothing, because
each provider shows a secret once. With it they create the credential, report
its id on stderr, and write only the secret to stdout, for piping into the
connection's item.

```sh
CLOUDFLARE_BOOTSTRAP_TOKEN=... scripts/mint-cloudflare-token.sh \
    --account <account-id> --days 90 --allow-ip 192.0.2.0/24 --print-secret
TAILSCALE_BOOTSTRAP_TOKEN=... scripts/mint-tailscale-client.sh --print-secret
```

- `mint-cloudflare-token.sh` needs a bootstrap token with API Tokens Write. It
  resolves the names in `scripts/cloudflare-observer-permissions.txt` to IDs
  through `GET /user/tokens/permission_groups` and creates a user-owned token
  (the probe verifies it at `/user/tokens/verify`) with an expiry and optional
  client IP ranges. A name Cloudflare does not list stops the mint;
  `--list-groups` prints the current names. The secret is the `cloudflare_api`
  item's `API_TOKEN`.
- `mint-tailscale-client.sh` creates an OAuth client (`keyType: client`) through
  `POST /tailnet/{tailnet}/keys`. The bootstrap credential needs the
  `oauth_keys` scope: an API access token, or an OAuth client given as
  `TAILSCALE_BOOTSTRAP_CLIENT_ID` and `TAILSCALE_BOOTSTRAP_CLIENT_SECRET`. Scopes
  come from `scripts/tailscale-observer-scopes.txt` and must all be `:read`.
  OAuth clients do not expire and take no IP restriction. The printed id is the
  item's `CLIENT_ID`, the secret its `CLIENT_SECRET`. The same scopes can be
  ticked under Trust credentials in the admin console instead. An observer
  client cannot approve routes or edit the policy; those need a separate
  credential with write scopes.

Both lists are the `requires` of every reading for that provider plus
`control_plane.credential_reads`, and a test fails when they differ.

The controller trusts internal provider TLS through the host trust store or a
deployment-provided `HQ_CONTROLLER_CA_FILE`. The internal CA certificate
is not stored in this public repository. Never disable TLS verification.

`hq sync` asks the Vault MCP for its complete validated manifest, then submits
it in one authenticated `hq.sync` Streamable HTTP MCP call over Tailscale. HQ
validates and commits it in one transaction. No intermediate payload is written
on example-host, and routine synchronization requires no SSH access. What HQ
holds about the infrastructure itself is not synchronized from anywhere: it is
swept, or declared in HQ.

The gated `main` deployment runs `scripts/install-controller.sh` after the new
application image is healthy. The installer refreshes controller-only
credentials, validates the systemd units, authenticates read-only to every
declared provider in plan mode, and only then enables the apply timer. Missing
credentials, untrusted TLS, and API failures stop activation. The HQ web
container never receives the provider environment.

What it installs is every unit and drop-in under `deploy/systemd`, found by
walking the directory (`scripts/lib/systemd-units.sh`); `*.example` templates
are copied into place by hand and never installed. Every shipped timer and path
is enabled. Adding a unit is adding its file: there is no list to update. The
daily `severino-hq-script-drift` check compares the same set, byte for byte,
with `/etc/systemd/system`, and names each file that differs. A drop-in the host
adds beside a shipped one is the host's and is not compared.

The same activation gate performs an authenticated pull of the live
`example.com` content index before installing and enabling its persistent
daily timer. Cloudflare Access credentials come from uppercase fields on the
existing the app-environment item item through the normal app-environment projection;
there is no second credential registry. A restart cannot lose the schedule:
systemd owns it, catches up missed runs, and the deployment revalidates the
pull before declaring the release healthy.

The controller claims only kind/action pairs a provider marks `apply`. Each
provider declares what may be done to it, and which of those may run
unprompted, beside its own definition. Self-contained controller adapters emit
that definition with their inventory, probe, and handlers; the admitted adapter
compiler refuses mismatched or duplicate surfaces at startup. Legacy providers
remain cross-checked against their handler table while they move through that
same seam. Its persistent systemd
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
`cloudflare-dns-example`; the controller resolves that reference through
`config/controller-connections.json`. The token is restricted to Zone Read and
DNS Edit for `example.com`, `example.net`, `example.org`, and
`example.test`. Controller activation verifies the token and proves all four
zones are readable without performing a DNS mutation.

Deployment identities are SSH key items in the controller's vault, generated
by 1Password, so no private key is ever created on or written to a host's disk.
Each SSH connection names its key item in an `identity` field.
`refresh-secrets.sh` renders the private half, the public half and a
`known_hosts` pinned from the connection's Ed25519 host key into the
controller's secret mount (a tmpfs mounted `noswap`, which the scripts check
with `findmnt` before rendering), refuses an item whose two halves do not
match, and installs the set with the controller environment as one generation
under an exclusive lock that readers take shared. It refuses to run while
private keys remain in the legacy `secrets/ssh/` directory on disk; the
operator removes those by hand. The web
container, the repository and the operator's workstation never hold them.
Rotating a key is generating a new item, authorizing its public half on the
target, and pointing the connection's `identity` at it. The same connection
registry emits each target's host, port and remote user;
`scripts/controller-ssh.sh` derives strict, batch-only, operation-allowlisted
SSH invocations from it. It does not accept arbitrary
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
once (the host's and each extension's) and whichever ran last won, so a host
release silently dropped every extension out of production.
`scripts/deploy-image.sh` stops reconciliation, records the currently running
image and the compose file it was started with, starts the replacement under the
compose file copied out of that verified image (so a compose change takes effect
in the same deploy), and restores both the previous image and its compose file
automatically if the exact SHA-tagged replacement does not become healthy or
its controller cannot pass activation. After rollback,
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

### A.5 Tailscale-only exposure: pick one

Two common patterns:

1. **Tailscale on the host, Caddy on the host**: install Tailscale on the
   homelab host, then run Caddy on the host listening on the host's Tailscale
   IP. Caddy proxies to `127.0.0.1:8000`. This is the simplest.

2. **Tailscale sidecar container**: run a `tailscale/tailscale` container in
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

> **`hq deploy` is legacy: do not run it.** It predates composition and
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

## Option B: systemd + Caddy/Nginx on a VPS

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
the Tailscale interface only: for example `100.x.y.z:443`:

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

- **502 from Caddy/Nginx**: the app isn't running on `127.0.0.1:8000`.
  Check `systemctl status severino-hq` or `docker compose logs app`.
- **CSRF errors after sign-in**: your `DJANGO_CSRF_TRUSTED_ORIGINS` doesn't
  include the full origin (scheme + host).
- **`SECRET_KEY must be set`**: the env file isn't being read by the unit.
  Check `EnvironmentFile=` and that the file is readable by the service user.
- **Receipt downloads 404**: `SEVERINO_MEDIA_ROOT` doesn't match where the
  file was originally written. Make sure the value is stable across restarts.
