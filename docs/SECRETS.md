# Secret delivery

Connect is a local read path for secrets already intended for the host. It is
not a new ingress for applications, a replacement for disk encryption, or a
boundary against host root. The certificate publisher uses a separate cloud
service account because Connect's file API does not support uploads.

## Bootstrap and ownership

Keep recovery copies of Connect credentials and reader tokens in an
operator-only 1Password vault that this Connect installation cannot access.
Provision the host over authenticated administrative SSH with host identity
verification. Transfer secret bytes through stdin into `systemd-creds encrypt`;
never put them in command arguments, shell history, CI output, or this repository.
Do not fetch a bootstrap token from Connect itself or require cloud access on
every boot. GitHub Actions does not need the runtime token.

Store each encrypted credential under `/etc/credstore.encrypted/`, root-owned
and mode 0600. Its embedded credential name must match the unit's
`LoadCredentialEncrypted=` name. systemd supplies the plaintext runtime file in
`CREDENTIALS_DIRECTORY`. Provision Connect's server credential and each client
token separately. Reader tokens have read-only access to the necessary vaults.

Use TPM-backed credentials where the actual host supports them and test recovery
after firmware and boot-policy changes. Host-key-only encryption cannot protect
against theft of a disk containing both ciphertext and the host key. Disk and
backup encryption remain separate requirements. Running root can obtain runtime
secrets with either approach.

The certificate publisher's service-account token is an item in the vault the
reader token reads, projected like any other connection (`service_account`).
A holder of the reader token can therefore read it, and that token is write
authority over the vault the publisher writes to. This is the operator's
accepted decision: every credential the controller uses is one item in one
vault, delivered one way. Credentials that can mint other credentials are the
exception and stay out of that vault; see "Minting observer credentials".

## Network and execution boundaries

- Connect binds only to IPv4 loopback. Do not publish it through a reverse proxy,
  Tailscale Serve/Funnel, LAN listener, or a tailnet address.
- Connect is published on a loopback port below 1024. The loopback rule proves
  the address, not who is listening: a port any account may bind could be taken
  while Connect is down, by any local process or a container on the host's
  network, and the reader token sent to whoever took it. Only root can listen
  below 1024, so only root's Connect can answer there. The renderer refuses an
  endpoint on any other port, its dialer refuses to connect to one, and it
  refuses to run on a host whose `net.ipv4.ip_unprivileged_port_start` is not
  above the port. There is no setting that relaxes this.
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

Connect needs outbound cloud synchronization. Cached availability does not mean
revocation is instantaneous during a disconnection. Monitor synchronization age
separately from successful cached reads. Define an acceptable stale-data window
per consumer and an emergency local shutdown procedure.

## Renderer behavior

`hq-secrets` (`controller/cmd/hq-secrets`) is a static Go binary. Root runs the
copy in the root-owned tree, `/usr/local/lib/severino-hq/deploy/bin/hq-secrets`,
which the image build compiles and `root-tree.sha256` covers. It holds no
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
readiness is the authenticated vault listing, never `/health`. No answer, a
timeout and a 5xx are retried with backoff under one deadline
(`-connect-timeout`, 75 seconds); a refused token and a malformed answer are
final. The unit's `TimeoutStartSec` is the outer bound.

One render is one consistent read: the vault's content version is read before
and after the items, and an item's version is checked against its listing. A
vault edited mid-read is read again; one that never holds still is refused.
Archived and deleted items are not rendered.

The renderer rejects failed discovery, a vault that does not resolve to exactly
one, duplicate connection metadata, refs and prefixes, invalid names, an
unknown projection, a required field that is missing or present twice, and
values containing control characters. The connection registry
(`hq/config/controller-connections.json`) defines shapes and is read strictly;
the vault remains the connection inventory. A render that resolved no
connection, an empty application environment, or one with fewer than fifteen
variables is a failure, not an empty success.
In the application environment a carriage return or a variable set
twice is refused, because readers would disagree about the value. A
connections document larger than the controller reads is refused as content,
so the last good one stays. An SSH host or user that
could read as an option, and an `env_prefix` in a namespace the controller's
own settings use (`HQ`, `SEVERINO`, `DJANGO`, `PYTHON`, `OP`, `SSL`, `LC`), are
refused. A bootstrap reference is compared to the vault's
names without regard to case or surrounding spaces. `known_hosts` names a host
as ssh looks it up: bare on port 22, `[host]:port` otherwise.

