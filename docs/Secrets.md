# Secrets

HQ reads every credential from one 1Password vault, through a 1Password
Connect server on the host's loopback. `hq-secrets` renders two things from it
onto a root-only tmpfs: the web container's application environment, and the
controller's connections document. Neither the web container nor the
controller talks to Connect, and neither holds the other's secrets.

Connect is a local read path for secrets already intended for the host. It is
not a new ingress for applications, a replacement for disk encryption, or a
boundary against host root. The certificate publisher uses a separate cloud
service account because Connect's file API does not support uploads.

## Bootstrap and ownership

The steps for a new host are in [Install](Install.md); this section is the
rules they keep.

The host's own values live in `/etc/severino-hq/host.json`
([`deploy/host.example.json`](../deploy/host.example.json)): the vault, the
application-environment item, the Connect unit and loopback URL, and the name of
the encrypted credential holding the reader token.

`hq-host bootstrap` renders them into drop-ins for
`severino-hq-secrets.service` (`10-estate.conf`, `20-connect.conf`). It refuses
a value systemd would read as something else, or a Connect that is not on
`127.0.0.1`. The tmpfs is `deploy/systemd/run-severino\x2dhq\x2dsecrets.mount`,
root-only, `noswap`.

| What | Rule |
|---|---|
| Recovery copies | keep recovery copies of Connect credentials and reader tokens in an operator-only 1Password vault that this Connect installation cannot access |
| Provisioning | over authenticated administrative SSH with host identity verification |
| Secret bytes | through stdin into `systemd-creds encrypt`; never in command arguments, shell history, CI output, or this repository |
| The bootstrap token | never fetched from Connect itself; no cloud access required on every boot |
| GitHub Actions | does not need the runtime token |

Store each encrypted credential under `/etc/credstore.encrypted/`, root-owned
and mode 0600. Its embedded credential name must match the `connect.credential`
in `host.json`, which becomes the unit's `LoadCredentialEncrypted=` name.
systemd supplies the plaintext runtime file in `CREDENTIALS_DIRECTORY`.
Provision Connect's server credential and each client token separately. Reader
tokens have read-only access to the necessary vaults.

Use TPM-backed credentials where the host supports them and test recovery
after firmware and boot-policy changes. Host-key-only encryption cannot protect
against theft of a disk containing both ciphertext and the host key. Disk and
backup encryption remain separate requirements. Running root can obtain runtime
secrets with either approach.

The certificate publisher's service-account token is an item in the vault the
reader token reads, projected like any other connection (`service_account`).
A holder of the reader token can therefore read it, and that token is write
authority over the vault the publisher writes to.

