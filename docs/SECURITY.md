# Severino HQ — security checklist

## Posture

- Single-user / very-small internal app.
- Tailscale-only network exposure. No path from the public internet.
- Tailnet-only is stated by the application, not only inherited from the
  network. The shipped `SEVERINO_TRUSTED_NETWORKS` default is Tailscale's IPv4
  and IPv6 ranges plus loopback — deliberately **not** RFC 1918. A LAN holds
  printers, televisions and guests; it is not a boundary anyone maintains, and
  a host firewall that is the only thing enforcing the rule is one `ufw
  disable` from silently admitting all of it.
- Django authentication required on **every** URL except `/accounts/login/`,
  `/accounts/logout/`, `/oidc/`, and `/static/`.
- No public registration. New users are created via `manage.py createsuperuser`
  or Django admin only.

## What v1 does for you out of the box

- `DEBUG=False` enforced when `DJANGO_SECRET_KEY` is set; missing key in
  production raises a startup error.
- `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` come from environment variables.
- `SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE` default ON in production,
  and where they are on the cookies are named `__Host-sessionid` and
  `__Host-csrftoken`. The prefix is enforced by the browser rather than by HQ:
  no other host or path under the domain can set a cookie of that name, so a
  session cannot be planted by a neighbour for HQ to read back.
- `SECURE_BROWSER_XSS_FILTER`, `SECURE_CONTENT_TYPE_NOSNIFF`, `X-Frame-Options:
  DENY`, `Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy:
  same-origin` and `Cross-Origin-Resource-Policy: same-origin` enabled. The
  static mount sets the last two itself, because it sits above the Django
  middleware that sets them everywhere else.
- `SECURE_PROXY_SSL_HEADER` is wired up when `DJANGO_BEHIND_TLS_PROXY=1`, and
  so is the redirect that keeps the plain port HQ binds from being a second
  front door. Only the healthcheck path is exempt, because it deliberately
  probes that port from inside the container's own network namespace.
- Forwarded identity is accepted only from the exact addresses in
  `SEVERINO_TRUSTED_PROXIES`. Keep this list to loopback when the reverse proxy
  is co-located. A Tailnet range belongs in `SEVERINO_TRUSTED_NETWORKS`, not in
  the proxy allowlist: a Tailnet peer is an admitted caller, not automatically
  an authority allowed to name some other caller.
- Identity systems may issue unrelated names for one person. Pocket ID can
  assert the association in its signed ID token: add a user custom claim named
  `tailscale_principal` with a string value such as `"operator@passkey"`.
  HQ binds that claim to the resulting OIDC session and compares it with the
  requesting device's Tailnet owner. HQ accepts exact equality or that signed
  association; it does not infer identity from similar usernames.
- The connection inspector joins the live request to cached Nginx Proxy Manager
  and Tailscale observations. It distinguishes the node serving HQ from the
  node whose daemon made the Tailnet observation, and calls the path an
  HQ-to-caller peering only when those independently resolve to the same node.
  The node keys, handshake age, endpoint and counters remain observer-relative;
  missing placement evidence stays visibly unverified.
- Django's native Content Security Policy middleware enforces same-origin
  assets, nonce-authorized scripts, no object embedding, and no framing.
  Application JavaScript is external; a regression test rejects inline scripts
  and event handlers before they can weaken the policy.
- The policy also requires Trusted Types, so assigning a string to `innerHTML`,
  `outerHTML`, `srcdoc` or a script URL throws instead of parsing — a DOM-based
  XSS sink cannot execute even if one is introduced, and `trusted-types 'none'`
  means no policy can be declared to opt back out. It costs nothing today
  because every dynamic node HQ builds uses `createElement`/`textContent`.
  Django admin's bundled jQuery cannot meet it, so `core.middleware`'s
  `AdminPolicyMiddleware` drops that one directive for `/admin/` and nothing
  else; a test asserts the relaxation stays that narrow.
- Violations are reported back. The policy carries `report-to` and `report-uri`
  pointing at `/csp-report/`, which records the directive, the blocked URI and
  the reporting address to the audit log — bounded body size, truncated fields,
  and one row per distinct complaint per hour. It is the only way HQ learns
  that a directive enforced in someone else's browser has stopped holding.
- Django 6.1's secure default rejects legacy cookies using the ambiguous
  pre-6.0.6 signing-salt derivation.
- HSTS on by default for a year, including subdomains. Preload stays opt-in:
  it is slow to undo and meaningless for a name the public internet cannot
  resolve.
- `LoginRequiredMiddleware` redirects anonymous users to login for every URL
  outside the small allowlist.
