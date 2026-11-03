# Security

## Posture

- An internal app for its operator and the few people they let in, each
  holding a role ([Roles](#roles)).
- Reachable over Tailscale only, with no path from the public internet.
- HQ states the tailnet-only rule itself as well as inheriting it from the
  network. The shipped `SEVERINO_TRUSTED_NETWORKS` default is Tailscale's IPv4
  and IPv6 ranges plus loopback, and leaves out RFC 1918 on purpose. A LAN
  holds printers, televisions and guests and is a boundary nobody maintains,
  and a host firewall that is the only thing enforcing the rule is one `ufw
  disable` from silently admitting all of it.
- Every URL requires a signed-in session except sign-in and sign-out
  (`/accounts/login/`, `/accounts/logout/`), the SSO handshake and the
  provider's back-channel logout (`/oidc/`), the
  container's probes (`/health/`), the browser's policy reports
  (`/csp-report/`), the icons a browser asks for at the root, and `/static/`.
  The machine API (`/api/v2/`) and `/api/mcp/` take a bearer token instead.
  `PUBLIC_SURFACE` in `hq/platform/core/tests/test_security.py` is the list.
- No public registration. New users are created via `manage.py createsuperuser`,
  or by a first sign-in the role settings admit.
- No Django admin. `django.contrib.admin` is not installed and nothing is
  routed under `/admin/`: it writes a model directly, with no capability,
  approval or audit attribution, and every write in HQ goes through the
  capability policy. A test holds that. An account's staff and superuser
  flags are shown in the account menu and the connection panel and authorize
  nothing; what a person may do is their role. A superuser the host's shell
  creates holds `operator` until a sign-in resolves its role.

## Roles

A person holds one role, `PersonRole`, and its capabilities are the most they
or anything acting for them may do. The roles are declared once, in
`hq/platform/application/roles.py`:

| Role | Holds |
| --- | --- |
| `operator` | every capability, the host's and every extension's (`operator_capabilities`) |
| `viewer` | the reads: `read` and every extension's read capabilities, the base every agent's ceiling starts from |

What each subject holds, with `HELD_ONLY_AT` applied on top:

| Subject | Holds |
| --- | --- |
| A person in the browser | their role's capabilities, as resolved at their last sign-in or renewal |
| An agent a person signed in (a token naming them in `sub`) | its grant, within the agent ceiling and that person's role |
| A person's registered device | its grant, within that person's role |
| A machine's own token (no person) | its grant, within the agent ceiling |
| A person with no role, or a token naming one | nothing: a token is refused with 403 at either door |

- A sign-in resolves the role from the identity provider's `groups` claim and
  the person's verified address. `SEVERINO_OIDC_ROLE_GROUPS` maps each group
  HQ admits to a role (`admins=operator,family=viewer`), and its keys are the
  only groups admitted. `SEVERINO_OIDC_ALLOWED_EMAILS` admits an address the
  provider verified, with the role its `=role` names, `viewer` when it names
  none. A person several entries name holds the highest. A group HQ's settings
  do not name, or a missing `groups` claim, is a refused sign-in, and no role is
  written.
- An entry naming a role HQ does not have stops HQ at start.
- The sign-in writes the role (`roles.assign`) and audits a change, so a
  person moved to another group holds its role from their next renewal.
- Every account that existed before roles holds `operator` until its next
  sign-in resolves it.
- The Agents page names each agent's person, their role and when it was last
  confirmed. It splits its grant into what is in force, what HQ's settings
  hold back and what lies beyond its person's role, by the same rule its token
  is held to (`security.agent_grant`).

### What a page reads under

Every page reads under one capability, and a signed-in person opens it only
while their role holds it. A viewer opens every page that reads under `read`,
which is most of HQ and every extension page that declares nothing, and is
refused the audit log (`read_audit_log`) and the calendar (`read_calendar`).

- A view declares its capability as `required_capability`, a class's attribute
  or a function view's `hq_sdk.web.capability_required`. A page that declares
  nothing reads under `read`, never under nothing, and one that declares it
  empty (`hq_sdk.web.CapabilityRequiredMixin` left as it comes) is never served.
- The check is `LoginRequiredMiddleware`'s, before the view runs, so a refused
  page has read nothing. It answers with the 403 page, which names no
  capability and shows nothing of the page, and the refusal is recorded in the
  audit log.
- A route that answers without a session is not a page this rule holds, and
  authorizes its own requests. These are the routes marked
  `login_not_required`: the machine API, which refuses a token by its grant;
  health; sign-in and the identity provider's callbacks, the back-channel
  logout checking its signed token; CSP reports; the icons. A new one has to carry its own refusal.
- What a caller who has not signed in can make HQ write is declared where the
  code that writes is, with `hq.platform.core.write_bounds.writes`: each such
  route, and each of HQ's middleware, as writing nothing or under a named
  `Bound` (the sign-in throttle, the refused-credential coalescing, the
  handshake's session expiry, the arrivals reading, the policy-report cap).
  `test_write_bounds.py` fails on one that declares nothing.
- A link to a page is offered only to a person who may open it: the nav, a
  page's head actions and trail, and a remedy each ask `pages.offered`, which
  asks the same rule. `test_offered_links.py` holds every nav item and every
  routed page to it.
- What a page offers to change is refused again by the capability its command
  requires, so a viewer who opens a page can do nothing on it a viewer may
  not.
- `PageReadCapabilityTests` walks the URL configuration, every installed
  extension's included, and holds that a person with no role opens no page and
  that each page reads under one capability an operator holds.

## What HQ enforces

- `DEBUG=False` enforced when `DJANGO_SECRET_KEY` is set; missing key in
  production raises a startup error.
- `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` come from environment variables.
- `SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE` default ON in production,
  and where they are on the cookies are named `__Host-sessionid` and
  `__Host-csrftoken`. The browser enforces the prefix: no other host or path
  under the domain can set a cookie of that name, so a session cannot be
  planted by a neighbor for HQ to read back.
- `SECURE_CONTENT_TYPE_NOSNIFF`, `X-Frame-Options:
  DENY`, `Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy:
  same-origin` and `Cross-Origin-Resource-Policy: same-origin` enabled. The
  static mount sets the last two itself, because it sits above the Django
  middleware that sets them everywhere else.
- Who sent a request, and over which scheme, is decided once, before any
  route. Uvicorn's `ProxyHeadersMiddleware`, applied in `hq/config/asgi.py`
  (`ForwardedCaller` in `hq/platform/core/network.py`), reads
  `X-Forwarded-For` and `X-Forwarded-Proto` only from a peer in
  `SEVERINO_TRUSTED_PROXIES`. It resolves the chain from the right to the
  first hop that is not one. The network gate, the sign-in lockout, the audit
  log and `request.is_secure()` all read that result; from any other peer the
  headers change nothing. The server itself reads none (`--no-proxy-headers`).
- `DJANGO_BEHIND_TLS_PROXY=1` turns on the redirect that keeps the plain port
  HQ binds from being a second front door: a request a trusted proxy did not
  forward as HTTPS is sent to the canonical name. Only the healthcheck path is
  exempt, because it deliberately probes that port from inside the
  container's own network namespace.
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
  assets, no object embedding, and no framing.
- Scripts are admitted by nonce alone. `script-src` is the response's nonce and
  `'strict-dynamic'`, so a browser that supports it ignores `'self'` (kept for
  one that does not). A same-origin file, a receipt or any static file, cannot
  run as script from markup HQ did not write.
- Every `<script>` a template renders carries `{% csp_nonce_attr %}`, held by a
  source test over every template; a script a nonced one adds is trusted
  through it. Application JavaScript is external. A regression test rejects
  inline scripts and event handlers before they can weaken the policy.
- The policy also requires Trusted Types. Assigning a string to `innerHTML`,
  `outerHTML`, `srcdoc` or a script URL throws instead of parsing, so a
  DOM-based XSS sink cannot execute even if one is introduced.
  `trusted-types 'none'` means no policy can be declared to opt back out. It
  costs nothing, because every dynamic node HQ builds uses
  `createElement`/`textContent`.
- The API reference at `/api/docs/` is the one exception. The vendored Scalar
  bundle (`static/vendor/scalar`, version and SHA-256 in `UPSTREAM`, checked by
  a test) renders through `innerHTML`. Its policy is derived from the main one
  (`SEVERINO_API_REFERENCE_CSP`). It keeps scripts to the nonce and
  `'strict-dynamic'` with no inline code or `eval`, and fetches nothing
  off-origin.
- Violations are reported back. The policy carries `report-to` and `report-uri`
  pointing at `/csp-report/`, which records the directive, what was blocked and
  the reporting address to the audit log. A directive is recorded only when
  HQ's policy governs it, what was blocked is kept as an origin or a CSP
  keyword (`inline`, `eval`, `data`), the same complaint writes one row an
  hour, and every complaint together writes at most `CSP_REPORTS.rows` an hour. It is the only way HQ learns
  that a directive enforced in someone else's browser has stopped holding.
- HQ keeps Django's default `SIGNED_COOKIE_LEGACY_SALT_FALLBACK = False`, so a
  signed cookie made with the older, ambiguous salt derivation is refused.
- HSTS on by default for a year, including subdomains. Preload stays opt-in:
  it is slow to undo and meaningless for a name the public internet cannot
  resolve.
- Django's `LoginRequiredMiddleware` (`hq.platform.core.middleware` adds extensions'
  token routes) redirects anonymous users to login for every view not marked
  `@login_not_required`. `AnonymousSweepTests` asks for every route without a
  session and holds the public ones to a reviewed list.
- Receipt files:
  - Stored at `SEVERINO_MEDIA_ROOT`, **outside the app code directory**.
  - Filenames are randomized (UUID), not user-supplied.
  - Storage's `base_url` is `None`: there is no public URL for these files.
  - The `receipts:file` view requires authentication, streams the file, sets
    `X-Content-Type-Options: nosniff` and `Cache-Control: private, no-store`,
    and audits the view.
- Uploads are content-type-filtered (`hq/domains/receipts/forms.py`) and size-capped
  (15 MB by default).
- Audit log on every create / update / delete (via signals), plus login,
  failed login, logout, upload, export, and import events, and refusals. An
  authenticated caller's refusal gets its own row; rejected credentials are
  counted per surface, source and reason per minute. Credentials are never
  recorded.
- Routine machine events (a connection probed by a sweep) are deleted after
  30 days (`SEVERINO_AUDIT_ROUTINE_DAYS`) by the daily `audit.prune`
  job. Only the types listed in `hq.platform.core.audit.ROUTINE_EVENTS` are eligible, and
  only when no user is attached. Logins, refusals, settings changes, deletions
  and every change to a resource or credential are kept indefinitely.
- The audit log is append-only by SQLite's own rule
  (`hq/platform/core/append_only.py`). Two triggers refuse an UPDATE or
  DELETE of an audit row from every write path (the ORM, raw SQL, a shell).
  They let through three changes: the prune above, a deleted user's
  `SET_NULL`, and a coalesced refusal's count, which only grows, and its last
  time, which only moves forward. The triggers are installed after every
  migrate, so a migration that rebuilds the table gets them back, and
  `test_append_only` holds that the database's triggers match what the code
  generates. A test's flush lifts them, and a production flush is refused.
- Each row says who acted in its own text (`actor`, `interface`, stored
  columns over its metadata), and a signed-in user's row names them there
  too, so a deleted account's rows still say who it was.
- Each committed audit event, and each recount of a refusal, is also written
  as one JSON line on the `severino.audit` logger with the row's id and
  request id. The line goes out once the write commits, never for one rolled
  back, so the container's output holds a copy outside the database.
- Sessions are database rows with a 12-hour age. The daily
  `sessions.clear` job runs Django's `clearsessions`, which deletes the ones
  past their expiry.
- Request-user attribution uses an ASGI-safe context variable, preventing one
  concurrent request's identity from leaking into another request's audit row.
- Server-generated request IDs connect response headers to structured JSON
  access logs without recording query strings or request bodies.
- Anonymous liveness and readiness probes disclose only component state;
  readiness fails on database, migration, or writable-storage problems.
- SQLite is opened with `journal_mode=WAL`, `foreign_keys=ON`,
  `synchronous=FULL` and `transaction_mode=IMMEDIATE`: writers queue instead
  of failing, and a power cut cannot undo a save HQ already reported.
- Password validators require min length 12 and reject common/numeric-only
  passwords.
- Optional Pocket ID / OIDC SSO is supported. HQ admits a person in a group
  `SEVERINO_OIDC_ROLE_GROUPS` names, or at an address
  `SEVERINO_OIDC_ALLOWED_EMAILS` names, gives them that role
  ([Roles](#roles)), and binds the identity to a Django user by the token's
  issuer and `sub`. One subject is one user, and one account elsewhere one
  person's (a unique `LinkedAccount` login per provider).
- A session renews with the provider every 15 minutes, which re-resolves the
  role. It is how a demotion that keeps HQ access takes effect, since the
  provider sends no logout then. A renewal HQ refuses (no admitted group, a
  token that does not verify) ends the session at once (`HQCallbackView`),
  rather than leaving it to its 12-hour expiry.
- Pocket ID's back-channel logout (`/oidc/backchannel-logout/`) ends every HQ
  session of the person it names the moment Pocket ID signs them out or they
  lose the group the HQ client allows. HQ checks the logout token as every
  token (`decode`: its `typ` `logout+jwt`, HQ's client as the audience, `exp`,
  `iss` and `aud`), then its back-channel logout event, a `sub`, a `jti` and
  no `nonce`. Pocket ID sends no `sid`, so every session of that subject ends;
  the account and its password, kept for break-glass, are untouched. The
  sessions end through the `session.sign_out` capability, run by HQ itself
  with the token's `jti` as its retry key, so a redelivered token acts once and
  the act is audited. Its `end_sessions` capability is held only at the
  internal interface (`HELD_ONLY_AT`), so no person, device or agent signs
  anyone out through it. A token HQ refuses is a 400, which Pocket ID does not
  retry; an unknown subject is a 200.
- Only an address the provider verified (`email_verified` true) introduces a
  subject to an account no subject is bound to yet. A username, or an address
  the provider has not verified, finds no account. After the first sign-in the
  subject alone decides. HQ asks for the `email` scope on every deployment.
- So a local admin who should get SSO carries the address Pocket ID verified
  for that person. When a deployment's Pocket ID leaves addresses unverified,
  that person's first sign-in creates an account of their own
  (`SEVERINO_OIDC_CREATE_USER`), or is refused when that is off.
- An account keeps only a verified address: a new one made at first sign-in
  holds none when the provider has not verified it, and an unverified change
  is not copied onto an existing one.
- HQ checks every token it is handed in one place (`decode` in
  `hq/platform/application/tokens.py`), the browser's ID token and an agent's
  access token alike. It checks the algorithm, the issuer, the audience (HQ's
  client for an ID token, `SEVERINO_API_RESOURCE` for an access token), `exp`, `iss` and `aud`
  required, and the header `typ` of that kind of token. An ID token for
  several parties is accepted only when HQ's client is its `azp`. Both read the
  provider's signing keys from the one store HQ reads at start and hourly
  (`KEYS`), never on a request. A token naming a key not held is refused and
  asks for a read, so a sign-in it stops succeeds when the person tries again.
- With SSO on, password sign-in is off. An operator with host access can turn
  it back on (`SEVERINO_PASSWORD_LOGIN_ENABLED`) as the break-glass path.
- The `/api/mcp/` Streamable HTTP endpoint is a separate security boundary,
  reached through the TLS proxy like every page. Uvicorn binds loopback only,
  and the caller is the address the proxy saw, as the forwarded-header
  middleware above resolves it for every route.
  The door admits only a caller in Tailscale's IPv4/IPv6 ranges, checks an
  explicit Host allowlist, and rejects browser Origins unless allowlisted
  (`TailnetGate` in `hq/platform/mcp/security.py`). The gate is HQ's because
  the MCP SDK's transport security takes any text after a host's colon and
  runs only after authentication.
- Agents authenticate to `/api/mcp/` with identity-provider access tokens, verified
  as the machine API verifies them, so each agent is its own attributable,
  revocable principal. There is no second credential and no shared static
  bearer, and a request with no verified caller reaches no tool.
- `/api/mcp/` answers as the MCP authorization spec asks. With no token, or one HQ
  did not verify, it is a 401 whose `WWW-Authenticate: Bearer` carries
  `error="invalid_token"` and `resource_metadata`: the RFC 9728 document at
  `/.well-known/oauth-protected-resource/api`, behind the same gate, naming the
  OIDC issuer as the authorization server and `SEVERINO_API_RESOURCE` as the
  resource a token must name, with HQ's permission keys as `scopes_supported`
  so a native sign-in asks for them. A verified token that grants no HQ permission
  is a 403 `insufficient_scope` naming `scope="read"`, on both doors. The
  MCP SDK's own pieces answer the 401 and serve the metadata
  (`BearerAuthBackend`, `RequireAuthMiddleware`,
  `ProtectedResourceMetadataHandler`); the 403 is HQ's, since the SDK's names
  no scope.
- No scope is required at the door: what a token may do is decided per call
  in the capability layer. A capability missing for one tool is that call's
  MCP error result, never an HTTP 403, so a client stays connected for the
  tools it may use.
- HQ verifies access tokens only. The header `typ` must be `at+jwt` or
  `application/at+jwt` (RFC 9068), so an ID token the same provider signs for
  the same audience is refused.
- <a id="agents"></a>Every token principal is an agent, on `/api/mcp/` and on the
  machine API (`/api/`) alike, unless it is a person's own device. Both turn a
  verified token into a principal through one function (`token_principal` in
  `hq/platform/application/tokens.py`, then `agent_principal` in
  `hq/platform/application/security.py`). It holds the token's grant capped by
  the deployment's agent ceiling, the `SEVERINO_AGENT_ENABLE_*` switches
  (`agent_ceiling`), and, on a token a person signed in, by that person's
  role, so an agent acting for a viewer reads and does nothing else. A token
  naming a person who holds no role is refused. The person is recorded on the
  agent (`AgentIdentity.person`), and a change audited.
- An agent refused a write on one surface is refused it on the other. A switch
  still named `SEVERINO_MCP_ENABLE_*` stops HQ from starting and names its
  replacement.
- A person's own device (the phone app, a Shortcut) holds a person's standing.
  Pocket ID's RFC 9068 access tokens name the client (`client_id`). On a grant
  a person allowed, they name that person (`sub`; a client-credentials token's
  is `client-<client_id>`). But nothing in them says the client is the person's
  own device rather than an agent the person authorized.
- So a device's standing takes two halves. One permission on HQ's Pocket ID
  API resource, `own_device` (`DEVICE_SCOPE`), given by an administrator to
  each device's client; and the person's own registration of that client in
  HQ, on the Agents page (`OwnDevice`, `agent.device.set`, held only by a
  person), matched to the token's subject as bound at their sign-in. A token
  carrying the permission from a client its person registered is
  `device_principal`'s, on the `device` interface. Its other permissions are
  bounded by its person's role, uncapped by the agent ceiling, and never
  stopped by the Agents pause. Its client is the actor, so the audit log names the device, and
  revoking that client in Pocket ID ends it alone. Its properties:
  - The permission is a standing, not a capability: it grants nothing on its
    own, and a device holds exactly the other permissions its token carries.
  - Only an administrator gives it. Pocket ID (v2.19.1) issues a scope on an
    API only from the client's grants on that API, for every grant type
    (`resolveResource`), and those grants are written only through its admin
    API, reached by an admin's session or API key and never by an OAuth
    token. Consent only narrows what a client was granted.
  - A client known only by a Client ID Metadata Document reaches an API
    through one opt-in covering every such client, so it is never a device.
    Its `client_id` is a URL, which no registered client's can be, and HQ
    refuses the permission on it. HQ refuses it too on a token with no person
    behind it (a client-credentials grant).
  - A device holds what its person's browser holds, their role, bounded by
    its token's grant. `DEVICE_INTERFACE` is one of `INTERACTIVE_INTERFACES`, so what is
    held for a person (`HELD_ONLY_AT`: approving, pausing agents, uploads,
    preferences, pins) is in its reach. Nothing it asks for is held for
    approval, and the capability policy's agent rules do not apply to it.
  - A use case that keeps something of a person's finds them by the token's
    subject, bound at their sign-in.
  - So a device token can approve an agent's request. A stolen one is
    stopped by blocking its client or removing its registration on the Agents
    page, or by removing that client's `own_device` grant in Pocket ID, each
    of which ends it alone, and every act it made names the client in the
    audit log.
  - HQ holds no Pocket ID admin credential, so nothing reaching HQ can give a
    client the permission.
- A token an agent's client got through a person's sign-in (an authorization
  code or device grant) is that agent's. Its client is the actor, and its
  grant is the client's own user-delegated grant, capped and paused as every
  agent's is. It stands for no person (`test_delegated_tokens.py`).
- So the provider administrator's grant alone never makes a device. From its
  claims, an agent's client given `own_device` in its delegated grant cannot
  be told from the person's phone. With no registration by that person it is
  admitted as an agent, and a registration another person made grants it
  nothing (`test_delegated_tokens.py`). A person registers only for
  themselves, only a client id a provider could have registered, and only
  once they have signed in through the provider, so a token can name them.
- A token's lifetime is the identity provider's to set. HQ requires `exp`,
  `iss` and `aud` and refuses an expired token, with
  `SEVERINO_API_LEEWAY_SECONDS` of clock skew, but sets no maximum lifetime
  of its own.
- An operator can pause every agent from the menu. The switch is global, read
  on every request to the machine API (`/api/v2/`) and on every tool call on
  `/api/mcp/`, and fails closed. Both surfaces accept the same Pocket ID tokens, so a brake on one
  alone would leave a paused agent a URL away from its capabilities.
- On `/api/v2/` a paused agent's request is a 403 `agents_paused`. On `/api/mcp/` the
  pause is inside the protocol: a paused agent still initializes and lists
  tools, and every tool call answers an MCP error result naming the pause. So
  its client stays signed in, and the first call after the resume runs on the
  same connection. A paused agent learns only the tool list, which is the
  public contract in `hq-api.openapi.json`, and reads and changes nothing.
- Until agents resume, a paused agent's held requests cannot be approved, and
  work it queued is not handed to the controller.
- A person can block one client from the Agents page (`agent.block.set`,
  held only by a person, as the pause is). The block is read on that client's
  every call at both doors, a person's own device included, and fails closed.
  On `/api/v2/` it is a 403 `agent_blocked`, and on `/api/mcp/` a tool result
  naming it, so the client stays connected and its first call after it is
  allowed again runs. Who set the block and when is kept on the client's
  record and in the audit log. A block holds what the client already asked
  for, as the pause does: its held requests can be declined but not approved,
  and its queued work is not handed to the controller until the block is
  lifted.
- All four read the pause through one helper (`paused_interfaces` in
  `hq/platform/application/agent_access.py`). It names every agent interface
  or none, so a person is never paused. Approving a held request and the
  controller's claim ask it through `held_askers`, one query over who asked
  that covers the pause and the block together. The two doors admit through one function
  (`admit` in `hq/platform/application/agent_admission.py`) and ask `refused`
  there of the principal it returns, never of the surface. `refusal` in
  `agent_access.py` answers a code and a reason, so a new brake reaches both
  doors in its own words. So a person's own device runs while the agents
  beside it are refused.
- Capability policy at `/agents/` sets, per capability and per surface or
  agent, whether a call is allowed, held for approval, or denied. Rules only
  narrow the identity provider's grant. An explicit rule beats the default, and
  between a surface and an agent rule the stricter wins.
- With no rules, gated infrastructure changes are held, and anything
  destructive an agent asks for (a deletion, or stopping a container) is held
  too. A person in the browser, or on their own device, is never held. A rule
  that allows a would-be-held call is recorded as its consent.
- A held request writes and queues nothing. It is decided on its audit entry
  (`/audit/?awaiting=1`), or from its action item, against a fingerprinted
  field-by-field diff. So a record that changes meanwhile supersedes the
  request.
- Requests lapse after 24 hours (`SEVERINO_APPROVAL_WINDOW_HOURS`), and each
  actor may hold ten. A held request is its asker's: the same change asked by
  another agent is a request of its own, approved as that agent, and once one
  of them applies the other goes stale rather than applying twice. An agent
  reads only the held requests it asked for, on every surface that lists them;
  a person reads every one. Approval requires a person, in the browser or on
  their own device, and never the caller that asked. So no agent's token
  approves anything.
- The change runs as the agent that asked, so its audit events name that
  agent. The person who approved or declined it is the request's
  `decided_actor`, which a decided request cannot be saved without.
- Every capability's audit events name its caller: the capability runner
  sets the actor, interface and capability name for whatever the handler
  records (`_run` in `hq/platform/application/capabilities.py`).

## Production checklist

- [ ] Generated a strong `DJANGO_SECRET_KEY` (50 random bytes or more).
- [ ] `DJANGO_DEBUG=0`.
- [ ] `DJANGO_ALLOWED_HOSTS` contains only your Tailnet hostname and `127.0.0.1`.
- [ ] `DJANGO_CSRF_TRUSTED_ORIGINS` matches the full origin you actually serve.
- [ ] Uvicorn binds `127.0.0.1:8000`, so port 8000 answers only loopback and
      every caller, browser or MCP client, comes through the reverse proxy.
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
      "HTTPS only" layer reads the live setting
      and says so when it is off; left at zero, it keeps telling browsers that
      plain HTTP is worth trying, indefinitely.
- [ ] The container runs read-only, with every capability dropped,
      `no-new-privileges`, a pids limit and a memory limit
      (`controller/host/web.go`). `/tmp` is the only
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
      client callback is `https://hq.example.com/oidc/callback/`.
- [ ] If SSO is enabled, the HQ OIDC client's back-channel logout URL is
      `https://hq.example.com/oidc/backchannel-logout/`, and its Allowed User
      Groups are the groups `SEVERINO_OIDC_ROLE_GROUPS` names, so a person who
      loses them is signed out of HQ at once. Each agent client's Allowed User
      Groups are set the same way.
- [ ] If SSO is enabled, `SEVERINO_OIDC_ROLE_GROUPS=admins=operator` and
      `SEVERINO_OIDC_CLIENT_SECRET` is delivered through the rendered app
      environment file, not the compose interpolation `.env`.
- [ ] The Pocket ID HQ client has PKCE enabled; HQ uses S256 in addition to
      confidential-client authentication.
- [ ] Documentation index records carrying secrets are flagged
      `sensitivity=sensitive` or `restricted` (these are excluded from
      automated retrieval).
- [ ] MCP callers authenticate only with an access token from the identity
      provider, one client per agent, so every call names its agent. There is no
      shared static bearer; without the provider configured, MCP is off.
- [ ] A destructive capability called by an agent waits for an operator's
      approval unless a rule explicitly allows it for that agent or surface.
- [ ] The app environment's source of truth is the 1Password app-environment
      item. It is rendered to the root-only noswap tmpfs at
      `/run/severino-hq-secrets/web/`, never to the runner-writable checkout,
      and the entrypoint sources it; the on-host `.env` contains no secrets
      and cannot redirect the bind.
- [ ] HQ stores exactly one class of secret: a certificate an operator generated
      themselves and asked HQ to install. Its private key is sealed to the
      controller, whose private half the web never holds
      ([Secrets](Secrets.md#sealing-to-the-controller)). Storing is refused
      outright when HQ has no key to seal to, never downgraded to plaintext.
- [ ] The sealed material reaches only the controller, still sealed, through a
      bridge action of its own, so it does not ride in the contract that
      `export` answers with. It appears in no serializer, no API response, and
      not in the reply to the upload that supplied it.
- [ ] The controller bridge listens on its Unix socket only: no web route
      reaches a bridge action, the socket's directory admits the web account
      alone, and both ends refuse a peer that is another uid. The controller's
      container mounts that directory read-only and holds neither HQ's
      database nor its application environment.
- [ ] Provider credentials remain outside the web container entirely. The
      controller report guard rejects any status carrying a key named
      `private`, `secret`, `token`, `password`, or `credential`.
- [ ] The Connect reader token can read only the vault `host.json` names. It
      is a host-bound encrypted systemd credential, never a file in that vault.
      Without a TPM, the credential key sits on the same disk as the
      ciphertext, so an offline disk or root compromise reads it.
- [ ] `severino-hq-secrets.timer` is enabled and its last service run
      succeeded. Refresh validates every output before installation and restarts
      HQ when web secrets change. Existing bind mounts use in-place writes;
      rotation is not a multi-file transaction ([Secrets](Secrets.md)).
- [ ] `SEVERINO_MCP_ALLOWED_HOSTS` contains only the host name the proxy serves
      HQ at.
- [ ] The MCP client connects to `https://<hq-host>/api/mcp/`, and the proxy
      forwards `/api/mcp/` and `/.well-known/oauth-protected-resource/api` to HQ behind
      the tailnet access list.
- [ ] `SEVERINO_API_RESOURCE` is `https://<hq-host>/api`, the Pocket ID API
      resource's identifier. Both machine doors sit under its path, so one
      audience covers them, and the metadata names what a token is asked for.
- [ ] `SEVERINO_AGENT_ENABLE_WRITES`, `SEVERINO_AGENT_ENABLE_DOC_SYNC` and
      `SEVERINO_AGENT_ENABLE_INFRASTRUCTURE` are enabled only for the
      capabilities some agent or API client actually needs.
- [ ] The DNS-01 token is its own connection item, with only Zone Read and
      DNS Edit on the zones it renews, and is not the Cloudflare API token.
- [ ] No personal SSH private key is rendered for the controller. Its
      identities are SSH Key items generated by 1Password, remote host keys
      are pinned in the connection registry, and each public key is authorized
      only for its declared target and deployment role.

## What HQ does not do

- Talk to the public internet from the app server *with a credential*. The
  outbound calls HQ does make are narrow and named: the content index, and the
  two public-registry lookups behind `lookup.name.read` / `lookup.address.read`.
- Give a lookup a credential. The lookups carry none, which is why they are
  allowed to run in the web process, rather than being queued through the
  controller like provider work.
- Read contact submissions from the web process. The controller reads and
  changes them through the Cloudflare API connection.
- Run a WordPress plugin, customer portal, or public webhooks.
- Decrypt git-crypted Obsidian content. The vault stays separate.
- Store credentials, API tokens, or secrets in models. The documentation
  index is metadata-only.
- Expose arbitrary commands, Django shell, raw SQL, SSH, deployments, receipt
  file contents, or runbook bodies through MCP.
