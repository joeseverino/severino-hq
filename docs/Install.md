# Install

From a Linux host with Docker to HQ up and the controller reading its first
connection. Each step says what it is for, what to do, and how to check it
worked. Do them in order: later steps read what earlier ones made.

Why each piece is shaped the way it is lives in [Deployment](Deployment.md)
and [Secrets](Secrets.md). This page is only the order and the commands.

## Before you start

### The host

- Linux with systemd, Docker and `python3`. systemd must support `noswap` on
  a tmpfs (the credential mount uses it); the setup's modules run on the
  host's `python3`.
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
- **Cloudflare**, with a zone of your own: the inbound-events Worker answers
  on a name in it ([step 12](#12-hear-providers-events)).
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

1. Register HQ's GitHub App with its webhook's Active unticked and the
   permissions in [`deploy/github-apps.json`](../deploy/github-apps.json).
   Install it on this repository and each extension. [Step
   12](#12-hear-providers-events) points its webhook
   at the inbound-events Worker.
2. Store the app's private key in 1Password, then set the secrets with
   `mise run wire-github-app`; name each extension with `--extension`, or
   none. It needs `gh` and `op` signed in:

   ```sh
   mise run wire-github-app --vault 'Example Vault' --item 'example github app' --client-id EXAMPLE_CLIENT_ID --extension example-owner/example-extension
   ```

3. Set the `COMPOSITION_EXTENSIONS` repository secret: JSON in the shape of
   [`composition/extensions.json`](../composition/extensions.json), listing
   the extensions to compose; `{"extensions": []}` composes the host alone.
   To take one out of production later, move its id to `retired`: a deploy
   refuses an image that drops an extension production runs unless it is named
   there. How an extension is admitted is in [Extensions](Plugins.md#composition).
4. Create the `production` environment with yourself as a required reviewer.
5. Add a `main` ruleset requiring the **Ready** check, with GitHub Actions
   as its expected source.

**Check:** the repository's secrets list `HQ_APP_KEY`, `HQ_APP_CLIENT_ID` and
`COMPOSITION_EXTENSIONS`.

## 3. Publish a composition

**For:** the host runs the composed image your repository publishes, pinned by
digest: `ghcr.io/OWNER/REPO/composition`, with your repository's owner and
name in lowercase, as GHCR names them. The commands below keep `OWNER/REPO`
for you to fill in.

Merge to `main`. CI's **Release** starts **Compose**, which publishes and signs
the composition. To run it by hand: Actions, **Compose**, Run workflow, on
`main`.

Compose then starts **Deploy**, which waits for your approval. The host is not
ready for it yet: reject it.

Find the digest of what Compose published. Either read it on the package's
page on GitHub, or, from any machine with Docker Buildx:

```sh
docker buildx imagetools inspect ghcr.io/OWNER/REPO/composition:COMMIT_SHA
```

**Check:** you have a reference of the form
`ghcr.io/OWNER/REPO/composition@sha256:<64 hex>`.

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
   `grants` and `automation` are optional
   ([Tokens on the host](Deployment.md#tokens-on-the-host)).

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

The variables are the *installer* rows of the table in
[Deployment](Deployment.md#configuration), each with what it does and its
default.

- **Required:** `DJANGO_SECRET_KEY` (generate a long random value in
  1Password) and `DJANGO_ALLOWED_HOSTS`. The renderer refuses an item that
  lacks one, or holds it empty, and names each one missing
  (`REQUIRED_APP_VARIABLES` in `hq/config/settings.py`).
- **Behind a TLS proxy:** `DJANGO_CSRF_TRUSTED_ORIGINS` and
  `DJANGO_BEHIND_TLS_PROXY`, and the proxy's address in
  `SEVERINO_TRUSTED_PROXIES`, the one peer whose forwarded address and scheme
  HQ believes. The renderer does not require them, but sign-in through a
  proxy does not work without them.
- **On a first install:** `DJANGO_DEBUG=0`, the two cookie switches,
  `DJANGO_TIME_ZONE` and `SEVERINO_SITE_NAME`, with `SEVERINO_OIDC_ENABLED=0` until sign-in is set up below.
- **The rest** when you use the feature it names.

**Check:** the item has `DJANGO_SECRET_KEY` and `DJANGO_ALLOWED_HOSTS` with
values, and no name appears twice.

## 8. Add the first connection

**For:** what HQ reads. This step is optional: HQ starts without a
connection, and you can add one now or later. For each provider not yet
connected, HQ's Connections page says what token to create, the permissions
it needs and the fields its item takes. Once the host has a connection, a
vault that holds none is refused and the installed connections are kept
([Secrets](Secrets.md#renderer-behavior)).

A connection is a vault item with three text fields the renderer reads:
`connection_ref` (a name you choose), `projection` (the shape of the
credential) and `env_prefix` (which provider it is for), plus the fields its
shape needs. Every provider's item, field by field, is in
[Connections](Connections.md); the rules are in [Secrets](Secrets.md#connections).

Tailscale is a natural first, because HQ already needs a tailnet. In the
Tailscale admin console, create an OAuth client with the read scopes
`mise run mint:tailscale` asks for ([Secrets](Secrets.md#minting-observer-credentials)).
Then create an **API Credential** item in the vault:

| Field | Value |
| :--- | :--- |
| username | the OAuth client's ID |
| credential | the OAuth client's secret |
| `connection_ref` | `tailscale` |
| `projection` | `oauth_client` |
| `env_prefix` | `TAILSCALE` |

Leave `manages` off until you want HQ to change things through it. Without it
the connection only reads ([Derived Facts](Derived-Facts.md#adoption)).

An item is read at the next render (hourly, or at once with `systemctl start
severino-hq-secrets.service`) and probed at the controller's next pass.

**Check:** the item is in the vault `host.json` names, not another one.
If you skip this step, step 11 has nothing to show until you add one.

## 9. Install HQ on the host

**For:** puts the root-owned tree, the renderer and the web unit in place and
starts them. Every later release arrives through Deploy.

Name the image once for this shell, with the reference from step 3:

```sh
export IMAGE='ghcr.io/OWNER/REPO/composition@sha256:DIGEST'
```

Verify its signature against your Compose workflow on `main`. Run this here
or on any machine with [cosign](https://docs.sigstore.dev/cosign/system_config/installation/):

```sh
cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity-regexp '^https://github\.com/OWNER/REPO/\.github/workflows/compose\.yml@refs/heads/main$' "$IMAGE"
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

Record the release, then set the host up for it:

```sh
install -d -m 0755 /var/lib/severino-hq
```

```sh
printf '%s\n' "$IMAGE" > /var/lib/severino-hq/release
```

```sh
/usr/local/lib/severino-hq/deploy/bin/hq-host install
```

`install` verifies and pulls the runner image the release pins, then applies
the release's setup playbook in it ([Host setup](Deployment.md#host-setup)):
the units, the drop-ins and the runner's sudo rule `host.json` decides, the
directories the units write, the render, HQ under its unit, the controller and
content preflights, and every timer and path, enabled and started. It prints
that HQ is installed, preflighted and enabled; its full output is in
`/var/log/severino-hq/setup.log`. A run that fails puts every unit file back.

**Check:** the render succeeded, HQ answers, and the host is its release:

```sh
cat /run/severino-hq-secrets/status.json
```

`last_attempt.outcome` is `rendered` or `current`, and `counts.connections` is
at least 1 if you added a connection in step 8. The document holds no secret.

```sh
curl -fsS http://127.0.0.1:8000/health/ready/
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

## 12. Hear providers' events

**For:** providers' webhooks reach a Cloudflare Worker on a name of your own
zone, never HQ, and HQ's listener dials out to it, so what changed is read
in seconds ([Security](Security.md#the-inbound-events-worker)).

Run these from a checkout of your repository, with `gh` and `op` signed in.
They keep `OWNER/REPO`, `events.example.com` for the name on your zone the
Worker answers on, `0123abcd` for your Cloudflare account ID, and the vault
and item names for you to fill in.

1. Create the item holding the Worker's secrets, `GITHUB_WEBHOOK_SECRET`,
   `TAILSCALE_WEBHOOK_SECRET` and `SOCKET_TOKEN`. Tailscale makes its own
   secret (step 6), so until then that field holds one nobody signs with,
   and the Worker's Tailscale route takes nothing:

   ```sh
   op item create --vault 'Example Vault' --category password --title 'example events' "GITHUB_WEBHOOK_SECRET[password]=$(openssl rand -hex 32)" "TAILSCALE_WEBHOOK_SECRET[password]=$(openssl rand -hex 32)" "SOCKET_TOKEN[password]=$(openssl rand -hex 32)"
   ```

2. Mint the Worker's deploy token, Workers Scripts Write alone, into an API
   Credential item of its own. The bootstrap token is one that may create API
   tokens ([Secrets](Secrets.md#minting-observer-credentials)):

   ```sh
   op item create --vault 'Example Vault' --category 'API Credential' --title 'example worker deploy'
   ```

   ```sh
   CLOUDFLARE_BOOTSTRAP_TOKEN='op://Example Bootstrap/cloudflare/credential' op run -- mise run mint:cloudflare --account 0123abcd --name hq-worker-deploy --permissions mise-tasks/worker/deploy.permissions --store 'op://Example Vault/example worker deploy/credential'
   ```

3. Create the `inbound-worker` environment, deploying from `main` alone, and
   give it the token, the account and the hostname. Deploy Worker runs there
   on each merge that changes the Worker:

   ```sh
   gh api -X PUT repos/OWNER/REPO/environments/inbound-worker -F 'deployment_branch_policy[protected_branches]=false' -F 'deployment_branch_policy[custom_branch_policies]=true'
   ```

   ```sh
   gh api -X POST repos/OWNER/REPO/environments/inbound-worker/deployment-branch-policies -f name=main -f type=branch
   ```

   ```sh
   op read 'op://Example Vault/example worker deploy/credential' | gh secret set CLOUDFLARE_API_TOKEN --repo OWNER/REPO --env inbound-worker
   ```

   ```sh
   gh secret set CLOUDFLARE_ACCOUNT_ID --repo OWNER/REPO --env inbound-worker --body 0123abcd
   ```

   ```sh
   gh secret set HQ_INBOUND_HOSTNAME --repo OWNER/REPO --env inbound-worker --body events.example.com
   ```

4. Deploy the Worker the first time, with its secrets. A later deploy keeps
   them; run this again after rotating one:

   ```sh
   CLOUDFLARE_API_TOKEN='op://Example Vault/example worker deploy/credential' CLOUDFLARE_ACCOUNT_ID=0123abcd HQ_INBOUND_HOSTNAME=events.example.com GITHUB_WEBHOOK_SECRET='op://Example Vault/example events/GITHUB_WEBHOOK_SECRET' TAILSCALE_WEBHOOK_SECRET='op://Example Vault/example events/TAILSCALE_WEBHOOK_SECRET' SOCKET_TOKEN='op://Example Vault/example events/SOCKET_TOKEN' op run -- mise run worker:deploy --with-secrets
   ```

5. Point HQ's GitHub App's webhook at the Worker: the command from [step
   2](#2-set-up-the-repository), with the secret it signs with.

   ```sh
   HQ_INBOUND_HOSTNAME=events.example.com mise run wire-github-app --vault 'Example Vault' --item 'example github app' --client-id EXAMPLE_CLIENT_ID --extension example-owner/example-extension --webhook-secret 'op://Example Vault/example events/GITHUB_WEBHOOK_SECRET'
   ```

   Then, on the App's settings page, tick Active and subscribe to the events
   the command prints. GitHub has no API for either.
6. With a Tailscale connection, point the tailnet's webhook at the Worker.
   In the admin console, under Settings, Webhooks, add an endpoint,
   `https://events.example.com/tailscale`, with the events
   `worker/src/generated/routes.json` lists for `tailscale`. Tailscale shows
   its secret once: put it in the item's `TAILSCALE_WEBHOOK_SECRET` field,
   then set it on the Worker, which keeps every other secret it holds:

   ```sh
   cd worker && CLOUDFLARE_API_TOKEN='op://Example Vault/example worker deploy/credential' CLOUDFLARE_ACCOUNT_ID=0123abcd TAILSCALE_WEBHOOK_SECRET='op://Example Vault/example events/TAILSCALE_WEBHOOK_SECRET' op run -- sh -c 'printf %s "$TAILSCALE_WEBHOOK_SECRET" | ./node_modules/.bin/wrangler secret put TAILSCALE_WEBHOOK_SECRET'
   ```

   Then send the console's test event: the Worker answers it 202 and
   forwards nothing, since HQ takes no `test` event.
7. Add the `cloudflare_events` connection, which the listener waits for
   before it starts: an item in the vault `host.json` names, laid out as
   [Connections](Connections.md#cloudflare-inbound-events) says, its
   `credential` the `SOCKET_TOKEN` from step 1 and its `website` the Worker's
   socket, `wss://events.example.com/socket`. The listener starts as the
   renderer installs it ([step 8](#8-add-the-first-connection)).

**Check:**

- **Events** (`/infrastructure/events/`) lists the Worker channel with when
  it was last heard, and each event under the channel it came by, with what
  it made due and its outcome. A push to a repository the App is installed
  on appears there within seconds.
- **Controller** (`/infrastructure/controller/`) shows the event listener
  online. Until the connection is installed it reads not set up, with a
  Setup guide link to this step.
- A Deploy waiting on `production` is a finding under **Needs you**, its
  link the run's page on GitHub, where you approve or reject it.

HQ is installed.

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
- **Releases.** A merge to `main` reaches this host through
  Compose and Deploy, after you approve it on GitHub
  ([Releasing a deploy](Deployment.md#releasing-a-deploy)).
  Each deploy sets the host up again from `host.json`; rerun `hq-host install`
  when `host.json` changes between releases.
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
| `class=config` or `class=host` naming the Connect port or `ip_unprivileged_port_start` | Connect is on a port 1024 or above, or any account can bind its port | publish Connect on `127.0.0.1` below 1024, set `connect.url` to match, rerun `hq-host install` |
| the renderer does not start: a dependency failed | the unit in `connect.unit` failed to start, or does not exist | fix Connect's unit; check its name in `host.json` |
| `class=connect_denied` | Connect refused the token | encrypt the right token again (step 5); check it can read the vault |
| `class=connect_unavailable` | Connect did not answer in 75 seconds, or the vault kept changing | check `curl -fsS http://127.0.0.1:880/health`; wait for edits to finish |
| `class=content`, "Refusing an application environment without" | the item lacks a required variable, or holds it empty; the line names each one | add it ([step 7](#7-create-the-application-environment)) |
| `class=content`, "a vault that resolved none" | connections were installed, and the vault now holds no item with `connection_ref`; they are kept, and HQ's environment is still rendered | put the items back in the vault `host.json` names, or check `vault` names the right one |
| `class=content`, "must resolve to exactly one item" | `env_item` matches no item, or two | make the item's title unique and equal to `env_item` |
| HQ answers 400 | the host name is not in `DJANGO_ALLOWED_HOSTS` | add it to the item; start `severino-hq-secrets.service` |
| `hq-host deploy` refuses the image | not a digest-pinned reference to your repository's composition, or not signed by its Compose on `main` | use the digest from step 3 |

The rest of the failure classes are in [Secrets](Secrets.md#renderer-behavior).
A web unit that stays `activating`, a CSRF error on sign-in, a 502 from the
proxy and later deploy failures are in [Deployment](Deployment.md#verifying).