- Receipt files:
  - Stored at `SEVERINO_MEDIA_ROOT`, **outside the app code directory**.
  - Filenames are randomized (UUID), not user-supplied.
  - Storage's `base_url` is `None` — there is no public URL for these files.
  - The `receipts:file` view requires authentication, streams the file, sets
    `X-Content-Type-Options: nosniff` and `Cache-Control: private, no-store`,
    and audits the view.
- Uploads are content-type-filtered (`receipts/forms.py`) and size-capped
  (15 MB by default).
- Audit log on every create / update / delete (via signals), plus login,
  failed login, logout, upload, export, and import events.
- Request-user attribution uses an ASGI-safe context variable, preventing one
  concurrent request's identity from leaking into another request's audit row.
- Server-generated request IDs connect response headers to structured JSON
  access logs without recording query strings or request bodies.
- Anonymous liveness and readiness probes disclose only component state;
  readiness fails on database, migration, or writable-storage problems.
- SQLite is opened with `journal_mode=WAL`, `foreign_keys=ON`, and
  `transaction_mode=IMMEDIATE` for safer concurrent operation.
- Password validators require min length 12 and reject common/numeric-only
  passwords.
- Optional Pocket ID / OIDC SSO is supported. HQ authorizes membership in
  `SEVERINO_OIDC_ALLOWED_GROUPS` and links the identity to a Django user by
  `preferred_username`. Email matching remains an optional fallback; password
  login remains available as the break-glass path.
- The `/mcp/` Streamable HTTP endpoint is a separate security boundary:
  it accepts only a direct socket peer in Tailscale's IPv4/IPv6 ranges, checks
  an explicit Host allowlist, rejects browser Origins unless allowlisted, and
  requires a constant-time-checked bearer token of at least 32 characters.
  Forwarded client-address headers are never trusted. The container uses host
  networking so the ASGI server receives the real peer address instead of a
  Docker bridge address.

## Production checklist

- [ ] Generated a strong `DJANGO_SECRET_KEY` (≥ 50 random bytes).
- [ ] `DJANGO_DEBUG=0`.
- [ ] `DJANGO_ALLOWED_HOSTS` contains only your Tailnet hostname (+ `127.0.0.1`).
- [ ] `DJANGO_CSRF_TRUSTED_ORIGINS` matches the full origin you actually serve.
- [ ] The browser UI reaches port 8000 only through the reverse proxy. If host
      networking leaves Uvicorn on `0.0.0.0` so direct Tailnet MCP can preserve
      the real socket peer, host firewall and Tailnet policy permit that port
      only from the intended Tailnet principals; no public or LAN route admits
      it.
- [ ] Caddy / Nginx / Tailscale Serve terminates TLS.
- [ ] A co-located proxy is the only member of `SEVERINO_TRUSTED_PROXIES`
      (`127.0.0.1/32,::1/128`). `SEVERINO_TRUSTED_NETWORKS` contains loopback
      and Tailscale's IPv4/IPv6 ranges, with no RFC 1918 blanket allowance.
- [ ] If the Tailnet login and OIDC principal use different namespaces,
      the Pocket ID user has a `tailscale_principal` custom claim and the
      connection page reports `SSO-signed principal link` as its evidence.
- [ ] Nginx Proxy Manager's access list contains the two Tailnet `allow` rules.
      Do not add a second explicit `deny all`: NPM materializes its disabled
      final deny automatically, and HQ records that effective default from the
      provider observation.
- [ ] `DJANGO_BEHIND_TLS_PROXY=1`, `DJANGO_SESSION_COOKIE_SECURE=1`,
      `DJANGO_CSRF_COOKIE_SECURE=1`.
- [ ] `DJANGO_HSTS_SECONDS` is **not** left at `0`. The connection page's
      "There is one way in, and it is encrypted" layer reads the live setting
      and says so when it is off; a deployment that once set it to zero while
      TLS was being sorted out will otherwise keep telling browsers that plain
      HTTP is worth trying, indefinitely.
- [ ] The container runs with `read_only: true`, `cap_drop: ALL`,
      `no-new-privileges`, a pids limit and a memory limit. `/tmp` is the only
      writable path outside the volumes.
- [ ] The host image and the composed image are cosign-signed by this
      repository's own workflows, the composition verifies the host image
      before building on it, and the deploy verifies the composition before
      recreating the container.
- [ ] `SEVERINO_DATABASE_PATH`, `SEVERINO_MEDIA_ROOT`, `SEVERINO_EXPORTS_ROOT`
      live outside the app code directory and are writable only by the service
      user (`chmod 750`).
