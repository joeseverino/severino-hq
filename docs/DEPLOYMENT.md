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
uses. It is optional: without it, the prefix decides.

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

Each release installs itself. Root runs units out of
`/usr/local/lib/severino-hq`, a root-owned copy of `scripts/`, `config/`,
`deploy/` and `docker-compose.yml` taken from the running image. Once the new
image is healthy, `deploy-image.sh` runs the `severino-hq-sync-scripts` that
image ships (never the host's copy), which replaces the tree and refuses it
unless it reproduces `root-tree.sha256` exactly: the manifest the image build
writes with `scripts/root-tree-manifest.sh`. It then installs that sync program
at `/usr/local/sbin` and runs the installer it just synced, so every step after
the sync is the new release's code. Run by hand, `install-controller.sh` syncs
and re-executes its synced copy the same way. A failed activation restores the
previous tree and sync program exactly. `fix-root-ownership.sh` is the first
bring-up, after `sh scripts/severino-hq-sync-scripts --from-checkout`.

The deploy job refuses before its `git pull` when anything under the checkout's
`.git` is not owned by the runner, and names the `chown` that fixes it. The
daily `severino-hq-script-drift` check asks the same, and also fails when the
tree no longer reproduces its manifest or the sync program is not the tree's.

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
app-environment item through the normal app-environment projection;
there is no second credential registry. A restart cannot lose the schedule:
systemd owns it, catches up missed runs, and the deployment revalidates the
pull before declaring the release healthy.

The controller claims only kind/action pairs a provider marks `apply`. Each
provider declares what may be done to it, and which of those may run
unprompted, beside its own definition. Self-contained controller adapters emit
that definition with their inventory, probe, and handlers; the admitted adapter
compiler refuses mismatched or duplicate surfaces at startup. Providers the
controller core implements directly are cross-checked against their handler
table. Its persistent systemd
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
private keys remain in a `secrets/ssh/` directory on disk; the
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
extension onto the new host image, and deploys that. With one path, a host
release cannot reach production without its extensions.
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

The host and composed images run Python 3.14. The host installs its locked
dependencies in a build stage; composition installs hash-verified extension
wheels in a separate installer stage. Neither production image contains pip or
uv.

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

The live homelab updates through workflows that each start the next, naming
the commit, so nothing waits on a schedule and no run of one workflow can be
mistaken for another's:

| Workflow | On | Does |
|---|---|---|
| **CI** (`ci.yml`) | every pull request and push to `main` | Checks, Tests, Browser and Image (build, prove healthy, scan, publish and sign the host image), and **Ready**, which writes HQ's review |
| **CodeQL** (`codeql.yml`) | every pull request and push to `main`, and weekly | GitHub's code scanning; the ruleset holds a merge while it has an alert |
| **Compose** (`compose.yml`) | a pull request (to verify it); started with a commit by CI once a push to `main` passes, by an extension's admission, or by hand | the host plus every admitted extension, verified as one application; on `main`, published and signed |
| **Deploy** (`deploy.yml`) | started with a commit by Compose once it has published HQ, or by hand to redeploy or roll back | waits for approval in `production`, then deploys on the self-hosted runner with health rollback |

Production runs the composed image (`…/composition:…`), never the host image
on its own. Migrations and `collectstatic` run on container boot via
`entrypoint.sh`. A pull request never starts Deploy, so no pull request's code
reaches the self-hosted runner. To redeploy or roll back, run **Deploy** with
the commit to put back.

HQ's app says where every change is, in one check posted by whatever just did
the work, whose details link to the run behind it:

- **Severino HQ · Review** on a pull request: *Checking* from CI's first
  seconds, then, once every gate, CodeQL and the build of HQ have finished,
  *Ready to merge*, or *Held*, why and what fixes it, with every gate's
  result, time and link below. It is the one check the ruleset requires, and
  only HQ's app can post it.
- **Severino HQ · Production** on a commit to `main`: *Building HQ* while
  Compose builds it, *Waiting for approval* once Deploy has found the signed
  image, then *Live in production*, or
  *Not deployed*, why and what fixes it, and one comment on the merged pull
  request. Report runs on a hosted runner, so HQ's key never reaches the
  homelab one.
- **Severino HQ · Production** on an extension's commit: its admission posts
  *Waiting for its composition and approval* in its own repository, and HQ's
  controller marks it live once production runs that commit, when its
  connection manages `github.delivery`.

Why a run failed comes from `deploy/diagnoses.json`: `scripts/diagnose.py`
matches the failed job's log against it and prints only the catalog's own
words, never a line of the log. The first time a failure needs investigating,
add it there, and it is named, with its fix, every time after. What any of
these checks says is public: the commit, the stage, the image and the run. No
machine and no extension is named in this repository.

#### Continuous delivery through HQ's GitHub App

One app, registered on your account with no webhook and the permissions in
`deploy/github-apps.json` (a test holds them to exactly what HQ asks for), and
installed on the host repository and every extension. Each use mints an
hour-long token for only what it does:

| Who | Holds the key as | Asks for |
|---|---|---|
| the controller | a 1Password SSH Key item, rendered for openssl only | what each read or report needs |
| an extension's admission | `HQ_APP_KEY` on its `admission` environment, main only | Actions write on this repository: start the composition |
| the composition | `HQ_APP_KEY` in this repository's Actions secrets | Actions and Contents read: the extensions' admissions |

For the controller, keep the key as an SSH Key item in the controller vault,
and beside it a connection item with `connection_ref`, `projection: github_app`,
`env_prefix: GITHUB`, `app_id` and `signing_key` (the SSH Key item's title),
**without** `manages`. The key is rendered beside the controller's SSH
identities and read only by openssl, which signs GitHub's JWT; HQ never loads
it. The connection's probe shows the key's fingerprint as GitHub lists it.

For the pipeline, `scripts/wire-github-app.py` reads the key from 1Password and
sets the `HQ_APP_KEY` and `HQ_APP_CLIENT_ID` secrets on this repository, and on
each extension creates the `admission` environment (main only) with the key and
sets the client ID, through standard input, never a file or a command line. The
client ID is a secret too: not because it is sensitive, but because a variable
is state outside the repository that changes what a build does. Each extension's `admit-plugin.yml` caller
names the environment on its admit job and passes both to the host's action.

A rotation: generate a second key on the app, replace the SSH Key item's key,
run the script again, wait for the probe to show the new fingerprint, delete
the old key on GitHub.

Read before writing. Until the connection item has `manages: 1`, HQ observes
only: the sweep reports each extension's admitted and running commit and the
composition run that carries it, and writes nothing. Once that matches GitHub,
set `manages` to `1` and adopt the `github.delivery` record.

> **Do not run `hq deploy`.** It deploys the *host-only* image, which takes
> every extension off production until the next composition. To rebuild by
> hand, run **Compose**; to redeploy or roll back, run **Deploy** with the
> commit you want.

The equivalent **manual** steps, for a standalone or first-time deploy, are:

```bash
git pull
docker compose build
docker compose run --rm app python manage.py migrate
docker compose run --rm app python manage.py collectstatic --noinput
docker compose up -d
```

#### Container upgrades

`scripts/upgrade-container.sh` is the program an upgrade plan describes. It
runs as root for another account, so it trusts nothing that account passes it
beyond naming the upgrade:

- The stack must be a directory directly under `/opt/apps` (a constant in the
  helper; through sudo nothing can move it), not a symlink. The directory, its
  compose file, any override and any `.env` must be owned by root or by the
  owner of `/opt/apps`, never by the calling account, and writable by no one
  else. Compose runs with the file set it would use by default (the compose
  file plus its override) and `-p` named from the directory.
- `--from` must be the image `docker compose config` resolves for the service
  and what its one running container runs; more than one container is refused.
  `--to` must be the same repository, pinned by digest.
- The data is the running container's writable mounts, read from Docker:
  volumes, and directories inside the stack. A writable directory mount outside
  the stack is refused. There is no `--data` argument.

For one compose service, it:

1. Pulls the target by digest.
2. Stops the service and snapshots its data, each to a temporary file renamed
   only when whole. A failed snapshot starts the service again unchanged.
3. Runs the target from the service's own compose definition
   (`compose run --no-deps`, no network, no published ports) against a copy of
   the data, until it proves itself. If it does not, the service is started
   again unchanged.
4. Pins the digest in the stack's compose override
   (`docker-compose.override.yml`, or the override the stack already has),
   keeping every other line of it and a byte-exact copy. The compose file
   itself is never edited.
5. Recreates the service and verifies it: running, healthy or with no health
   check, and not restarted, for a settle window. A restart starts the window
   again, so a crash loop never verifies.
6. Otherwise, restores the override and the data and recreates the service.

An error or a signal after the service is stopped puts it back the same way and
still records a result; a full disk while restoring data is reported as such,
and the snapshot is kept. It is idempotent by operation id and prints one JSON
result: exit 0 kept, 3 rolled back, 2 refused or unchanged, 1 failed and not put
back. It needs Docker Compose 2.24.4 or later. `scripts/test-upgrade-container.sh`
drills it against a stand-in Docker, on Linux and macOS.

Every deploy syncs it root-owned to `/usr/local/lib/severino-hq/scripts/` on the
machine HQ runs on, and `scripts/preflight.sh` checks that copy is root's, under
directories only root can write, and identical to the release. Any other
machine needs its own copy: the container's upgrade plan gives the steps (this
build's commit, installed `root:root 0755`, checked against the digest of the
copy HQ ships).

A machine runs it only through a sudo rule for that one program with only an
upgrade's arguments, written as a regular expression (sudo 1.9.10 or later; an
older sudo reads it literally and admits nothing). The plan shows the exact
rule, and how to check it, for as long as it is missing. HQ does not know which
account the controller signs in as on a machine, so the rule's step names a
placeholder and refuses to run until it is set.