That is by design: every credential the controller uses is one item in one
vault, delivered one way. Credentials that can mint other credentials are the
exception and stay out of that vault; see
[Minting observer credentials](#minting-observer-credentials).

## Network and execution boundaries

- Connect binds only to IPv4 loopback. Do not publish it through a reverse proxy,
  Tailscale Serve/Funnel, LAN listener, or a tailnet address.
- Connect is published on a loopback port below 1024. The loopback rule proves
  the address and says nothing of who is listening. A port any account may
  bind could be taken while Connect is down, by any local process or a
  container on the host's network, and the reader token sent to whoever took
  it. Only root can listen below 1024, so only root's Connect can answer there.
- The renderer refuses an endpoint on any other port, and its dialer refuses to
  connect to one. It refuses to run on a host whose
  `net.ipv4.ip_unprivileged_port_start` is not above the port. There is no
  setting that relaxes this.
- The renderer's unit `Requires=` the Connect unit and is ordered after it, so
  a Connect that failed to start stops the render.
- Tailscale grants restrict administrative access to the host. They do not
  authorize processes on the host to use Connect. Local nftables rules enforce
  that boundary; test local unprivileged callers and container bridge paths,
  including direct access to the container address.
- Run Connect under a dedicated identity, with only its required credential and
  cache mounts. Audit Docker privileges, capabilities, socket access, restart
  ownership, resource limits, and image provenance separately.
- Only the root renderer holds the reader token. It reads the token from the
  unit's credential file and sends it in one place, the `Authorization` header
  of a request to loopback. The token is never an argument, an environment
  variable of any process, a log field, or part of an error. The web container
  and the controller container receive rendered files, never Connect
  credentials, and neither talks to Connect.
- The certificate writer runs `op` with its service-account token in that one
  child's environment, inside the controller container. It still requires
  cloud access.
- The controller reaches HQ over one Unix socket, which HQ's web process
  serves and nothing else listens on. HQ answers with declarations and queued
  work, and is sent readings and results.
- A secret crosses the socket only sealed to the controller, toward the
  controller: an uploaded certificate's private key. The `material` action
  hands it over as HQ stored it, and the controller opens it when it is about
  to install it. No provider credential crosses it in either direction.
- The socket's directory is a volume only the web account can enter, mounted
  read-only into the controller's container. Each side checks the directory,
  the socket and the peer's uid before a byte is exchanged
  ([Architecture](Architecture.md#infrastructure-control-plane)).
- The controller's container is given neither HQ's database nor its
  application environment. So the process that holds every provider credential
  holds none of HQ's, and the reverse.

Connect needs outbound cloud synchronization. Cached availability does not mean
revocation is instantaneous during a disconnection. Monitor synchronization age
separately from successful cached reads. Define an acceptable stale-data window
per consumer and an emergency local shutdown procedure.

## Renderer behavior

`hq-secrets` (`controller/cmd/hq-secrets`) is a static Go binary. Root runs the
copy in the root-owned tree, `/usr/local/lib/severino-hq/deploy/bin/hq-secrets`,
which the image build compiles and `hq-host sync` installs. It holds no
provider code. It reads through Connect and nothing else: there is no
service-account read path and no fallback. A host still configured with
`SEVERINO_SECRETS_BACKEND` set to anything but `connect` is refused.

Connect is read over its REST API with types generated from 1Password's
published OpenAPI document (`controller/api/vendor/onepassword-connect`). The
endpoint must be `http://127.0.0.1:PORT`, and the dialer itself refuses any
address that is not IPv4 loopback, so a later change to the endpoint check
cannot widen where the token goes. The client uses no proxy, follows no
redirect, bounds every phase and every response, and accepts only JSON.

Connect stays locked after a restart until an authenticated request arrives, so
readiness is the authenticated vault listing, never `/health`.

| Answer | Treated as |
|---|---|
| No answer, a timeout, a 5xx | retried with backoff under one deadline (`-connect-timeout`, 75 seconds) |
| A refused token, a malformed answer | final |

The unit's `TimeoutStartSec` is the outer bound. A change to the item is live
at the next hourly run, or at once with
`systemctl start severino-hq-secrets.service`. The web container restarts only
when its environment changed.

One render is one consistent read: the vault's content version is read before
and after the items, and an item's version is checked against its listing. A
vault edited mid-read is read again; one that never holds still is refused.
Archived and deleted items are not rendered.

The renderer rejects:

- failed discovery, or a vault that does not resolve to exactly one;
- duplicate connection metadata, refs and prefixes;
- invalid names, or an unknown projection;
- a required field that is missing or present twice;
- values containing control characters.

The connection registry (`hq/config/controller-connections.json`) says where
on a vault item each setting of each shape comes from. HQ emits it from the
connection shapes its registry declares (`manage.py bridge_contract`). It is
read strictly. One whose projections are not the shapes the connections
document is typed with is refused whole. The vault remains the connection
inventory.

An item that names no provider is for the one its `env_prefix` spells, except
an SSH transport, which is for `ssh`.

An empty application environment, or one without a variable HQ cannot run
without, is refused, and the refusal names each one missing. Which variables
those are is declared once, as `REQUIRED_APP_VARIABLES` in
`hq/config/settings.py`. `manage.py bridge_contract` writes them into the
registry as `required_app_variables`: `DJANGO_ALLOWED_HOSTS` and
`DJANGO_SECRET_KEY`. Any other variable is optional.

A vault that resolves no connection renders an empty connections document
where none is installed, or where the installed one is empty. A new host, or
one whose tmpfs a reboot cleared, starts HQ before any connection exists.

Where the installed document holds a connection, or is there and cannot be
read as the renderer's own, a vault that resolves none is refused as `content`.
The installed connections and identities stay. The application environment is
still installed and the web container restarted for it. No state is saved, so
every run reads the vault in full and refuses again until it holds a
connection.

HQ remembers what each controller last reported. A controller that reported
connections and then reports none raises the serious finding
`connections-lost`. That is what catches an emptied vault on a host that had
nothing installed to keep.

The renderer also refuses:

| Refused | Why |
|---|---|
| A carriage return or a variable set twice, in the application environment | readers would disagree about the value |
| A connections document larger than the controller reads | refused as content, so the last good one stays |
| An SSH host or user that is not a plain name | |
| An `env_prefix` in a namespace the controller's own settings use (`HQ`, `SEVERINO`, `DJANGO`, `PYTHON`, `OP`, `SSL`, `LC`) | |

A bootstrap reference is compared to the vault's names without regard to case
or surrounding spaces. `known_hosts` names a host as an SSH client looks it up:
bare on port 22, `[host]:port` otherwise.

Refresh takes an exclusive lock on the runtime tmpfs, checks the host, and
only then reads Connect. Everything is read and validated in memory. What is
renamed into place is staged on the private tmpfs, before any installed file is
touched.

Retrieval or validation failure preserves the previous files. A staging
directory a killed run left is removed by the next run, under the lock.

Every installed file, the application environment included, is written under
the lock the launcher holds shared while it copies them, taken before the
first write.

A changed application environment is followed by a restart of the web
container. That restart is owed from before the file is written until the
container is healthy on it. The renderer keeps `web-restart-pending.json` on
the tmpfs, naming the environment by salted digest.

Each run pays an owed restart, whether the render succeeded, failed or found
the vault unchanged, and only onto the file the mark names. A run that fails as
`web_unhealthy` keeps failing that way, hourly, until the container comes back.

An unchanged vault is not re-read item by item. The renderer keeps a root-only
state file on the tmpfs with the vault's content version and a salted digest of
every file it installed. When the version, the registry and the configuration
are unchanged and every installed file still matches, the hourly run costs two
requests and writes nothing. Any mismatch, a reboot, or 24 hours without a full
read forces one.

Failures are filed under one word each, in the journal line
(`event=secrets.render.failed class=...`), in the status document, and as the
exit status:

| Class | Exit | Meaning |
|---|---|---|
| `config` | 2 | the unit's configuration or the registry was refused |
| `host` | 3 | a directory, mount, lock file or destination was not as required |
| `busy` | 4 | another render holds the lock |
| `connect_unavailable` | 5 | no answer within the deadline, or the vault kept changing |
| `connect_denied` | 6 | Connect refused the token |
| `connect_response` | 7 | an answer was malformed, oversized, redirected or for another vault |
| `content` | 8 | what the vault holds was refused |
| `web_unhealthy` | 9 | the web container did not come back after its environment changed |
| `internal` | 1 | anything else |

## Where each secret lives

Controller credentials live at
`/run/severino-hq-secrets/controller-connections.json`: a root-owned 0400 file
in a root-owned 0700 directory. The directory must be a tmpfs mounted `noswap`.
The renderer checks the mount table and the filesystem itself, and refuses
anything else, a link above the directory included. Replacement uses a
same-filesystem rename.

The directory is never mounted into the web container; `/run/severino-hq` is
reserved for its doorbells. `SEVERINO_CONTROLLER_SECRET_DIR`, if overridden,
must be consistent across the renderer and all consumers, with matching
systemd write permissions. A `SEVERINO_CONTROLLER_ENV` override is refused.

The document's schema is emitted from the connection shapes HQ's registry
declares (`controller/api/hq-connections.openapi.json`). Both the renderer and
the controller use the Go types generated from it (`controller/connections`).

A connection states its ref, the provider it is for, whether HQ may change
things through it and where its credential is kept. It holds its settings
under the one shape it arrived in.

Each of these is an error on both sides: an unknown or repeated field, another
`schema_version`, a connection with no shape or two, a setting its shape does
not declare, a missing required setting, or a second value.

```json
{
  "schema_version": 2,
  "connections": [
    {"ref": "example", "provider": "example", "manages": false,
     "store": {"vault": "example-vault", "item": "example-item"},
     "api_token": {"api_token": "...", "url": "https://api.example.com"}}
  ]
}
```

The document's version is part of what makes a render current, so a renderer
that writes another version rewrites the document on its first run and never
leaves the controller one it refuses.

The launcher copies the document into the run's directory on the same tmpfs,
gives it to the controller's account with mode 0400, bind-mounts it read-only
and passes `HQ_CONTROLLER_CONNECTIONS`, its path. The controller refuses a
document that is not its own account's private, single-link regular file, and
a connection set in its environment.

No connection value is an environment variable, so `docker inspect` and the
container's on-disk configuration show none. Docker administrators and host
root can still read the mount.

The application environment keeps its name, format and place
(`web/severino_hq_env`, shell-quoted `KEY='value'` lines) and its inode, for
the existing Docker bind mount. Its update is in place. The install as a whole
is not a multi-file transaction: an interruption can leave mixed or partial
files. A destination that is not a regular single-link file owned by the web
account is refused, and nothing is written through it.

SSH identities and signing keys are written beside the document, in `ssh/`:
`<ref>` and `<ref>.pub`, `<ref>.key` and `<ref>.key.pub`, and `known_hosts`.
Halves are parsed and proven to be one key before anything is written. An
identity is written in OpenSSH format, a signing key as PKCS#8 for `openssl`.

`hq-host install` installs the renderer's units first and alone, starts a
render, and checks the connections document before anything that reads it.
A failed install restores the prior units; a deploy's rollback restores the
prior tree.

## Status document

After every run that held the lock, the renderer writes
`/run/severino-hq-secrets/status.json` (`secretstatus.Status` in
`controller/secretstatus/status.go`). It holds no secret, no vault or item name and
no connection ref:

```json
{
  "schema_version": 1,
  "last_attempt": {"at": "2026-01-01T00:00:00Z", "outcome": "current"},
  "last_success": {
    "at": "2026-01-01T00:00:00Z", "rendered_at": "2025-12-31T23:00:00Z",
    "content_version": 42, "attribute_version": 3,
    "counts": {"items_read": 20, "connections": 9, "app_variables": 31,
               "identities": 2, "signing_keys": 1}
  },
  "connect": {"read_at": "2026-01-01T00:00:00Z", "version": "1.8.1",
              "dependencies": [{"service": "sync", "status": "ACTIVE"}]}
}
```

`outcome` is `rendered`, `current` or `failed`; a failure carries its class in
`failure` and leaves `last_success` as it was. `connect` is what `/health`
reported, reduced to short words. Every string in the document besides its
times is one short word (`WordPattern`).

`controller/secretstatus` imports nothing of the renderer or the controller.
The renderer writes the document and the controller reads it with the same
strict decoder (`secretstatus.Decode`). It refuses an unknown or repeated
member, another `schema_version`, an outcome that is not one of the three, or a
string that is not a short word.

### How HQ reads it

The directory is root-only, so the document takes the path the tailnet and
firewall readings take. `hq-host controller` copies it into the run's private
directory and gives the copy to the controller's account with mode 0400. It
bind-mounts it read-only at `/run/severino-hq/render-status/<name>.json`, and
passes `SEVERINO_RENDER_STATUS`, a comma-separated list of `<name>=<path>`
pairs.

The name is a fixed word the launcher chooses per renderer (`hq` for this
one), and is all HQ is told of where a document came from. A name is passed
whether or not its document exists.

The controller reports each as one record of the `host.render_status` reading:

| Record | Holds |
|---|---|
| The renderer's name | always |
| `state` `read` | the document |
| `state` `unreadable` | one reason word: `missing`, `unreadable`, `oversized`, `invalid` |

A document that cannot be read is still a record, so HQ can say which renderer
went quiet. HQ stores it through a schema that admits only the fields above,
and stores any string that is not a short word as `unreadable`.

What is mounted holds no secret, and the mount adds no way to write. The
controller receives a copy it cannot change, of a file only root can write,
from a directory it never sees. A local account that could write the document
is already root.

The document is reported to HQ as data and decides nothing there. The worst a
forged one can do is raise or hide the findings below.

`hq/platform/application/render_status_findings.py` raises, on the machine the
controller runs on:

| Finding | When | Severity |
|---|---|---|
| `render-failing` | the last run failed; names the class | attention; serious once the last success is past the allowance |
| `render-stale` | the last recorded run did not fail, and nothing had confirmed the files current within the allowance, or read them in full within a day and the allowance, when the reading was taken. The renderer is not running, or fails before it takes the lock and so records nothing | serious |
| `connect-sync-stalled` | Connect's `sync` dependency is not `ACTIVE`, or is not reported | attention |
| `render-status-unread` | the document is missing or was refused, or the reading is older than the slowest the controller ever reads; then it says when it was read and, while the bridge refuses the controller's sweep, why | attention |

The allowance is three runs at their latest, derived from the timer's interval
and randomized delay (`deploy/systemd/severino-hq-secrets.timer`). So a
renderer two runs behind is raised as the third falls due. A full read is
forced every 24 hours (`FullEvery`), so it is overdue that allowance later.

Both are measured against when the reading was taken, never against the
present moment, so a quiet stretch between reads raises nothing. A reading
older than the slowest the controller ever reads says nothing of how the
renderer stands now, so no rule judges it: `render-status-unread` says when it
was read instead (`reading_facts.unjudged`). A controller that cannot start
because its secrets are gone raises that, beside the controller's own findings.

Each finding names the command to run on the machine for a renderer whose unit
HQ knows (`RENDERER_UNITS`).

## Connections

A connection is a vault item and nothing else. `connection_ref`, `projection`
and `env_prefix` on the item are what make it one, and the renderer reads them.
So no file in this repository names a connection.

Connection shapes are declared once, in
`hq/domains/control_plane/connection_shapes.py`. `manage.py bridge_contract`
emits `hq/config/controller-connections.json` from them, and the renderer
derives each connection's settings from it. Built-in 1Password fields may be
selected by stable ID; custom fields by their unique label, since 1Password
assigns an opaque ID per item.

The provider comes from the env prefix (`ADGUARD_*` is AdGuard), unless the
item carries a `provider` field, which lets two of a kind coexist
(`PORTAINER_HOME` and `PORTAINER_CLOUD`). A connection only observes unless
the item carries `manages` set to `1`; HQ adopts what a sweep finds only
through a connection that manages ([Derived Facts](Derived-Facts.md#adoption)).

On each pass the controller probes every connection it was handed and reports
what answered and what it can act on: the machines behind a Portainer, the
zones a DNS token may edit. HQ stores the report, not the credential, and
`/infrastructure/connections/` shows it. OAuth probes exchange the client
credential for a short-lived token and discard it at once.

The controller trusts internal provider TLS through the host trust store
(`/usr/local/share/ca-certificates`) or a deployment-provided
`HQ_CONTROLLER_CA_FILE`. Never disable TLS verification.

### SSH identities

Deployment identities are SSH Key items in the vault, generated by 1Password,
so no private key is created on or written to a host's disk. Each SSH
connection names its key item in an `identity` field.

The renderer writes the private half, the public half and a `known_hosts`
pinned from the connection's Ed25519 host key into `ssh/` on the tmpfs. It
refuses an item whose halves do not match, and installs the set with the
connections document as one generation.

Authorize each public key with the narrowest remote account or forced command
available ([Deployment](Deployment.md#remote-targets)). Rotating a key is
generating a new item, authorizing its public half, and pointing the
connection's `identity` at it.

### Sealing to the controller

The write-only form of a held secret: HQ seals a value with the controller's
public key, so once it is stored only the controller can read it. With no
public key, HQ refuses the upload and says so.

An uploaded certificate's private key is sealed this way, into a
`SealedSecret` row for its record and slot, which only `set_secret` writes.
The bridge's `material` action carries it still sealed, and the web never reads
it back.

- **The key.** HPKE (RFC 9180) with ML-KEM-768+X25519, HKDF-SHA256 and
  AES-256-GCM.
- **The seed.** Before HQ first starts, `hq-host` writes a 32-byte seed as
  root into `/opt/apps/severino-hq/secrets/seal/`, named `<key id>.seed` and
  owned by the controller's account. A deploy does this before it restarts HQ,
  and an install in its preflight pass. It writes a seed only when none is
  there, and never replaces one.
- **The public key.** Each of those steps, and every controller pass, derives
  the public key from the seed and writes it, as root's file, to
  `/var/lib/severino-hq/seal/public.key`.
- **Who holds what.** The controller's container mounts the seed directory
  read-only. The web container never mounts it. `hq-host run-web` binds the
  public key into the web read-only (`SEVERINO_SEAL_PUBLIC_KEY_FILE`), and
  only when root owns the file and its directory and no other account can
  write either. Without the file, HQ seals nothing and says so.
- **Why root's file.** The web and the controller run as one account. So a key
  the web could write, or one sent over the bridge, would let the web choose
  the key it seals to.
- **A sealed value** is `hq2.<key id>.<ciphertext>`. The key id is the first 8
  hex digits of SHA-256 over the public key, so a rotated key can be kept for
  opening. The value is bound to its resource and slot through HPKE's info,
  `hq-seal/v1|<resource key>|<slot>`, so a ciphertext copied to another place
  does not open. `controller/internal/sealing` owns the format, and
  `hq/platform/core/secrets.py` agrees with it. Both suites' tests hold that
  agreement on the vector in `controller/runtime/testdata/seal/`.
- **Losing the seed loses every sealed value.** The nightly archive holds
  it, encrypted only to post-quantum recipients kept off the host
  ([Backup](Backup.md)), so a lost disk is restored with the database.

## Minting observer credentials

HQ holds no credential that can create tokens. When a connection lacks a
permission, is refused, or nears expiry, its row and finding show one command
for the operator's machine, for example:

```sh
CLOUDFLARE_BOOTSTRAP_TOKEN='op://Example Bootstrap/cloudflare/credential' \
  op run -- mise run mint:cloudflare --account 0123abcd \
  --store 'op://Example Vault/exampleitem01/credential'
```

`op run` resolves the bootstrap reference (Touch ID); the task checks the
connection item carries the field, mints a token with every permission in
`tools/cloudflare-observer-permissions.txt`, and pipes the secret into
`op item edit` as the item's JSON template. The secret reaches no argument,
file or output. The controller reads it on its next render.

The command is derived: the account from the readings, the vault and item from
the renderer (`<PREFIX>_STORE_VAULT`, `<PREFIX>_STORE_ITEM`), and the bootstrap
from the connection item's optional `bootstrap` field, an `op://<vault>/<item>`
reference shaped like the connection. The renderer refuses a bootstrap in the
vault it reads, because a reader of that vault could then mint.

Two tasks mint read-only credentials, both over `tools/credentials.py`. Both
take the bootstrap credential from the environment. Without `--store` they print what they would create and
create nothing: a secret goes into 1Password and nowhere else.

- `mise run mint:cloudflare` needs a bootstrap token with API Tokens
  Edit (API Tokens Write in the API). It resolves the names in `tools/cloudflare-observer-permissions.txt`
  and creates a user-owned token with an expiry and optional client IP ranges.
  `--list-groups` prints the current names.
- `mise run mint:tailscale` creates an OAuth client with the scopes in
  `tools/tailscale-observer-scopes.txt`, all `:read`. The bootstrap needs the
  `oauth_keys` scope: an API access token, or an OAuth client given as
  `TAILSCALE_BOOTSTRAP_CLIENT_ID` and `TAILSCALE_BOOTSTRAP_CLIENT_SECRET`.

Both lists are the `requires` of every reading for that provider plus
`hq.domains.control_plane.credential_reads`, and a test fails when they differ.

`mise run wire-pocket-id` writes Pocket ID's permission list for HQ's API the
same way, over `tools/pocket_id.py`: the operator's admin key comes from the
environment (`POCKET_ID_API_KEY`, through `op run`) and travels only in Pocket
ID's `X-API-Key` header. HQ stores none ([API](API.md#setting-up-a-client)).

## Why it is built this way

These are the constraints behind the renderer's shape, so a later change does
not remove a line that is load-bearing.

**There is one backend.** `OP_CONNECT_HOST` and `OP_CONNECT_TOKEN` take
precedence over `OP_SERVICE_ACCOUNT_TOKEN` everywhere inside `op`, and a process
holding both uses Connect, which is read-only. The renderer does not run `op`
and refuses a token in its environment, so the two cannot be confused.
The publisher is the only `op` caller left, and it is given one token.

**The credential is named for its consumer.** A host may run
more than one renderer, and `LoadCredentialEncrypted=` with a shared name means
two units overwrite each other's credential.

**The loopback rule is in the dialer.** An endpoint check is a string
comparison someone can loosen. The dialer's check is on the address the socket
is about to connect to, after resolution.

**The lock keeps one read whole.** Two renderers interleaving their installs is how
a host ends up holding files from two different reads of the vault.

**Installs preserve the inode deliberately.** Replacing it breaks single-file
bind mounts: the running container keeps the old file indefinitely. The cost is
that this is not an atomic swap, as stated above. A generation-directory layout
would make it transactional and requires moving the mounts first.

**Each validator has a failure it stops.** Each rejects a specific way a
render can succeed while producing something unusable: an unresolved reference,
an empty file, a variable with no value, a missing required variable, or a
vault that resolves no connection after holding one.

**The skip is verified.** A matching content version only skips
the read when every installed file still matches its recorded digest. The state
lives on the tmpfs, so the first run of every boot is a full read.

**An identity is compared as a key.** Its bytes cannot be: the OpenSSH encoding holds
random check bytes, so the same key encodes differently on every render.

**The certificate publisher runs `op`.** 1Password's Go SDK runs a
closed-source WebAssembly core in process, is pre-1.0, and would put a vault
client inside the image the web container shares; lending the host's `op` to
one run avoids all three. The SDK at 1.0, or file upload in Connect, would
change this.

## Checking the renderer on a host

The Go tests run against an in-process Connect and cannot show these.

1. **Connect after a restart.** Restart Connect, start the unit, and read the
   journal: `secrets.connect.ready attempts=N` shows the retries.
2. **Key format.** `openssl pkey -in <ref>.key -noout` accepts a rendered
   signing key.
3. **No credential in the container's configuration.** While a controller run
   is up, `docker inspect $(docker ps -q --filter label=severino-hq.role=controller) --format '{{json .Config.Env}}'`
   lists only paths, names and the run nonce.

## References

- [Connect CLI operations](https://www.1password.dev/connect/cli)
- [Connect REST API](https://www.1password.dev/connect/api-reference)
- [Connect security model](https://www.1password.dev/connect/security)
- [Encrypted systemd credentials](https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html)
- [Tailscale grants](https://tailscale.com/docs/reference/syntax/grants)