Refresh takes an exclusive lock on the runtime tmpfs, checks the host, and only then reads Connect.
Everything is read and validated in memory, and what is renamed into place is
staged on the private tmpfs, before any installed file is touched. Retrieval or
validation failure preserves the previous files. A staging directory a killed
run left is removed by the next run, under the lock.

Every installed file, the application environment included, is written under
the lock the launcher holds shared while it copies them, taken before the
first write. A changed application environment is followed by a restart of the
web container, and that restart is owed from before the file is written until
the container is healthy on it: the renderer keeps `web-restart-pending.json`
on the tmpfs, naming the environment by salted digest. Each run pays an owed
restart, whether the render succeeded, failed or found the vault unchanged, and
only onto the file the mark names. A run that fails as `web_unhealthy` keeps
failing that way, hourly, until the container comes back.

An unchanged vault is not re-read item by item. The renderer keeps a root-only
state file on the tmpfs with the vault's content version and a salted digest of
every file it installed. When the version, the registry and the configuration
are unchanged and every installed file still matches, the hourly run costs two
requests and writes nothing. Any mismatch, a reboot, or
24 hours without a full read forces one.

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
in a root-owned 0700 directory. The directory must be a tmpfs mounted `noswap`;
the renderer checks the mount table and the filesystem itself and refuses
anything else, a link above the directory included. Replacement uses a
same-filesystem rename. The directory is never mounted into the web container;
`/run/severino-hq` is reserved for its doorbell.
`SEVERINO_CONTROLLER_SECRET_DIR`, if overridden, must be consistent across the
renderer and all consumers, with matching systemd write permissions. A
`SEVERINO_CONTROLLER_ENV` override is refused.

The document's shape is declared once, in `controller/connections`, and both
the renderer and the controller use that type. An unknown or repeated field, a
wrong `schema_version`, or a second value is an error on both sides:

```json
{
  "schema_version": 1,
  "connections": [
    {"ref": "example", "prefix": "EXAMPLE",
     "values": {"CONNECTION_REF": "example", "API_TOKEN": "..."}}
  ]
}
```

The launcher copies the document into the run's directory on the same tmpfs,
gives it to the controller's account with mode 0400, bind-mounts it read-only
and passes `HQ_CONTROLLER_CONNECTIONS`, its path. The controller refuses a
document that is not its own account's private, single-link regular file, a
setting that is already in its environment, and a connection set in the
environment beside it. No connection value is an environment variable, so
`docker inspect` and the container's on-disk configuration show none. This is
not protection against Docker administrators or host root, who can read the
mount.

The application environment keeps its name, format and place
(`web/severino_hq_env`, shell-quoted `KEY='value'` lines) and its inode, for
the existing Docker bind mount. Its update is in place, and the install as a
whole is not a multi-file transaction: an interruption can leave mixed or
partial files. A destination that is not a regular single-link file owned by
the web account is refused, not written through. The MCP token file earlier
releases kept is removed: nothing accepts one.

SSH identities and signing keys are written beside the document, in `ssh/`:
`<ref>` and `<ref>.pub`, `<ref>.key` and `<ref>.key.pub`, and `known_hosts`.
Halves are parsed and proven to be one key before anything is written. An
identity is written in OpenSSH format, a signing key as PKCS#8 for `openssl`.

The controller installer installs and reloads the renderer unit from the
root-owned image copy before refreshing secrets. Failed activation restores
the prior renderer unit; deployment rollback restores the prior tree. After
activation, the installer removes what earlier releases left: any controller
credential under `/run/severino-hq`, and the shell renderer's environment file
on the tmpfs. Copies on disk or in backups are outside its reach and need
separate cleanup and credential rotation.

## Status document

After every run that held the lock, the renderer writes
`/run/severino-hq-secrets/status.json` (`secrets.Status` in
`controller/secrets/status.go`). It holds no secret, no vault or item name and
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
reported, reduced to short words. A reader should treat an old `last_success.at`,
a `failed` attempt, or a `sync` status other than `ACTIVE` as stale secrets.
Nothing in HQ reads this document yet: the directory is root-only, so the
natural path is the one the tailnet and firewall readings take, a file the
launcher mounts into the controller for a provider to report.

