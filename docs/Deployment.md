# Deployment

HQ runs on one Linux host, reachable only over a tailnet. systemd runs it: the
web container under `severino-hq-web.service`, the controller and the scheduled
work as oneshot units beside it. A merge to `main` reaches that host through
three workflows and one approval. Nothing on the host is built there or read
from a checkout.

![How changes reach HQ: a host pull request and an extension's admission both lead to Compose, which publishes the signed composition; Deploy waits for approval and the self-hosted runner hands it to hq-host on the host](diagrams/changes-reach-hq.png)

<sup>Diagram source: [`diagrams/changes-reach-hq.mmd`](diagrams/changes-reach-hq.mmd).</sup>

## The pipeline

Each workflow is started by the one before it, naming the commit, so nothing
waits on a schedule.

| Workflow | Started by | Does |
| :--- | :--- | :--- |
| **CI** (`ci.yml`) | every pull request and push to `main` | Checks, Tests, Browser, Controller, CodeQL and Image (build, boot healthy, publish and sign the host image); on a pull request the image's checks and its composition beside them, then **Ready**, which passes only when every job did; on `main`, **Release**, which starts Compose |
| **Compose** (`compose.yml`) | CI on a pull request (verify only); CI's Release on `main`; an extension's admission; a person | the host image plus every admitted extension, verified as one application; on `main`, published and signed |
| **Deploy** (`deploy.yml`) | Compose, once it has published; a person, to redeploy or roll back | waits for approval in the `production` environment, then deploys on the self-hosted runner |

Each CI job runs the `mise` aggregate of its name, so the gates are declared
once, as tasks in their job's file under [`tasks/`](../tasks) ([Development](Development.md)).

Production runs the composed image (`ghcr.io/joeseverino/severino-hq/composition`),
never the host image alone. How Compose admits, fingerprints and assembles
the extensions is in [Plugins](Plugins.md#composition). It stamps the released
commit into the image (`SEVERINO_HQ_REVISION`), so production reports the
commit it runs.

A pull request never starts Deploy, so no pull request's code reaches the
self-hosted runner. The runner dials out to GitHub; nothing inbound is opened.
To redeploy or roll back, run **Deploy** with the commit to put back. To
rebuild the current set, run **Compose**. A rollback to a composition from
before an extension was added drops it, so Deploy refuses it; run Compose at
that commit instead, which composes the extensions admitted now onto that host
commit.

A release that fails is undone whole. Its boot keeps a copy of the database
before migrating it (`manage.py boot_migrate`, at the path `hq-host run-web`
names), and the rollback stops HQ, puts that copy back and starts the previous
image on the schema it was built with; what the failed release wrote is lost
with it. A rollback by hand, to a release that already stood, keeps the
schema the newer release left: nothing unapplies a migration. So a migration is
one the release before it can run on (add a table or a column with a default,
remove one only in a later release), or that rollback starts from a backup
([Backup](Backup.md)). `tools/pipeline/host-boot-drill.sh` deploys a migrating
release that cannot serve and proves the previous one comes back on its schema.

### Checked once

When a pull request merges, the push to `main` asks
`tools/pipeline/proven-on-pr.sh` whether the pull request already proved it:
its last CI run passed, its image carries a `dev.severino.hq.tree` label equal to the
pushed commit's tree, and it is signed by that run. With all three, Checks,
Tests and Browser are skipped and Image promotes that image by digest to the
commit's tag. Short of all three, every gate runs again.

### Where a change stands

GitHub's own records say it:

- **Ready** on a pull request is the one check the `main` ruleset requires,
  expected from GitHub Actions. It fails when a job it needs did not pass, and
  its summary lists each job's result.
- A **deployment** to the `production` environment, on the commit Deploy was
  handed: *queued* while it waits for approval, *in progress* once approved,
  then *success* when it passed its health check, *failure* when it did not
  (production still runs the previous image), or *inactive* when it was not
  approved or a newer release took its place. GitHub shows it on the commit,
  the merged pull request and the environment's page. Resolve records it
  itself: the environment's job sets `deployment: false`, since the
  deployment GitHub would record names the commit `main` was at when the run
  started and succeeds once approved.
- **Severino HQ · Production** on an extension's commit: opened by its
  admission and moved on by HQ's controller, through HQ's GitHub App, until
  production runs it, when its connection manages `github.delivery`. That is
  HQ reporting on what it runs, not a CI verdict.

### When a run fails

`mise run annotations RUN_ID` prints the annotations of each failed job, one
per line (level, `path:line`, message): they appear as soon as a step fails,
before its log can be read. `mise run ci:timings RUN_ID` prints where a run's
time went: each job's and step's seconds, and the jobs on its critical path.

Every log is public, so each failure is reported at two levels:

| Where | Says | Holds |
| :--- | :--- | :--- |
| the run's log and summary | the failed step and its error, failing host test ids, counts | nothing that names an extension or the host |
| the sealed `failure-logs` artifact | what the log withheld: the composed suite's output, a refused composition's reason, a container's log | readable only with the operator's identity |
| the host, `/var/log/severino-hq/` | the pull, sync, install and failed-health output, root-only | the host's own detail |

Compose writes what it withholds to `WITHHELD_DIR`;
`tools/pipeline/seal-failure-logs.sh` encrypts it with [age](https://age-encryption.org)
to `FAILURE_LOG_RECIPIENT` in [`tools/pipeline/toolchain.env`](../tools/pipeline/toolchain.env).
`mise run failure-logs RUN_ID` opens it with the identity in
`SEVERINO_FAILURE_LOG_IDENTITY`. To rotate, generate a pair with
`age-keygen -pq`, store the identity, and replace the recipient; runs sealed
before then need the old identity.

Extension values never pass through a step's `env:` block, which the log
prints: they move between steps as files.

### HQ's GitHub App

One app, registered with no webhook and the permissions in
[`deploy/github-apps.json`](../deploy/github-apps.json) (a test holds them to
what HQ asks for), installed on the host repository and every extension. Each
use mints an hour-long token for only what it does:

| Who | Holds the key as | Asks for |
| :--- | :--- | :--- |
| the controller | an SSH Key item in the vault, rendered for openssl only | what each read or report needs |
| an extension's admission | `HQ_APP_KEY` on its `admission` environment, `main` only | Actions write here: start Compose |
| Compose and CI's Release | `HQ_APP_KEY` and `HQ_APP_CLIENT_ID` repository secrets | Actions and Contents read the extensions; Actions write here: start the next workflow |

`mise run wire-github-app` reads the key from 1Password and sets those
secrets on this repository and each extension's `admission` environment,
through standard input. The client ID is a secret too: a variable is state
outside the repository that changes what a build does.

For the controller, keep the key as an SSH Key item beside a connection item
with `connection_ref`, `projection: github_app`, `env_prefix: GITHUB`,
`app_id` and `signing_key` (the SSH Key item's title). Leave `manages` off
until the sweep's view of each extension's admitted and running commit
matches GitHub, then set it and adopt the `github.delivery` record.

To rotate: generate a second key on the app, replace the SSH Key item's key,
run the script again, wait for the connection probe to show the new
fingerprint, then delete the old key on GitHub.

## The host

| Path | Holds | Written by |
| :--- | :--- | :--- |
| `/usr/local/lib/severino-hq/` | the root-owned tree: `hq/config/`, `deploy/` (units, `bin/hq-host`, `bin/hq-secrets`) | `hq-host sync`, from the image |
| `/usr/local/lib/severino-hq-verifier/cosign` | root's pinned cosign, outside the tree a deploy replaces | `hq-host install` |
| `/etc/severino-hq/host.json` | the host's own values ([`deploy/host.example.json`](../deploy/host.example.json)) | the operator |
| `/etc/systemd/system/` | every unit and drop-in under `deploy/systemd`, plus the drop-ins `hq-host bootstrap` renders | `hq-host install`, `hq-host bootstrap` |
| `/etc/sudoers.d/severino-hq-deploy` | the runner's one rule: `hq-host deploy *`. sudo-rs matches no pattern inside an argument, so root checks the image: exactly one argument, a digest-pinned composition of this repository, signed by its compose workflow | `hq-host bootstrap` |
| `/etc/credstore.encrypted/` | the Connect token, as an encrypted systemd credential | the operator ([Secrets](Secrets.md)) |
| `/var/lib/severino-hq/release` | the digest-pinned image the web unit runs | a deploy |
| `/run/severino-hq-secrets/` | rendered credentials, on a root-only `noswap` tmpfs (`run-severino\x2dhq\x2dsecrets.mount`) | `hq-secrets` |
| `/run/severino-hq/` | the doorbell HQ rings when it queues controller work | `deploy/tmpfiles/severino-hq.conf` |
| `/run/severino-hq-renovate/` | root-only: a validated dependency handoff waiting to run, and each run's environment files, removed when it ends | `hq-host controller`, `hq-host renovate` |
| `/var/lib/severino-hq/renovate/dependency-run.json` | the last dependency run's outcome, given read-only to the next controller pass | `hq-host renovate` |
| `/opt/apps/severino-hq/secrets/` | the renderer's lock and the controller's ACME state | `hq-secrets`, the controller |
| docker volumes `severino-hq_severino_{data,media,exports,bridge}` | the database, uploads, exports and the bridge socket | HQ |
| `/var/log/severino-hq/` | output a deploy keeps off the public log, `extensions.log`: which extensions a refused deploy would have dropped, and `renovate.log`: every dependency run's own output | `hq-host` |

### hq-host

`hq-host` (`controller/cmd/hq-host`) is the host's side of HQ. It runs as root,
from the tree, and every unit and the deploy call it.

| Command | Does |
| :--- | :--- |
| `run-web` | runs the web container (`severino-hq-web.service`); waits for the rendered environment, notifies systemd once HQ is healthy |
| `controller [--apply]` | one controller pass: plan, or claim and apply queued work. A pass may leave a dependency handoff in its own out directory; root opens it without following links, validates it (each owner's repositories by GitHub's naming rules, a token that expires within the hour, no other key) and moves it to `/run/severino-hq-renovate/`, or logs and drops it |
| `renovate` | runs the spooled handoff: Renovate once per owner, in a container with no capability, a read-only root, no host path but `deploy/renovate/config.json`, and the token in an environment file; records each owner's outcome for the next pass. The container reaches public IPv4 addresses and nothing else: not the host, the LAN, the tailnet, link-local, multicast or another container. It runs on its own bridge, `severino-hq-renovate` (10.231.0.0/24, interface `hq-renovate0`, no IPv6), made if absent and refused unless it is exactly that, with public resolvers; before each run root loads the nftables table `inet severino_hq_renovate` (`controller/host/egress.go`) in one transaction, dropping everything the bridge sends to a private, reserved or IPv6 destination and everything it sends to the host itself. A run that cannot load the table never starts |
| `deploy IMAGE` | verifies the signature, refuses an image that drops an extension production runs, pulls, and hands over to the image's own `hq-host release` |
| `release IMAGE` | syncs the tree, installs the web unit, restarts HQ on the image, then `install`; rolls back on any failure, the database with it when the release migrated it |
| `install` | installs every shipped unit, renders secrets, preflights the controller and the content index, enables every timer and path |
| `sync IMAGE` | replaces the tree with the image's, refusing a unit that names a path the tree lacks |
| `verify` | reports where the host differs from its release: tree, units, and anything retired (`severino-hq-script-drift.timer`, daily) |
| `bootstrap` | renders the drop-ins and sudo rule `host.json` decides |
| `backup` | snapshots and archives HQ's volumes, where docker says they are ([Backup](Backup.md)) |
| `upgrade ARGS` | moves one compose service to a pinned image, through its own sudo rule ([Container upgrades](#container-upgrades)) |
| `check` | the host's half of `mise run preflight`, as any account and read-only: the runner's sudo rule, and that `sudo -l` lists it no command HQ does not render (a failure names the command and the grep that finds its file), `verify` (or the drift unit's run since the release was installed), every program a sudo rule names is root's alone, the established units, free disk, and, where sudo lets it read nftables, that a loaded Renovate egress table is the one `hq-host` renders (an absent one waits for the next run, which loads it). Preflight builds it from HEAD for the host's platform and runs it from a temporary directory over SSH, so the release being checked is the one checking. Exits 0 ready, 1 a fault, 3 unverified |

### Units

`hq-host install` installs every unit and drop-in under `deploy/systemd`,
found by walking the directory (`controller/host/units.go`), and enables every
timer, path and unit with an `[Install]` section. Adding a unit is adding its
file. A unit a release no longer ships is disabled and removed. A drop-in the
host adds beside a shipped one is the host's, and `verify` does not compare it.

| Unit | Runs |
| :--- | :--- |
| `severino-hq-web.service` | the web container; the only thing that starts it |
| `severino-hq-secrets.service` / `.timer` | `hq-secrets`, hourly ([Secrets](Secrets.md)) |
| `severino-hq-controller.service` / `.timer` / `.path` | `hq-controller --apply`, when HQ rings the doorbell (work queued or a pass due), else 15 minutes after each run ends |
| `severino-hq-job@.service` | one scheduled job inside the web container (`hq-controller job NAME`) |
| `severino-hq-due.timer` | hourly, asks for `due`: every piece of scheduled work, the host's and the extensions', whose cadence (`every=`) has come round |
| `severino-hq-renovate.service` / `.path` | `hq-host renovate`, when a handoff reaches the spool |
| `severino-hq-backup.service` / `.timer` | `hq-host backup`, nightly ([Backup](Backup.md)) |
| `severino-hq-script-drift.service` / `.timer` | `hq-host verify`, daily |

A unit that fails is reported to HQ with no wiring of its own:
`deploy/systemd/severino-hq-.service.d/10-on-failure.conf` (and the same for
timers and paths) sets `OnFailure=severino-hq-job@units.read.service` for
every unit whose name begins `severino-hq-`. The controller, the dependency
run and the `units.read` job carry an empty drop-in of that name, because each
would otherwise restart itself on failure. `controller/host/units_rules_test.go`
holds the shipped units to those rules.

## A deploy

The Deploy job pipes the registry token to `sudo
/usr/local/lib/severino-hq/deploy/bin/hq-host deploy IMAGE`, the one command
the runner may run as root. The runner checks nothing itself and keeps no
checkout or credential: root does not trust it, so a check there would protect
nothing. `hq-host deploy`:

1. Refuses anything but a digest-pinned reference to the composition, and
   verifies its signature as root, against the Compose workflow on `main`
   (`Source.Signer` in `controller/host/deploy.go`, the one copy of that
   identity).
2. Reads the extensions the running release carries, from the lock in its
   image (pulled again by digest if the host no longer has it).
3. Prunes unused images, checks free space, pulls, and refuses the image if it
   drops an extension the running release carries that its composition does
   not declare retired ([Plugins](Plugins.md#composition)). The public log
   says how many; `/var/log/severino-hq/extensions.log` says which.
4. Copies the image's own `hq-host` out, so every step after the pull is the
   new release's code.
5. Stops every active timer and path the tree ships and waits up to three
   minutes for a controller run or secret render already in flight.
6. Replaces the tree with the image's, installs its web unit, records the
   image and restarts `severino-hq-web.service`, which returns once HQ is
   healthy on it.
7. Runs the new tree's `hq-host install`: the controller preflight is the
   release's first pass against the new bridge.
8. Starts what step 5 stopped.

A failure at any step puts back the previous image, tree and units, then
restarts what was held. A unit that was not active before the deploy is not
started by it.

### The bridge

The controller reaches HQ over a Unix socket the web container serves
(`/run/hq-bridge/bridge.sock`, in the `severino_bridge` volume). The socket
exists once the web process starts and is gone while the container is
replaced; a pass in that gap fails with the reason and the next one runs. The
volume takes its owner and mode from the image, and HQ refuses to start if the
directory is not its account's alone. The design is in
[Architecture](Architecture.md#infrastructure-control-plane).

## A new host

[Install](Install.md) brings up a new host, step by step: Connect, the
encrypted credential, the runner, `host.json`, the vault items and the first
start. `tools/pipeline/host-boot-drill.sh` does the web unit's part of it on every
image CI builds.

Every later change arrives through Deploy. Rerun `hq-host bootstrap` when
`host.json` changes.

## Configuration

Every setting is a value in [`hq/config/settings.py`](../hq/config/settings.py).
The environment carries only what differs per installation or is secret, and
these are all the variables HQ reads. An installer sets the *installer* rows on
the application-environment item ([Install](Install.md#7-create-the-application-environment));
the image, the composition and `hq-host run-web` set the *platform* rows; the
*development* rows are for a checkout, its gates and CI. A real environment
variable wins over the rendered file, so a platform row set on the item
changes nothing. A secret marked `_FILE` may instead be read from the file
that variable names. `test_settings_env.py` holds this table to the code.

| Variable | Set by | Does |
| :--- | :--- | :--- |
| `DJANGO_SECRET_KEY` | installer | signs sessions; required, HQ will not start without it |
| `DJANGO_ALLOWED_HOSTS` | installer | every hostname browsers and clients use, comma-separated; required, anything else gets 400 |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | installer | each full origin the browser uses, such as `https://hq.example.com`; sign-in through a proxy fails without it |
| `DJANGO_BEHIND_TLS_PROXY` | installer | `1`: trust the proxy's `X-Forwarded-Proto` and redirect plain HTTP to `SEVERINO_SITE_HOST` |
| `DJANGO_DEBUG` | installer | `0` in production; `1` turns on development's defaults |
| `DJANGO_SESSION_COOKIE_SECURE` | installer | the session cookie only over HTTPS; on unless `DJANGO_DEBUG` |
| `DJANGO_CSRF_COOKIE_SECURE` | installer | the CSRF cookie only over HTTPS; on unless `DJANGO_DEBUG` |
| `DJANGO_HSTS_SECONDS` | installer | how long browsers insist on HTTPS; a year by default, `0` only while TLS is not working |
| `DJANGO_TIME_ZONE` | installer | the time zone dates are shown in, such as `America/New_York`; `America/Chicago` by default |
| `SEVERINO_SITE_NAME` | installer | the name in page titles and the header |
| `SEVERINO_SITE_HOST` | installer | the canonical host for printed pages, exports and the HTTPS redirect; derived from the two lists above when unset |
| `SEVERINO_TRUSTED_NETWORKS` | installer | who may reach HQ at all, as CIDR ranges; loopback and Tailscale's ranges by default |
| `SEVERINO_TRUSTED_PROXIES` | installer | the proxy's exact address, the only source whose `X-Forwarded-For` HQ believes; loopback by default |
| `SEVERINO_SECRET_STORE_KEY`, `SEVERINO_SECRET_STORE_KEY_FILE` | installer | 32 characters or more; seals the secrets an operator hands HQ ([Secrets](Secrets.md#severino_secret_store_key)); unset, HQ holds none |
| `SEVERINO_POSTURE` | installer | `advanced` lists each unmet check of HQ's standard in Needs you ([Posture](Posture.md)) |
| `SEVERINO_OIDC_ENABLED` | installer | `1` signs in through the identity provider and closes the password form |
| `SEVERINO_OIDC_ISSUER` | installer | the identity provider's issuer URL |
| `SEVERINO_OIDC_CLIENT_ID` | installer | HQ's client at the identity provider |
| `SEVERINO_OIDC_CLIENT_SECRET` | installer | that client's secret |
| `SEVERINO_OIDC_ALLOWED_GROUPS` | installer | the groups whose members may sign in, comma-separated |
| `SEVERINO_OIDC_ALLOWED_EMAILS` | installer | addresses that may sign in, honored only when the provider marks them verified; setting it requests the `email` scope |
| `SEVERINO_OIDC_CREATE_USER` | installer | whether a first sign-in creates the account; on by default |
| `SEVERINO_PASSWORD_LOGIN_ENABLED` | installer | break-glass: `1` reopens the password form beside SSO; follows `SEVERINO_OIDC_ENABLED` when unset |
| `SEVERINO_API_RESOURCE` | installer | the identity provider's API audience; empty disables the machine API and MCP |
| `SEVERINO_MCP_ALLOWED_HOSTS` | installer | the host names MCP clients connect to; empty disables MCP |
| `SEVERINO_MCP_ALLOWED_NETWORKS` | installer | the networks MCP answers; Tailscale's ranges by default |
| `SEVERINO_MCP_ALLOWED_ORIGINS` | installer | browser origins MCP accepts; none by default |
| `SEVERINO_MCP_ENABLE_WRITES` | installer | `1` lets an agent make ordinary writes |
| `SEVERINO_MCP_ENABLE_DOC_SYNC` | installer | `1` lets an agent mirror the documentation index, without other writes |
| `SEVERINO_MCP_ENABLE_INFRASTRUCTURE` | installer | `1` lets an agent declare infrastructure |
| `SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS` | installer | `1` lets public DNS declarations reconcile |
| `SEVERINO_DASHBOARD_LINKS` | installer | extra dashboard links, as a JSON list of `{"label", "sub", "href"}` |
| `HQ_CONTROLLER_ID` | installer | the installation a sweep files its findings under; the machine's name when unset |
| `CONTENT_INDEX_URL` | installer | a public site's content index to sync; unset turns the sync off |
| `CONTENT_INDEX_PROJECT_SLUG` | installer | the project the index belongs to, when its public URL does not say |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_ID_FILE` | installer | the Cloudflare Access service token's ID the index sits behind |
| `CF_ACCESS_CLIENT_SECRET`, `CF_ACCESS_CLIENT_SECRET_FILE` | installer | that token's secret |
| `CLOUDFLARE_D1_DATABASE_NAME` | installer | which D1 database is the contact inbox, when more than one holds the contact table |
| `SEVERINO_APP_ENV_PATH` | platform | the rendered application environment, which settings load into every process |
| `SEVERINO_DATABASE_PATH` | platform | the SQLite database; HQ will not start without it |
| `SEVERINO_MEDIA_ROOT` | platform | where uploads are kept |
| `SEVERINO_EXPORTS_ROOT` | platform | where exports are written |
| `DJANGO_STATIC_ROOT` | platform | the collected static tree, built into the image |
| `SEVERINO_HQ_SOURCE` | platform | the repository this build came from |
| `SEVERINO_HQ_REVISION` | platform | the commit this build is |
| `SEVERINO_HQ_PLUGINS` | platform | the installed extensions' manifests, as `package.module:PluginManifest` entries |
| `SEVERINO_HQ_PLUGIN_LOCK` | platform | the composition's signed lock of admitted extensions |
| `SEVERINO_HQ_PLUGIN_POLICY_SHA256` | platform | the digest of the admission policy the lock was made under |
| `SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION` | platform | whether extensions must be admitted; on unless `DJANGO_DEBUG` when unset |
| `SEVERINO_BRIDGE_SOCKET` | platform | the Unix socket the controller bridge is served on; unset serves none |
| `SEVERINO_CONTROLLER_DOORBELL` | platform | the file HQ touches when it queues controller work |
| `SEVERINO_SCHEMA_SNAPSHOT` | platform | where boot keeps a copy of the database before migrating it |
| `SEVERINO_LOG_LEVEL` | development | a standard Python log level; `INFO` by default |
| `DJANGO_TEMPLATE_CACHE` | development | whether rendered templates are cached; on unless `DJANGO_DEBUG` |
| `DJANGO_WHITENOISE_AUTOREFRESH` | development | serves static files from the source trees, uncached; refused with `DJANGO_DEBUG` off in a deployment check |
| `SEVERINO_DEBUG_TOOLBAR` | development | `1` with `DJANGO_DEBUG` shows Django Debug Toolbar |
| `SEVERINO_DEBUG_TOOLBAR_IPS` | development | who sees the toolbar; loopback by default |
| `SEVERINO_CONTROLLER_HEARTBEAT` | development | where HQ records that the controller reached it; beside the database when unset |
| `SEVERINO_ACTIVITY_MARKER` | development | where HQ marks itself in use; beside the database when unset |
| `SEVERINO_TEST_DATABASE_PATH` | development | the suite's database file; one per run in the temporary directory by default |
| `SEVERINO_TEST_MARKERS` | development | the directory a test run keeps the controller's markers in |
| `SEVERINO_TEST_BUILDS` | development | the directory a test run builds programs into |
| `SEVERINO_TEST_LEAK_LEDGER` | development | the file parallel test workers record leaked resources in |
| `HQ_TEST_STALL_SECONDS` | development | seconds a test process may go without progress before it prints what it is running, every stack and the browser's log, and fails that test; 300 when unset, `0` turns it off |
| `HQ_TEST_REPORTS` | development | the folder a gate names for a test run to write each failed test into, with its exception and traceback; set by `tools/gate/verdict.py` |
| `HQ_API_RECORD_EXAMPLES` | development | `1`, with `--parallel 1`, writes the API responses a run receives to `openapi-examples.json` |

## Network exposure

The container uses host networking and Uvicorn binds port 8000, so `/mcp/`
sees the real tailnet peer rather than Docker's bridge gateway. A co-located
reverse proxy terminates TLS and forwards the browser UI to `127.0.0.1:8000`;
its address is the only entry in `SEVERINO_TRUSTED_PROXIES`. MCP clients
connect to the host's tailnet address on port 8000 directly. Limit host
ingress for 443 and 8000 to the tailnet interface and loopback, and forward
neither port from a router.

With Nginx Proxy Manager, attach an access list whose client rules allow
exactly the tailnet ranges in `SEVERINO_TRUSTED_NETWORKS`, in that order, and
keep `satisfy_any` off. NPM generates the final `deny all` itself; HQ records
that implicit default from its reading. The checklist is in
[Security](Security.md#production-checklist).

## Remote targets

The receivers the controller deploys certificates to are in
[`deploy/targets/`](../deploy/targets/). Install the edge controller and
dispatcher root-owned, force the edge key to the dispatcher, and allow that
account to sudo only the controller. Install the cPanel controller as the
cPanel account and force its key to that script. Each rejects every operation
outside its allowlist. These are installed by an administrator; a deploy
cannot rewrite its own remote authorization.

## Container upgrades

`hq-host upgrade` ([`controller/host/upgrade`](../controller/host/upgrade))
is the program an upgrade plan in HQ describes. It runs as root for another
account, so it trusts nothing that account passes beyond naming the upgrade:

- The stack is a directory directly under `/opt/apps`, not a symlink. The
  directory, its compose file, any override and any `.env` are owned by root
  or by the owner of `/opt/apps`, and writable by no one else.
- `--from` is the image `docker compose config` resolves for the service and
  what its one running container runs. `--to` is the same repository, pinned
  by digest.
- The data is the running container's writable mounts, read from Docker. A
  writable directory mount outside the stack is refused.

For one compose service it pulls the target, stops the service and snapshots
its data, proves the target against a copy of the data with no network, pins
the digest in the stack's compose override, recreates the service and
verifies it stays up for a settle window. Any failure restores the override
and the data. It is idempotent by operation id and prints one JSON result:
exit 0 kept, 3 rolled back, 2 refused or unchanged, 1 failed and not put back.
It needs Docker Compose 2.24.4 or later, `tar`, and Linux.

A machine runs it only through a sudo rule for that one program,
`hq-host upgrade *`; the plan shows the rule and how to check it. sudo-rs
matches no pattern inside an argument, so the helper's parser, run as root, is
the boundary: each flag once, every value whole by its pattern, the stack
directly under `/opt/apps`. The rule is only worth having if that
account cannot already reach Docker. HQ's own host has `hq-host` in its tree.
Any other machine needs its own root-owned copy: the plan's fetch step takes
it from the composed image HQ runs, by digest, after `cosign` verifies the
compose workflow's signature, and stops if the image's platform is not the
machine's. HQ queues no upgrade to another machine, so there the helper is
run by hand with the plan's values.

A machine whose rule names a program other than `hq-host upgrade` admits
nothing: re-run the plan's allow step there to write the current rule.

## Verifying

From the host:

```sh
curl -I http://127.0.0.1:8000/accounts/login/
systemctl status severino-hq-web.service
sudo /usr/local/lib/severino-hq/deploy/bin/hq-host verify
```

From a device on the tailnet, every URL redirects to `/accounts/login/` until
you sign in; the audit log records the sign-in.

| Symptom | Cause |
| :--- | :--- |
| 502 from the proxy | HQ is not answering on `127.0.0.1:8000`: `journalctl -u severino-hq-web.service` |
| the web unit stays `activating` | the environment has not rendered: `journalctl -u severino-hq-secrets.service` |
| CSRF errors on sign-in | `DJANGO_CSRF_TRUSTED_ORIGINS` lacks the full origin, scheme and host, or `DJANGO_BEHIND_TLS_PROXY` is not `1` |
| Compose fails in "Resolve the host image" | CI for that commit failed, so the host image was never published |
| an extension merged and nothing deployed | its admission did not dispatch Compose, or found nothing new: run Compose |
| the Deploy step failed | production still runs the previous image; the detail is in `/var/log/severino-hq/` on the host |
| a deploy refuses an image that drops an extension | the composition does not say it was meant: name the extension in `retired` in `COMPOSITION_EXTENSIONS` and run Compose, or restore it to the set |
| a push says `permission_denied: write_package` | the package does not give this repository write: in its settings, under Manage Actions access, give it the Write role |
| minting HQ's token says `Bad credentials` or `could not create installation access token` | the key or client ID in the secrets is not the app's: run `mise run wire-github-app` |
| a step as HQ's app says `Resource not accessible by integration` | the installation lacks a permission: compare it with `deploy/github-apps.json` and accept the change |
