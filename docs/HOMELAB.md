# Severino HQ — homelab deployment notes

This file documents how Severino HQ runs on **Severino Labs' homelab**
specifically. The generic deployment recipe is in
[`DEPLOYMENT.md`](DEPLOYMENT.md); this file captures the actual concrete
choices for our environment so future redeploys are mechanical.

> If you are setting this up somewhere else, ignore this file and follow
> `DEPLOYMENT.md`.

---

## Where it runs

| Property | Value |
|---|---|
| Host | `homelab-server` (Hyper-V VM, Ubuntu 26.04, LAN `192.168.1.233`, Tailscale `100.85.33.67`) |
| Install path | `/opt/apps/severino-hq/` (owned by `joe:joe`) |
| Container | `severino-hq` |
| Container bind | `0.0.0.0:8000` on the VM (NPM is bridge-networked and cannot reach host loopback; access control is the app's auth gate, not the bind address) |
| LAN-reachable URL | `https://hq.jseverino.com` (via Nginx Proxy Manager) |
| TLS | Local CA (Severino Labs Root CA), NOT Let's Encrypt — name resolves only inside the network |
| DNS | AdGuard Home rewrite `hq.jseverino.com → 192.168.1.233`. NOTE: any device that doesn't use AdGuard as its resolver (e.g. a Mac on Wi-Fi using ISP DNS) won't see this name. |
| Monitoring | Uptime Kuma HTTP(s) check on `https://hq.jseverino.com/accounts/login/` |
| Backups | `scripts/backup.sh` — `VACUUM INTO` then tar + optional age |

The container is **not** reachable from the LAN directly. The only way in
is the NPM HTTPS frontend on `hq.jseverino.com` (LAN + Tailscale).

---

## Pocket ID / SSO

HQ can sign users in through Pocket ID at `https://sso.jseverino.com`.
Password login at `/accounts/login/` remains the break-glass path.

Pocket ID OIDC client:

| Field | Value |
|---|---|
| Name | `Severino HQ` |
| Callback URL | `https://hq.jseverino.com/oidc/callback/` |
| Allowed group | `admins` |

HQ env:

```env
SEVERINO_OIDC_ENABLED=1
SEVERINO_OIDC_ISSUER=https://sso.jseverino.com
SEVERINO_OIDC_CLIENT_ID=<from Pocket ID>
SEVERINO_OIDC_CLIENT_SECRET=<from Pocket ID>
SEVERINO_OIDC_ALLOWED_GROUPS=admins
SEVERINO_OIDC_CREATE_USER=1
```

---

## SSH + deploy key on the server

The VM has its own ed25519 deploy key registered as **read-only** on the
GitHub repo. Joe's personal SSH keys are never present on the server.

| Path on VM | Purpose |
|---|---|
| `~/.ssh/severino-hq-deploy` | Private key — ed25519, no passphrase |
| `~/.ssh/severino-hq-deploy.pub` | Pubkey registered as read-only deploy key on GitHub |
| `~/.ssh/config` → `Host github.com-severino-hq` | Routes git through the deploy key automatically |

Clone URL on the server: `git@github.com-severino-hq:joeseverino/severino-hq.git`.

This means `git pull` from the server "just works" without env vars or
agent forwarding — and the key cannot push.

---

## .env on the server (`/opt/apps/severino-hq/.env`)

Owned by `joe`, `chmod 600`, not in git. Generated on first deploy:

```
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=<64-byte token_urlsafe>
DJANGO_ALLOWED_HOSTS=hq.jseverino.com,hq.jseverino.com,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://hq.jseverino.com,https://hq.jseverino.com
DJANGO_BEHIND_TLS_PROXY=1
DJANGO_SESSION_COOKIE_SECURE=1
DJANGO_CSRF_COOKIE_SECURE=1
DJANGO_HSTS_SECONDS=0
DJANGO_TIME_ZONE=America/New_York
SEVERINO_SITE_NAME=Severino HQ
SEVERINO_OIDC_ENABLED=1
SEVERINO_OIDC_ISSUER=https://sso.jseverino.com
SEVERINO_OIDC_CLIENT_ID=<from Pocket ID>
SEVERINO_OIDC_CLIENT_SECRET=<from Pocket ID>
SEVERINO_OIDC_ALLOWED_GROUPS=admins
SEVERINO_OIDC_CREATE_USER=1
```

Storage paths inside the container come from the image
(`/data`, `/media`, `/exports`, `/static`) and live in named Docker volumes.

---

## Routine ops

```bash
# Pull, rebuild, restart
ssh homelab-server "cd /opt/apps/severino-hq && git pull && sudo docker compose build && sudo docker compose up -d"

# Logs
ssh homelab-server "cd /opt/apps/severino-hq && sudo docker compose logs --tail=100 app"

# Interactive command — `ssh -t` is required for a TTY (createsuperuser,
# changepassword, shell, dbshell). Without -t Django silently skips with
# "Superuser creation skipped due to not running in a TTY".
ssh -t homelab-server "cd /opt/apps/severino-hq && sudo docker compose exec app python manage.py createsuperuser"

# Shell inside the container
ssh -t homelab-server "cd /opt/apps/severino-hq && sudo docker compose exec app python manage.py shell"

# Non-interactive one-off
ssh homelab-server "cd /opt/apps/severino-hq && sudo docker compose exec app python manage.py <command>"

# Piping stdin in — use -T on docker exec, plain ssh
ssh homelab-server "cd /opt/apps/severino-hq && sudo docker compose exec -T app python manage.py import_docs_manifest -" \
  < /path/to/local/docs_manifest.json

# Container health
ssh homelab-server "sudo docker inspect --format '{{.State.Health.Status}}' severino-hq"
```

---

## Wiring up the NPM frontend (one-time, after first deploy)

The vault has runbooks for this — these are the manual UI steps in NPM:

1. AdGuard: add a DNS rewrite `hq.jseverino.com → 192.168.1.233` (the
   `*.homelab` wildcard does NOT cover `.jseverino.com`).
2. Cert: reuse the existing `*.jseverino.com` wildcard already loaded in
   NPM — no `cert-gen` for this hostname.
3. NPM → Hosts → Proxy Hosts → Add Proxy Host:
   - Domain Names: `hq.jseverino.com`
   - Scheme: `http`
   - **Forward Hostname / IP: `192.168.1.233`** (NPM is bridge-networked;
     `127.0.0.1` here is NPM's own container loopback and returns 502)
   - Forward Port: `8000`
   - SSL tab → pick the `*.jseverino.com` cert, Force SSL on, HTTP/2 on.
4. Save. Verify with `curl -kI --resolve hq.jseverino.com:443:192.168.1.233
   https://hq.jseverino.com/accounts/login/` — expect `HTTP/2 200`.

---

## Backups

`scripts/backup.sh` defaults to host bind-mount paths (`/srv/...`). Since
this deploy uses Docker named volumes, the practical backup is one of:

- Run `VACUUM INTO` inside the container, write to the `/exports` volume,
  then tar the named volumes from the host.
- Or run the script inside a sidecar container that has the named volumes
  mounted into the same paths the script expects.

A nightly systemd timer fires `scripts/backup.sh` against the mounts; the
unit file template is in [`BACKUP.md`](BACKUP.md).

---

## Troubleshooting

### NPM returns 502 for `hq.jseverino.com`

NPM runs in a bridge-networked container. From inside it, `127.0.0.1` is its
own loopback, not the host's. Set the NPM proxy host's **Forward Hostname /
IP** to `192.168.1.233` (the VM's LAN IP), not `127.0.0.1`.

### `192.168.1.233:8000` from a browser returns 400 "Disallowed Host"

That's correct — Django rejects requests whose `Host` header isn't in
`DJANGO_ALLOWED_HOSTS`. The intended path is through NPM, which rewrites the
`Host` header to `hq.jseverino.com`. If you really need to hit the backend
directly for debugging, add `192.168.1.233` to `DJANGO_ALLOWED_HOSTS` in
`.env` and recreate the container — but don't leave it there.

### `https://hq.jseverino.com` won't resolve on my Mac

Your Mac probably isn't using AdGuard as its DNS resolver. The Spectrum
router hands out its own IP for DHCP regardless, and Tailscale's "Use
nameservers globally" must be on (with `100.85.33.67` as the nameserver) for
tailnet devices to see the rewrite. Confirm with:

```bash
scutil --dns | grep 'nameserver\[0\]' | head -3
dig +short hq.jseverino.com
```

For a quick one-off test, you can curl with a forced resolve:

```bash
curl -kI --resolve hq.jseverino.com:443:192.168.1.233 https://hq.jseverino.com/accounts/login/
```

### CSRF errors after sign-in via NPM

`DJANGO_CSRF_TRUSTED_ORIGINS` doesn't list the full origin the browser is
using. It must be scheme + host with no path — e.g.
`https://hq.jseverino.com`, not `hq.jseverino.com` or
`https://hq.jseverino.com/`.

---

## Rollback

```bash
# Stop, keep data
ssh homelab-server "cd /opt/apps/severino-hq && sudo docker compose down"

# Stop, DESTROY volumes — only after a known-good backup
ssh homelab-server "cd /opt/apps/severino-hq && sudo docker compose down -v"
```

---

## Source-of-truth links

Inside the Severino Labs Obsidian vault:

- `02 Infrastructure/Severino HQ/Severino HQ.md` — full service doc.
- `02 Infrastructure/Homelab Server/Homelab Server.md` — VM-level context.
- `03 Runbooks/Deploy Severino HQ.md` — fresh-deploy procedure.
- `03 Runbooks/Add Nginx Proxy Host.md` — NPM wiring.
- `03 Runbooks/Generate Homelab Certificate.md` — local cert.
- `03 Runbooks/Update Vault After Container Change.md` — bookkeeping after
  any container add / remove / rename.
- `CLAUDE.md` (vault root) — first thing Claude reads; container tables.