## Minting observer credentials

HQ holds no credential that can create tokens. When a connection lacks a
permission, is refused, or nears expiry, its row and finding show one command
for the operator's machine, for example:

```sh
CLOUDFLARE_BOOTSTRAP_TOKEN='op://Operator Vault/Cloudflare bootstrap/credential' \
  op run -- ./scripts/mint-cloudflare-token.sh --account 0123abcd \
  --store 'op://Example Vault/exampleitem01/credential'
```

`op run` resolves the bootstrap reference (Touch ID); the script checks the
connection item carries the field, mints a token with every permission in
`scripts/cloudflare-observer-permissions.txt`, and pipes the secret into
`op item edit` as the item's JSON template. The secret reaches no argument,
file or output. The controller reads it on its next render.

The command is derived: the account from the readings, the vault and item from
the renderer (`<PREFIX>_STORE_VAULT`, `<PREFIX>_STORE_ITEM`), and the bootstrap
from the connection item's optional `bootstrap` field, an `op://<vault>/<item>`
reference shaped like the connection. The renderer refuses a bootstrap in the
vault it reads, because a reader of that vault could then mint.

## Why it is built this way

These are the constraints behind the renderer's shape, so a later change does
not remove a line that is load-bearing.

**There is one backend.** `OP_CONNECT_HOST` and `OP_CONNECT_TOKEN` take
precedence over `OP_SERVICE_ACCOUNT_TOKEN` everywhere inside `op`, and a process
holding both uses Connect, which is read-only. The renderer no longer runs `op`
and refuses a token in its environment, so the two can no longer be confused.
The publisher is the only `op` caller left, and it is given one token.

**The credential is named for the consumer, not the protocol.** A host may run
more than one renderer, and `LoadCredentialEncrypted=` with a shared name means
two units overwrite each other's credential.

**The loopback rule is in the dialer.** An endpoint check is a string
comparison someone can loosen. The dialer's check is on the address the socket
is about to connect to, after resolution.

**The lock is not about speed.** Two renderers interleaving their installs is how
a host ends up holding files from two different reads of the vault.

**Installs preserve the inode deliberately.** Replacing it breaks single-file
bind mounts: the running container keeps the old file indefinitely. The cost is
that this is not an atomic swap, as stated above. A generation-directory layout
would make it transactional and requires moving the mounts first.

**The validators are not defensive padding.** Each rejects a specific way a
render can succeed while producing something unusable: an unresolved reference,
an empty file, a variable with no value, or zero connections.

**The skip is verified, not assumed.** A matching content version only skips
the read when every installed file still matches its recorded digest. The state
lives on the tmpfs, so the first run of every boot is a full read.

**An identity is compared as a key, not as bytes.** The OpenSSH encoding holds
random check bytes, so the same key encodes differently on every render.

**The certificate publisher still runs `op`.** 1Password's Go SDK
(`onepassword-sdk-go` v0.4.1) was evaluated for it on 2026-10-04 and not
adopted. It builds without cgo and can attach files, but it embeds a 9.5 MB
closed-source WebAssembly core and runs it in process (extism on wazero): a
minimal program linking it is 22.7 MB against 9.4 MB for the whole controller,
client start-up took 2.8 seconds and 173 MB of memory before any request, its
errors are strings apart from two types, it adds ten modules to the build, and
its API is pre-1.0. It would also put a vault client inside the image the web
container shares, which lending the host's `op` to one run avoids. Revisit when
the SDK reaches 1.0 or Connect gains file upload.

## Cutover gates

1. Inventory consumers, vault scope, read/write authority, credential owner,
   destination, expiry, and recovery procedure using metadata only. Record real
   identifiers in private operational documentation.
2. Remove human, signing-authority, remote-host, and bootstrap credentials from
   the proposed sync scope. The publisher's token stays in the vault, as above.
3. Validate the reader token and warm every required item. An unauthenticated
   health endpoint is insufficient evidence of usable cached secrets.
4. Install a host-specific version of
   `deploy/systemd/severino-hq-secrets-connect.conf.example`: Connect on a
   loopback port below 1024, the unit requiring Connect and ordered after it
   without requiring cloud readiness. Validate the effective systemd unit.
