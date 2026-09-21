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

The writer credential must not be recoverable using a Connect reader token.
Keep it outside synced vaults and deliver it only to the writer's execution
boundary. Moving the token to a differently named item in the same readable
vault does not isolate it. Review every stored automation credential for this
same read-to-write escalation before granting a vault to Connect.

## Network and execution boundaries

- Connect binds only to IPv4 loopback. Do not publish it through a reverse proxy,
  Tailscale Serve/Funnel, LAN listener, or a tailnet address.
- Tailscale grants restrict administrative access to the host. They do not
  authorize processes on the host to use Connect. Local nftables rules enforce
  that boundary; test local unprivileged callers and container bridge paths,
  including direct access to the container address.
- Run Connect under a dedicated identity, with only its required credential and
  cache mounts. Audit Docker privileges, capabilities, socket access, restart
  ownership, resource limits, and image provenance separately.
- Only the root renderer holds the reader token. The web container receives its
  rendered application files, not Connect credentials. `op` receives its token
  in its short-lived process environment; this is not protection against root.
- The certificate writer explicitly removes inherited Connect authentication
  when invoking `op` with its service account. It still requires cloud access.

Connect needs outbound cloud synchronization. Cached availability does not mean
revocation is instantaneous during a disconnection. Monitor synchronization age
separately from successful cached reads. Define an acceptable stale-data window
per consumer and an emergency local shutdown procedure.

## Renderer behavior

`scripts/refresh-secrets.sh` selects `SEVERINO_SECRETS_BACKEND` explicitly.
There is no default: a missing, empty or unknown backend fails closed.
`service-account` must be explicitly configured for consumers that require it;
`connect` loads the credential named by `SEVERINO_CONNECT_CREDENTIAL` from
systemd and clears service-account authentication. Each consumer uses a distinct
credential name and a read-only token scoped to its required vaults. There is no
automatic cloud fallback. The endpoint must be `http://127.0.0.1:PORT`.

Connect's CLI supports reads but not `op item list`.
`scripts/list-secret-items.sh` uses the REST listing endpoint for discovery,
resolves the vault uniquely, bounds requests, bypasses proxy configuration, and
does not follow redirects. Its bearer header travels through stdin rather than
argv. Individual item reads continue using `op`.

The renderer rejects failed discovery, duplicate connection metadata and
prefixes, invalid variable names, and controller values containing NUL or line
breaks. Values are shell-quoted before the launcher sources them. The connection
registry defines shapes; the vault remains the connection inventory.

Refresh takes an exclusive lock and stages all reads on private tmpfs before
modifying installed files. Retrieval or validation failure preserves previous
files. Cleanup targets only that invocation's staging directory.

Controller credentials live at
`/run/severino-hq-secrets/severino_controller_env`: a root-owned 0400 file in a
root-owned 0700 directory. Readers reject unsafe permissions, symlinks and
non-tmpfs storage. Replacement uses a same-filesystem rename. The directory is
never mounted into the web container; `/run/severino-hq` is reserved for its
doorbell. `SEVERINO_CONTROLLER_SECRET_DIR`, if overridden, must be consistent
across the renderer and all consumers, with matching systemd write permissions.

Application and MCP files retain their inodes for existing Docker bind mounts.
Their updates are in place, not a multi-file transaction: an interruption during
installation can leave mixed or partial files.

The controller launcher currently forwards provider credentials as container
environment variables. Docker administrators can inspect those values. A
read-only credential mount would reduce configuration exposure but would not
protect against Docker administrators or host root.

Remove any legacy `SEVERINO_CONTROLLER_ENV` override before upgrading. The
controller installer installs and reloads the renderer unit from the root-owned
image copy before refreshing secrets. Failed activation restores the prior
renderer unit; deployment rollback restores the prior scripts. After activation,
the installer removes the old runtime credential before granting the web UID
doorbell ownership. Retired disk copies and backups require separate cleanup
and credential rotation.

## Why it is built this way

`scripts/lib/secrets.sh` is deliberately terse. These are the constraints behind
its shape, so a later change does not remove a line that is load-bearing.

**The backends are mutually exclusive by more than convention.**
`OP_CONNECT_HOST` and `OP_CONNECT_TOKEN` take precedence over
`OP_SERVICE_ACCOUNT_TOKEN` everywhere inside `op`. A process holding both uses
Connect, and Connect is read-only, so a writer then fails in a way that reads
like a permissions problem. `secrets_backend_select` clears the other side for
that reason rather than for tidiness.

**The credential is named for the consumer, not the protocol.** A host may run
more than one renderer, and `LoadCredentialEncrypted=` with a shared name means
two units overwrite each other's credential.

**`secrets_stage` sets a variable rather than printing one.** Calling it through
`$( )` would run it in a subshell, arming the cleanup trap in a shell that exits
immediately and leaving the staging directory behind on every run.

**The lock is not about speed.** Two renderers interleaving their installs is how
a host ends up holding files from two different reads of the vault.

**Installs preserve the inode deliberately.** Replacing it breaks single-file
bind mounts: the running container keeps the old file indefinitely. The cost is
that this is not an atomic swap, as stated above. A generation-directory layout
would make it transactional and requires moving the mounts first.

**The validators are not defensive padding.** Each rejects a specific way a
render can succeed while producing something unusable: an unresolved reference,
an empty file, or a variable with no value.

**POSIX `sh`, not bash.** These run under dash, so a bashism fails on a host
rather than in a test.

## Cutover gates

1. Inventory consumers, vault scope, read/write authority, credential owner,
   destination, expiry, and recovery procedure using metadata only. Record real
   identifiers in private operational documentation.
2. Remove human, signing-authority, remote-host, and bootstrap credentials from
   the proposed sync scope. Independently provision the publisher credential.
3. Validate the reader token and warm every required item. An unauthenticated
   health endpoint is insufficient evidence of usable cached secrets.
4. Install a host-specific version of
   `deploy/systemd/severino-hq-secrets-connect.conf.example`. Order after Connect
   without requiring cloud readiness. Validate the effective systemd unit.
5. Compare rendered results privately without printing values. Exercise denied
   tokens, missing items, malformed responses, unavailable Connect, unchanged
   refreshes, and writer operation with Connect variables inherited.
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

## References

- [Connect CLI operations](https://www.1password.dev/connect/cli)
- [Connect REST API](https://www.1password.dev/connect/api-reference)
- [Connect security model](https://www.1password.dev/connect/security)
- [Encrypted systemd credentials](https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html)
- [Tailscale grants](https://tailscale.com/docs/reference/syntax/grants)
