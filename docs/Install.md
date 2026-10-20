# Install

From a Linux host with Docker to HQ up and the controller reading its first
connection. Each step says what it is for, what to do, and how to check it
worked. Do them in order: later steps read what earlier ones made.

Why each piece is shaped the way it is lives in [Deployment](Deployment.md)
and [Secrets](Secrets.md). This page is only the order and the commands.

## Before you start

### The host

- Linux with systemd and Docker. systemd must support `noswap` on a tmpfs
  (the credential mount uses it).
- `net.ipv4.ip_unprivileged_port_start` above the port you give Connect. The
  default, 1024, is fine.
- On a tailnet. HQ answers the tailnet and loopback and refuses everything
  else ([Security](Security.md)).
- A reverse proxy on the same host that terminates TLS and forwards to
  `127.0.0.1:8000` ([Network exposure](Deployment.md#network-exposure)).

### Accounts

- **GitHub.** Your own copy of this repository. A host deploys only images its
  own repository's Compose workflow signed, so you cannot run someone else's.
- **1Password**, with Secrets Automation, for one vault that holds every
  credential HQ reads and a Connect server that reads it.
- **Extensions, if you want any.** HQ runs without them; an empty list
  composes the host alone (see [step 2](#2-set-up-the-repository)).
- **A provider HQ can read**, such as Tailscale, Cloudflare, Portainer or
  your proxy, when you want HQ to read one. HQ starts without any
  ([step 8](#8-add-the-first-connection)).

Run every host command below in a root shell (`sudo -i`).

## 1. Copy the repository

**For:** the images your host runs are built and signed by your repository.

Fork or copy this repository to your account. Then change what names this
instance: [Blueprint Setup](Blueprint-Setup.md) lists every value, and
section 3 there the text that has to be edited by hand.

**Check:** CI runs on your copy's pull requests.

## 2. Set up the repository

**For:** CI, Compose and Deploy need an app, secrets, an environment and a
ruleset before they can publish anything.

1. Register HQ's GitHub App with no webhook and the permissions in
   [`deploy/github-apps.json`](../deploy/github-apps.json). Install it on
   this repository and each extension.
2. Store the app's private key in 1Password, then set the secrets with
   `scripts/wire-github-app.py`; name each extension with `--extension`, or
   none. It needs `gh` and `op` signed in:

   ```sh
   scripts/wire-github-app.py --vault 'Example Vault' --item 'example github app' --client-id EXAMPLE_CLIENT_ID --extension example-owner/example-extension
   ```

3. Set the `COMPOSITION_EXTENSIONS` repository secret: JSON in the shape of
   [`composition/extensions.json`](../composition/extensions.json), listing
   the extensions to compose; `{"extensions": []}` composes the host alone.
   To take one out of production later, move its id to `retired`: a deploy
   refuses an image that drops an extension production runs unless it is named
   there. How an extension is admitted is in [Plugins](Plugins.md#composition).
4. Create the `production` environment with yourself as a required reviewer.
5. Add a `main` ruleset requiring the **Severino HQ · Review** check.

**Check:** the repository's secrets list `HQ_APP_KEY`, `HQ_APP_CLIENT_ID` and
`COMPOSITION_EXTENSIONS`.

## 3. Publish a composition

**For:** the host runs the composed image your repository publishes, pinned by
digest. For this repository that is `ghcr.io/joeseverino/severino-hq/composition`.

Merge to `main`. CI's **Release** starts **Compose**, which publishes and signs
the composition. To run it by hand: Actions, **Compose**, Run workflow, on
`main`.

Compose then starts **Deploy**, which waits for your approval. The host is not
ready for it yet: reject it.

Find the digest of what Compose published. Either read it on the package's
page on GitHub, or, from any machine with Docker Buildx:

```sh
docker buildx imagetools inspect ghcr.io/joeseverino/severino-hq/composition:COMMIT_SHA
```

**Check:** you have a reference of the form
`ghcr.io/joeseverino/severino-hq/composition@sha256:<64 hex>`, with your
repository's owner and name in place of these, lowercase, as GHCR names them.

## 4. Run 1Password Connect

**For:** the renderer reads every secret through a Connect server on the
host's loopback, and nothing else.

HQ does not install Connect. Follow 1Password's guide,
[Get started with a 1Password Connect server](https://www.1password.dev/connect/get-started),
to create the Secrets Automation workflow, its `1password-credentials.json`
and a token that can read only HQ's vault. Keep recovery copies in a vault
this Connect cannot read.

HQ needs three things of it that the vendor's example does not do:

- **IPv4 loopback only, on a port below 1024.** The vendor's example
  publishes 8080, which any account can bind; the renderer refuses it.
  Publish the API on `127.0.0.1` at a privileged port, for example
  `127.0.0.1:880`. Why is in [Secrets](Secrets.md#network-and-execution-boundaries).
- **A systemd service unit** that starts Connect. The renderer `Requires=` it
  by name (`connect.unit` in step 6). HQ ships no unit for it.
- Nothing else in front of it: no proxy, no Tailscale Serve, no LAN or
  tailnet listener.

**Check:** Connect answers on loopback, and the unprivileged port floor is
above its port:

```sh
curl -fsS http://127.0.0.1:880/health
```

```sh
sysctl net.ipv4.ip_unprivileged_port_start
```

## 5. Store the reader token as an encrypted credential

**For:** only the root renderer holds the token, and it never appears in an
argument, an environment variable or a file in plain text.

Pick a credential name, here `op_connect_example`. Encrypt the token from
standard input, so it reaches no command line or shell history. Paste it,
press Enter, then Ctrl-D:

```sh
install -d -o root -g root -m 0700 /etc/credstore.encrypted
```

```sh
systemd-creds encrypt --name=op_connect_example - /etc/credstore.encrypted/op_connect_example
```

```sh
chmod 0600 /etc/credstore.encrypted/op_connect_example
```

The name embedded in the credential must equal `connect.credential` in
`host.json`. Use TPM-backed encryption where the host has a TPM
([Secrets](Secrets.md#bootstrap-and-ownership)).

**Check:** it decrypts under that name. This prints a byte count, not the
token:

```sh
systemd-creds decrypt --name=op_connect_example /etc/credstore.encrypted/op_connect_example - | wc -c
```

## 6. Install the runner and write host.json

**For:** Deploy runs on a self-hosted runner on this host, and `host.json` is
everything about this host the repository does not hold.

1. Add a self-hosted runner to your repository with the label `severino-hq`,
   following GitHub's
   [Adding self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/adding-self-hosted-runners),
   and install it as a service. Run it as an unprivileged account that is not
   in the `docker` group.
2. Write `/etc/severino-hq/host.json` from
   [`deploy/host.example.json`](../deploy/host.example.json):

   | Field | Set it to |
   | :--- | :--- |
   | `vault` | the 1Password vault HQ reads |
   | `env_item` | the title of the application-environment item ([step 7](#7-create-the-application-environment)) |
   | `connect.unit` | Connect's service unit, from step 4 |
   | `connect.url` | `http://127.0.0.1:PORT`, the port below 1024 |
   | `connect.credential` | the credential name from step 5 |
   | `runner.unit` | the runner's service unit |
   | `runner.user` | the runner's account; never `root` |

   An unknown field, a non-loopback URL, or a value with quotes, `%`, `$` or a
   backslash is refused.

**Check:** the runner shows as idle under the repository's Actions runners,
and the file parses:

```sh
python3 -m json.tool /etc/severino-hq/host.json
```

## 7. Create the application environment

**For:** this one vault item becomes the web container's environment.

Create an item in the vault, titled as `env_item` says. Add one field per
variable: the field's label is the variable's name, its value the value.
Only labels in UPPER_SNAKE_CASE with a non-empty value are rendered; anything
else on the item is ignored. A Secure Note with custom fields works.

Every name is read in [`hq/config/settings.py`](../hq/config/settings.py);
[`.env.example`](../.env.example) has the longer notes.

**Required.** HQ will not start or cannot serve without these. The renderer
refuses an item that lacks one, or holds it empty, and names each one missing
(`REQUIRED_APP_VARIABLES` in `hq/config/settings.py`):

| Variable | For |
| :--- | :--- |
| `DJANGO_SECRET_KEY` | signs sessions; HQ will not start without it. Generate a long random value in 1Password. |
| `DJANGO_ALLOWED_HOSTS` | every hostname browsers and clients use, comma-separated; anything else gets 400 |

**Behind a TLS proxy.** The renderer does not require these, but sign-in
through a proxy does not work without them:

| Variable | For |
| :--- | :--- |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | each full origin the browser uses, such as `https://hq.example.com`; without it sign-in fails |
| `DJANGO_BEHIND_TLS_PROXY` | `1`: trust the proxy's `X-Forwarded-Proto` and redirect plain HTTP |

**Set on a first install.** Each has a default; setting it records this host's choice:

| Variable | For |
| :--- | :--- |
| `DJANGO_DEBUG` | `0` in production |
| `DJANGO_SESSION_COOKIE_SECURE` | `1`: the session cookie only over HTTPS |
| `DJANGO_CSRF_COOKIE_SECURE` | `1`: the CSRF cookie only over HTTPS |
| `DJANGO_HSTS_SECONDS` | how long browsers insist on HTTPS; `31536000` is a year |
| `DJANGO_HSTS_INCLUDE_SUBDOMAINS` | `1` or `0`: whether HSTS covers subdomains |
| `DJANGO_HSTS_PRELOAD` | `0` for a name the public internet cannot resolve |
| `DJANGO_TIME_ZONE` | your time zone, such as `America/New_York`; the default is `America/Chicago` |
| `SEVERINO_SITE_NAME` | the name in page titles and the header |
| `SEVERINO_TRUSTED_PROXIES` | the proxy's exact address, such as `127.0.0.1/32`; the only source whose `X-Forwarded-For` HQ believes |
| `SEVERINO_SECRET_STORE_KEY` | 32 characters or more; seals certificates you ask HQ to install ([Secrets](Secrets.md#severino_secret_store_key)) |
| `SEVERINO_OIDC_ENABLED` | `0` to start with a password; `1` once sign-in is set up below |

**Optional.** Set when you use the feature:

| Variable | For |
| :--- | :--- |
| `SEVERINO_OIDC_ISSUER`, `SEVERINO_OIDC_CLIENT_ID`, `SEVERINO_OIDC_CLIENT_SECRET`, `SEVERINO_OIDC_ALLOWED_GROUPS` | sign-in through your identity provider; turning it on closes the password form |
| `SEVERINO_API_RESOURCE` | the identity provider's API audience; empty disables the machine API and MCP |
| `SEVERINO_MCP_ALLOWED_HOSTS` | the host names MCP clients connect to; empty disables MCP |
| `SEVERINO_MCP_ENABLE_*` | MCP write switches, all off by default |
| `SEVERINO_TRUSTED_NETWORKS` | who may reach HQ at all; the default is loopback and Tailscale's ranges |
| `CONTENT_INDEX_URL` | a public site's content index to sync; unset turns the sync off |
| `CLOUDFLARE_D1_DATABASE_NAME` | which D1 database is the contact inbox, when more than one holds the contact table; the inbox itself is read and written through the Cloudflare API connection |

The image sets the storage paths (`SEVERINO_DATABASE_PATH`,
`SEVERINO_MEDIA_ROOT`, `SEVERINO_EXPORTS_ROOT`, `DJANGO_STATIC_ROOT`) and the
composition sets `SEVERINO_HQ_PLUGINS`. A real environment variable wins over
the rendered file, so setting those on the item changes nothing.

**Check:** the item has `DJANGO_SECRET_KEY` and `DJANGO_ALLOWED_HOSTS` with
values, and no name appears twice.

## 8. Add the first connection

**For:** what HQ reads. This step is optional: HQ starts without a connection,
and you can add one now or later from the Connections page. Once the host has a connection, a vault that holds none is refused and the installed connections are kept ([Secrets](Secrets.md#renderer-behavior)).

A connection is a vault item with three text fields the renderer reads:
`connection_ref` (a name you choose), `projection` (the shape of the
credential) and `env_prefix` (which provider it is for), plus the fields its
shape needs. Every provider's item, field by field, is in
[Connections](Connections.md); the rules are in [Secrets](Secrets.md#connections).

Tailscale is a natural first, because HQ already needs a tailnet. In the
Tailscale admin console, create an OAuth client with the read scopes in
[`scripts/tailscale-observer-scopes.txt`](../scripts/tailscale-observer-scopes.txt).
Then create an **API Credential** item in the vault:

| Field | Value |
| :--- | :--- |
| username | the OAuth client's ID |
| credential | the OAuth client's secret |
| `connection_ref` | `tailscale` |
| `projection` | `oauth_client` |
| `env_prefix` | `TAILSCALE` |

Leave `manages` off. Without it the connection only reads.

**Check:** the item is in the vault `host.json` names, not another one.
If you skip this step, step 11 has nothing to show until you add one.

## 9. Install HQ on the host

**For:** puts the root-owned tree, the renderer and the web unit in place and
starts them. Every later release arrives through Deploy.

Name the image once for this shell, with the reference from step 3:

```sh
export IMAGE='ghcr.io/joeseverino/severino-hq/composition@sha256:DIGEST'
```

Verify its signature against your Compose workflow on `main`. Run this here
or on any machine with [cosign](https://docs.sigstore.dev/cosign/system_config/installation/):

```sh
cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity-regexp '^https://github\.com/joeseverino/severino-hq/\.github/workflows/compose\.yml@refs/heads/main$' "$IMAGE"
```

Pull it. If the package is private, `docker login ghcr.io` first.

```sh
docker pull "$IMAGE"
```

Copy `hq-host` out of the image and let it install the tree:

```sh
container=$(docker create --pull never "$IMAGE" true)
```

```sh
docker cp "$container:/app/deploy/bin/hq-host" /root/hq-host
```

```sh
docker rm "$container"
```

```sh
/root/hq-host sync "$IMAGE"
```

```sh
rm /root/hq-host
```

Render the drop-ins and the runner's sudo rule from `host.json`:

```sh
/usr/local/lib/severino-hq/deploy/bin/hq-host bootstrap
```

Record the release and make the directories the units expect:

```sh
install -d -m 0755 /var/lib/severino-hq
```

```sh
printf '%s\n' "$IMAGE" > /var/lib/severino-hq/release
```

```sh
install -d -o root -g root -m 0700 /opt/apps/severino-hq/secrets
```

```sh
systemd-tmpfiles --create /usr/local/lib/severino-hq/deploy/tmpfiles/severino-hq.conf
```

Install the credential mount, the renderer and the web unit, then start them:

```sh
install -m 0644 '/usr/local/lib/severino-hq/deploy/systemd/run-severino\x2dhq\x2dsecrets.mount' /usr/local/lib/severino-hq/deploy/systemd/severino-hq-secrets.service /usr/local/lib/severino-hq/deploy/systemd/severino-hq-web.service /etc/systemd/system/
```

```sh
systemctl daemon-reload
```

```sh
systemctl start 'run-severino\x2dhq\x2dsecrets.mount'
```

```sh
systemctl start severino-hq-secrets.service
```

```sh
systemctl start severino-hq-web.service
```

The web unit waits for the rendered environment, then returns once HQ is
healthy.

**Check:** the render succeeded and HQ answers:

```sh
cat /run/severino-hq-secrets/status.json
```

`last_attempt.outcome` is `rendered` or `current`, and `counts.connections` is
at least 1. The document holds no secret.

```sh
curl -fsS http://127.0.0.1:8000/health/ready/
```

Then install and enable everything else: the controller, the timers, the
backup and root's own cosign.

```sh
/usr/local/lib/severino-hq/deploy/bin/hq-host install
```

```sh
/usr/local/lib/severino-hq/deploy/bin/hq-host verify
```

`verify` prints that the host is its release.

## 10. Sign in

**For:** HQ has no registration. The first account is made on the host.

```sh
docker exec -it severino-hq python manage.py createsuperuser
```

Open HQ through your proxy from a device on the tailnet, such as
`https://hq.example.com`, and sign in with that account.

**Check:** you reach the dashboard, and the audit log records the sign-in.

To move to single sign-on later, set the `SEVERINO_OIDC_*` variables and
`SEVERINO_OIDC_ENABLED=1` on the item; the next render restarts HQ on them.

### Your GitHub account

Starred repos reads the GitHub account your sign-on names. Add a claim named
`github` holding your GitHub login to your user at the identity provider, then
sign in again.

## 11. The controller's first reading

**For:** the controller holds the credentials and reads what each can see.
HQ shows what it reported.

`hq-host install` enabled the controller's timer, so a pass runs within a
minute or two. To run one now:

```sh
systemctl start severino-hq-controller.service
```

**Check:** the journal shows the pass, and HQ shows the connection, if you
added one in step 8:

```sh
journalctl -u severino-hq-controller.service -n 50
```

Open **Connections** (`/infrastructure/connections/`). Your connection is
listed with what answered and what it can see.

HQ is installed.

## Where connections fit

A connection is a vault item and nothing else. Nothing in this repository or
in HQ's database holds the credential; HQ stores what the controller reported
about it.

HQ's Connections page lists every provider HQ can read. For one not yet
connected, it says what token to create, the permissions it needs and the
fields to put on its vault item. Add the item, and it is read at the next
render (hourly, or at once with `systemctl start severino-hq-secrets.service`)
and probed at the controller's next pass. The same, for every provider, is in
[Connections](Connections.md).

A connection only reads until its item carries `manages` set to `1`. Leave it
off until you want HQ to change things through it
([Derived Facts](Derived-Facts.md#adoption)).

## What happens next

- **The controller's pass.** On each pass the controller probes every
  connection, takes its readings and applies any queued work. It runs when
  HQ has something for it: at once when work is queued or a reading is asked
  for, and when a sweep comes due. Otherwise it runs 15 minutes after the last
  pass ends.
- **Get started.** The dashboard lists what a new HQ still needs: run the
  controller, add a connection, choose a machine for the dashboard. Each step
  goes away once HQ can see it done.
- **Problems.** Anything that needs you, such as a failing render, a refused
  credential or a missing permission, is a finding. It appears under
  **Needs you** (`/action-items/`, and the count in the header) and in full
  on **Findings** (`/infrastructure/findings/`), each with what fixes it.
- **HQ's standard.** The Connections page lists checks beyond what every HQ
  enforces, such as tailnet lock. `SEVERINO_POSTURE=advanced` on the env item
  lists each unmet one in Needs you ([Posture](Posture.md)).
- **Releases.** From now on a merge to `main` reaches this host through
  Compose and Deploy, after your approval ([Deployment](Deployment.md)).
  Rerun `hq-host bootstrap` whenever `host.json` changes, and once after a
  release that changes the sudo rule it renders.
- **Backup.** `hq-host install` enabled the nightly backup. It needs
  `/etc/severino-hq/backup.env`, `sqlite3`, and `age` to encrypt
  ([Backup](Backup.md)).

## When the first run fails

Start with the renderer's journal. Every failure is one line with one class
([Secrets](Secrets.md#renderer-behavior)):

```sh
journalctl -u severino-hq-secrets.service -n 50
```

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `class=config` or `class=host` naming the Connect port or `ip_unprivileged_port_start` | Connect is on a port 1024 or above, or any account can bind its port | publish Connect on `127.0.0.1` below 1024, set `connect.url` to match, rerun `hq-host bootstrap` |
| the renderer does not start: a dependency failed | the unit in `connect.unit` failed to start, or does not exist | fix Connect's unit; check its name in `host.json` |
| `class=connect_denied` | Connect refused the token | encrypt the right token again (step 5); check it can read the vault |
| `class=connect_unavailable` | Connect did not answer in 75 seconds, or the vault kept changing | check `curl -fsS http://127.0.0.1:880/health`; wait for edits to finish |
| `class=content`, "Refusing an application environment without" | the item lacks a required variable, or holds it empty; the line names each one | add it ([step 7](#7-create-the-application-environment)) |
| `class=content`, "a vault that resolved none" | connections were installed, and the vault now holds no item with `connection_ref`; they are kept, and HQ's environment is still rendered | put the items back in the vault `host.json` names, or check `vault` names the right one |
| `class=content`, "must resolve to exactly one item" | `env_item` matches no item, or two | make the item's title unique and equal to `env_item` |
| the renderer fails with `status=226/NAMESPACE` | a directory it writes is missing | `install -d -o root -g root -m 0700 /opt/apps/severino-hq/secrets` |
| HQ answers 400 | the host name is not in `DJANGO_ALLOWED_HOSTS` | add it to the item; start `severino-hq-secrets.service` |
| `hq-host deploy` refuses the image | not a digest-pinned reference to your repository's composition, or not signed by its Compose on `main` | use the digest from step 3 |

The rest of the failure classes are in [Secrets](Secrets.md#renderer-behavior).
A web unit that stays `activating`, a CSRF error on sign-in, a 502 from the
proxy and later deploy failures are in [Deployment](Deployment.md#verifying).