5. Compare rendered results privately without printing values. Exercise denied
   tokens, missing items, malformed responses, unavailable Connect, unchanged
   refreshes, and writer operation.
6. Test a full host boot with WAN unavailable and an independently armed
   recovery mechanism. Verify DNS, systemd ordering, firewall startup, cached
   reads, and application readiness. A container restart alone is insufficient.
7. Observe a defined soak period with off-host logging, collection heartbeats,
   sync-age monitoring, and renderer failure alerts. Then revoke the retired
   reader service account and remove its local credential. Rollback during the
   soak is an explicit operator action, never a silent authentication fallback.

Provisioning, live cutover, credential revocation, and destructive outage tests
are separate operational steps. Local tests do not establish that these gates
have passed on a deployed host.

## First deploy from the shell renderer

The deploy that replaces the shell renderer is started by the host's previous
`deploy-image.sh`. The order of events:

1. The previous deploy script stops the controller and content timers, pulls
   the image, and takes the new release's compose file and
   `severino-hq-sync-scripts` out of it.
2. It starts the new web container and waits for it to be healthy, then backs
   up the root-owned tree and the host's sync program.
3. It runs the new release's sync program. That replaces
   `/usr/local/lib/severino-hq` in one swap, and `deploy/bin/hq-secrets`
   arrives with it. The previous sync program never runs for this release.
4. It runs the synced tree's `install-controller.sh`, which installs
   `severino-hq-secrets.service` and the shipped
   `severino-hq-secrets.service.d/10-root-owned-exec.conf` over the host's
   copy (the one whose `ExecStart` named `refresh-secrets.sh`), reloads
   systemd, and starts the unit. This is the first run of the Go renderer.
5. The installer requires the connections document, runs the controller
   preflight, installs and enables the remaining units, and removes the shell
   renderer's environment file from the tmpfs.

Between steps 3 and 4 the installed unit still names `refresh-secrets.sh`,
which is gone. If the hourly timer or the controller's path unit fires in that
window, the unit fails to start and nothing is written; the installer's start
in step 4 is unaffected. A controller run between the sync and the first
successful render is refused by the launcher, which requires the document.

One change is required before the deploy: Connect must be on a loopback
port below 1024. The renderer refuses the endpoint otherwise (`config`, exit
2), the previous files are kept, and the deploy rolls back as described below.
Make the change while the shell renderer is still live, which accepts any
loopback port, in this order, so nothing is broken in between:

1. Publish Connect on `127.0.0.1:<port below 1024>` beside or in place of its
   current port, and restart it.
2. Move the firewall guard that admits only uid 0 to Connect to the new port
   (keep the old rule until step 4 if both ports are published).
3. Set `OP_CONNECT_HOST=http://127.0.0.1:<port>` in the host's drop-in, change
   its `Wants=` on the Connect unit to `Requires=`, and `systemctl daemon-reload`.
4. `systemctl start severino-hq-secrets.service` and confirm the shell
   renderer succeeds on the new port. Then stop publishing the old port and
   remove its firewall rule.
5. Confirm `sysctl net.ipv4.ip_unprivileged_port_start` is above the port
   (1024 unless the host lowered it).
6. Deploy.

Nothing else in the host-owned drop-ins needs an edit. The renderer reads
`SEVERINO_SECRETS_VAULT`, `SEVERINO_ENV_ITEM`, `SEVERINO_CONNECT_CREDENTIAL`,
`OP_CONNECT_HOST` and the `LoadCredentialEncrypted=` line as they are;
tolerates `SEVERINO_SECRETS_BACKEND=connect`; and ignores
`SEVERINO_MCP_SECRET_REF` and a `RuntimeDirectory=severino-hq-op` line. After
a healthy deploy, remove those three, and any `TimeoutStartSec=` shorter than
the unit's. It refuses a `SEVERINO_SECRETS_BACKEND` other than `connect`, an
`OP_CONNECT_TOKEN` or `OP_SERVICE_ACCOUNT_TOKEN` variable, and a
`SEVERINO_CONTROLLER_ENV` override, and an endpoint that is not IPv4 loopback
on a port below 1024.

