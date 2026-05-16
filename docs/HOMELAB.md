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
| Container bind | `127.0.0.1:8000` (loopback only on the VM) |
| LAN-reachable URL | `https://severino-hq.homelab` (provided by Nginx Proxy Manager) |
| TLS | Local CA (Severino Labs Root CA), NOT Let's Encrypt — `.homelab` is internal |
| DNS | AdGuard Home wildcard `*.homelab → 192.168.1.233` |
| Monitoring | Uptime Kuma HTTP(s) check on `https://severino-hq.homelab/accounts/login/` |
| Backups | `scripts/backup.sh` — `VACUUM INTO` then tar + optional age |

The container is **not** reachable from the LAN directly. The only way in
is the NPM HTTPS frontend on `severino-hq.homelab` (LAN + Tailscale).

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
DJANGO_ALLOWED_HOSTS=severino-hq.homelab,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://severino-hq.homelab
DJANGO_BEHIND_TLS_PROXY=1
DJANGO_SESSION_COOKIE_SECURE=1
DJANGO_CSRF_COOKIE_SECURE=1
DJANGO_HSTS_SECONDS=0
DJANGO_TIME_ZONE=America/New_York
SEVERINO_SITE_NAME=Severino HQ
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

# Shell inside the container
ssh homelab-server "cd /opt/apps/severino-hq && sudo docker compose exec app python manage.py shell"

# One-off Django command
ssh homelab-server "cd /opt/apps/severino-hq && sudo docker compose exec app python manage.py <command>"

# Container health
ssh homelab-server "sudo docker inspect --format '{{.State.Health.Status}}' severino-hq"
```

---

## Wiring up the NPM frontend (one-time, after first deploy)

The vault has runbooks for this — these are the manual UI steps in NPM:

1. `cert-gen severino-hq.homelab` on the Mac → produces
   `severino-hq.homelab.key` + `fullchain.pem`.
2. NPM → Certificates → Add Certificate → Custom → upload both files.
3. NPM → Hosts → Proxy Hosts → Add Proxy Host:
   - Domain Names: `severino-hq.homelab`
   - Scheme: `http`
   - Forward Hostname/IP: `127.0.0.1`
   - Forward Port: `8000`
   - SSL: select the cert above, Force SSL on, HTTP/2 on.
4. Verify: `curl -sI https://severino-hq.homelab/accounts/login/` returns
   `HTTP/2 200`.

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
