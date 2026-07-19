# Severino HQ — security checklist

## Posture

- Single-user / very-small internal app.
- Tailscale-only network exposure. No path from the public internet.
- Django authentication required on **every** URL except `/accounts/login/`,
  `/accounts/logout/`, `/oidc/`, and `/static/`.
- No public registration. New users are created via `manage.py createsuperuser`
  or Django admin only.

## What v1 does for you out of the box

- `DEBUG=False` enforced when `DJANGO_SECRET_KEY` is set; missing key in
  production raises a startup error.
- `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` come from environment variables.
- `SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE` default ON in production.
- `SECURE_BROWSER_XSS_FILTER`, `SECURE_CONTENT_TYPE_NOSNIFF`, `X-Frame-Options:
  DENY`, `Referrer-Policy: same-origin` enabled.
- `SECURE_PROXY_SSL_HEADER` is wired up when `DJANGO_BEHIND_TLS_PROXY=1`.
- HSTS off by default; turn it on (`DJANGO_HSTS_SECONDS=31536000`) only after
  verifying TLS works end-to-end.
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
- [ ] App binds to `127.0.0.1:8000` (Docker) **or** to the Tailscale interface
      via the reverse proxy. Never to `0.0.0.0` on a public interface.
- [ ] Caddy / Nginx / Tailscale Serve terminates TLS.
- [ ] `DJANGO_BEHIND_TLS_PROXY=1`, `DJANGO_SESSION_COOKIE_SECURE=1`,
      `DJANGO_CSRF_COOKIE_SECURE=1`.
- [ ] `SEVERINO_DATABASE_PATH`, `SEVERINO_MEDIA_ROOT`, `SEVERINO_EXPORTS_ROOT`
      live outside the app code directory and are writable only by the service
      user (`chmod 750`).
- [ ] Backups configured (`scripts/backup.sh` from cron / a systemd timer).
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
      AI-safe references).
- [ ] The MCP token's source of truth is 1Password. Production mounts a
      validator copy through `SEVERINO_MCP_TOKEN_FILE_HOST`; the token is never
      placed in `.env` or the container environment.
- [ ] `SEVERINO_MCP_ALLOWED_HOSTS` contains only the direct Tailscale IP and/or
      MagicDNS hostname used by the MCP client.
- [ ] The MCP client connects directly to `http://<tailscale-host>:8000/mcp/`;
      it does not use the LAN/NPM browser route.
- [ ] The tailnet ACL permits the intended admin client to reach port 8000 and
      no broader identity than required.

## What v1 deliberately does NOT do

- Talk to the public internet from the app server. No outbound calls.
- Run a WordPress plugin, customer portal, or public webhooks.
- Decrypt git-crypted Obsidian content. The vault stays separate.
- Store credentials, API tokens, or secrets in models. The documentation
  index is metadata-only.
- Expose arbitrary commands, Django shell, raw SQL, SSH, deployments, receipt
  file contents, or runbook bodies through MCP.