If the first render or the preflight fails, the installer restores the
previous unit and drop-in, and the previous deploy script restores the
previous image, the previous tree with `refresh-secrets.sh` in it, the
previous sync program and the timers. The shell renderer's environment file is
still on the tmpfs, because it is removed only in step 5, so the restored
launcher keeps working and the hourly timer renders as before. Files the Go
renderer wrote before a later step failed (`controller-connections.json`,
`status.json`, its state file) stay on the tmpfs until a reboot; nothing in
the previous release reads them.

## Proving the renderer on a host

The Go tests run against an in-process Connect and cannot show these. Probe
each before the live unit is trusted with it.

1. **The hardening, in a transient unit.** Copy the binary out of the image and
   run it with the unit's properties and the host's own drop-in values, against
   a scratch tmpfs mounted `noswap`, so nothing live is written:
   `systemd-run --wait --pipe -p LoadCredentialEncrypted=... -p ProtectSystem=strict -p ReadWritePaths=... -p CapabilityBoundingSet='CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE' -p RestrictAddressFamilies='AF_UNIX AF_INET' -p IPAddressDeny=any -p IPAddressAllow=127.0.0.1 -p SocketBindDeny=any -p SystemCallFilter=@system-service -p SystemCallErrorNumber=EPERM -p SystemCallArchitectures=native -p ProtectProc=invisible -p PrivateIPC=yes -p MemorySwapMax=0 -p MemoryMax=512M -p TasksMax=128 -p LimitCORE=0 -p MemoryDenyWriteExecute=yes ...`
   with `SEVERINO_CONTROLLER_SECRET_DIR` and `SEVERINO_HQ_SECRET_DIR` pointed at
   the scratch directories. Each directive the shell renderer's unit did not
   have is one to remove and retry if the probe fails:
   - `CapabilityBoundingSet` (the in-place rewrite of a 0400 file the web
     account owns);
   - `RestrictAddressFamilies` without `AF_INET6`;
   - `IPAddressDeny=any` with `IPAddressAllow=127.0.0.1`;
   - `SocketBindDeny=any`;
   - `SystemCallFilter=@system-service` with `SystemCallErrorNumber` and
     `SystemCallArchitectures`;
   - `ProtectProc=invisible` and `PrivateIPC=yes`;
   - `MemorySwapMax=0`, `MemoryMax=512M`, `TasksMax=128`, `LimitCORE=0`. Run
     the probe with a changed application variable too, so the docker CLI's
     restart runs under the memory and task bounds.
   The renderer also reads `/proc/sys/net/ipv4/ip_unprivileged_port_start`
   under `ProtectKernelTunables=yes`, which leaves `/proc/sys` readable;
   confirm the probe gets past that read.
2. **Connect after a restart.** Restart Connect, start the unit, and read the
   journal: `secrets.connect.ready attempts=N` shows the retries. If Connect
   answers a locked first request with 401 or 403 rather than no answer or 5xx,
   the run ends as `connect_denied`; that is the one classification this
   repository could not confirm.
3. **Key formats over REST.** An SSH Key item's `private key` field is expected
   as PKCS#8 or OpenSSH PEM. Confirm `ssh -i` accepts a rendered identity and
   `openssl pkey -in <ref>.key -noout` a rendered signing key.
4. **The application environment is unchanged.** Before the upgrade:
   `sha256sum /run/severino-hq-secrets/web/severino_hq_env`. After the first
   render, the same digest means byte-identical and no web restart. A different
   digest with the same sorted lines means Connect returned the fields in
   another order than `op` printed them: harmless, one restart.
5. **No credential in the container's configuration.** While a controller run
   is up: `docker inspect $(docker ps -q --filter label=severino-hq.role=controller) --format '{{json .Config.Env}}'`
   lists only paths, names and the run nonce.
6. **Rollback.** Redeploy the previous image. Its installer restores the
   previous unit and tree, and its renderer rewrites the shell environment file
   on its first run. Nothing the Go renderer wrote is read by it.

## References

- [Connect CLI operations](https://www.1password.dev/connect/cli)
- [Connect REST API](https://www.1password.dev/connect/api-reference)
- [Connect security model](https://www.1password.dev/connect/security)
- [Encrypted systemd credentials](https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html)
- [Tailscale grants](https://tailscale.com/docs/reference/syntax/grants)