The rule is only worth having if that account cannot already reach Docker: an
account in the `docker` group, or able to write to its socket, is root on that
machine with or without the helper. Under sudo the helper sets its own `PATH`
and `HOME` and ignores `DOCKER_CONFIG`, `DOCKER_HOST` and `DOCKER_CONTEXT`, so a
sudo that keeps the caller's environment still runs root's Docker CLI and
plugins, never the caller's. It snapshots and restores writable volumes and the
directories and files the stack mounts from inside its own folder; a service
writing to anything outside it is refused and upgraded by hand. The pin goes in
the override Compose loads beside the compose file (`compose.yaml` beside
`compose.override.yaml`), and any other override name is refused rather than
written to, since a plain `docker compose up` would never read it.

HQ does not yet queue an
upgrade to the machine a container runs on, because operations are claimed by
capability rather than by machine. Until then the helper is run by hand, with
the plan's values.

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

The deploy checks the three host paths the web container binds before compose
reads them. The checkout's `.env` is writable by the deploy account, so none of
them is taken on trust:
- The app environment is not read from `.env` at all. `refresh-secrets.sh`
  renders it to `/run/severino-hq-secrets/web/severino_hq_env`, on the noswap
  tmpfs, in a directory only root can enter, as a single-link regular file
  owned by the web user. The deploy binds that file and nothing else; a copy
  left in the checkout's `secrets/` is removed once a deploy is healthy.
- `SEVERINO_CONTROLLER_RUN_DIR` must be `/run/severino-hq`.
- `SEVERINO_CONTROLLER_CA_FILE_HOST` must be a certificate under
  `/usr/local/share/ca-certificates/`.

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
