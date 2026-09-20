# Secret delivery and Connect migration

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
`service-account` remains the compatibility default until host cutover;
`connect` loads `op_connect_token` from systemd and clears service-account
authentication. There is no automatic cloud fallback. The Connect endpoint must
be `http://127.0.0.1:PORT`.

Connect's CLI supports reads but not `op item list`.
`scripts/list-secret-items.sh` uses the REST listing endpoint for discovery,
resolves the vault uniquely, bounds requests, bypasses proxy configuration, and
does not follow redirects. Its bearer header travels through stdin rather than
argv. Individual item reads continue using `op`.

The renderer rejects failed discovery, duplicate connection metadata and
prefixes, invalid variable names, and controller values containing NUL or line
breaks. Values are shell-quoted before the launcher sources them. The connection
registry defines shapes; the vault remains the connection inventory.

Refresh takes an exclusive lock and stages all reads and validation in a private
directory before modifying installed files. Retrieval or validation failure
preserves all previous files. Cleanup targets only that invocation's directory.

**Installation is not a multi-file transaction.** Existing single-file Docker
bind mounts require preserving the inode, so updates are in place. Interruption
or disk failure during installation can still leave mixed or partial files.
Do not describe this as atomic rotation. A future generation-directory design
must migrate mounts and coordinate all readers before it can provide that
guarantee. The current tests prove failure before installation, not crash safety
during it.

The controller launcher currently forwards provider credentials as container
environment variables. Docker administrators can inspect those values. A
read-only credential mount reduces configuration exposure but does not protect
against Docker administrators or host root. Review this delivery boundary before
claiming credentials are absent from container metadata.

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