- [ ] The committed `severino-hq-backup.timer` is active, its archives are age
      encrypted, and off-host replication is monitored.
- [ ] Restore drill done once, and documented locally.
- [ ] Superuser created via `manage.py createsuperuser`; no shared accounts.
- [ ] If SSO is enabled, Pocket ID has an `admins` group and the HQ OIDC
      client callback is `https://hq.jseverino.com/oidc/callback/`.
- [ ] If SSO is enabled, `SEVERINO_OIDC_ALLOWED_GROUPS=admins` and
      `SEVERINO_OIDC_CLIENT_SECRET` is stored only in
      `/opt/apps/severino-hq/.env`.
- [ ] The Pocket ID HQ client has PKCE enabled; HQ uses S256 in addition to
      confidential-client authentication.
- [ ] Documentation index records carrying secrets are flagged
      `sensitivity=sensitive` or `restricted` (these are excluded from
      automated retrieval).
- [ ] The MCP token's source of truth is 1Password. Production mounts a
      validator copy through `SEVERINO_MCP_TOKEN_FILE_HOST`; the token is never
      placed in `.env` or the container environment.
- [ ] The app environment's source of truth is the 1Password `severino-hq env`
      item. Production mounts the rendered file through
      `SEVERINO_APP_ENV_FILE_HOST` and the entrypoint sources it; the on-host
      `.env` contains no secrets (only the two `*_FILE_HOST` paths).
- [ ] HQ stores exactly one class of secret: a certificate an operator generated
      themselves and asked HQ to install. It is sealed with
      `SEVERINO_SECRET_STORE_KEY`, which lives on the env item and not in the
      database; storing is refused outright when that key is absent, never
      downgraded to plaintext. The material is read only by the controller,
      through a bridge command of its own so it does not ride in the contract
      that `export` prints, and it appears in no serializer, no API response,
      and not in the reply to the upload that supplied it.
- [ ] Provider credentials remain outside the web container entirely. The
      controller report guard still rejects any status carrying a key named
      `private`, `secret`, `token`, `password`, or `credential`.
- [ ] The 1Password service account can read only the dedicated production
      vault. Its auth token is stored as a host-bound encrypted systemd
      credential; it is not readable by the service account from that vault.
      This VM has no usable TPM and its host credential key lives on the same
      unencrypted virtual disk, so offline disk/root compromise remains a
      documented residual risk until vTPM-backed disk protection is enabled.
- [ ] `severino-hq-secrets.timer` is enabled and its last service run
      succeeded. Rotation refreshes the validator atomically and restarts HQ
      only when the value changes.
- [ ] `SEVERINO_MCP_ALLOWED_HOSTS` contains only the direct Tailscale IP and/or
      MagicDNS hostname used by the MCP client.
- [ ] The MCP client connects directly to `http://<tailscale-host>:8000/mcp/`;
      it does not use the LAN/NPM browser route.
- [ ] The tailnet ACL permits the intended admin client to reach port 8000 and
      no broader identity than required.
- [ ] `SEVERINO_MCP_ENABLE_WRITES`, `SEVERINO_MCP_ENABLE_PRUNE`, and
      `SEVERINO_MCP_ENABLE_DELETES` are enabled only for the capabilities the
      MCP service account actually needs.
- [ ] The DNS-01 token is a distinct `Cloudflare DNS - HQ Controller` item with
      `connection_ref=cloudflare-dns-jseverino`; it has only Zone Read and DNS
      Edit on the four declared Severino zones and is not the D1 application
      token.
- [ ] No personal SSH private key is rendered for the controller. Dedicated
      Ed25519 identities are generated on `homelab-server`, remote host keys
      are pinned in the connection registry, and each public key is authorized
      only for its declared target and deployment role.

## What v1 deliberately does NOT do

- Talk to the public internet from the app server *with a credential*. The
  outbound calls HQ does make are narrow and named: Cloudflare D1 for contact
  submissions, the content index, and the two public-registry lookups behind
  `lookup.name` / `lookup.address`. Only the first two carry a token; the
  lookups carry none, which is why they are allowed to run in the web process
  rather than being queued through the controller like provider work.
- Run a WordPress plugin, customer portal, or public webhooks.
- Decrypt git-crypted Obsidian content. The vault stays separate.
- Store credentials, API tokens, or secrets in models. The documentation
  index is metadata-only.
- Expose arbitrary commands, Django shell, raw SQL, SSH, deployments, receipt
  file contents, or runbook bodies through MCP.
