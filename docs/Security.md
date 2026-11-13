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
| `viewer` | the reads: `read` and every extension's read capabilities |

What each subject holds, with `HELD_ONLY_AT` and `NEEDS_A_PERSON` applied on top:

| Subject | Holds |
| --- | --- |
| A person in the browser | their role's capabilities, as resolved at their last sign-in or renewal |
| An agent a person signed in (a token naming them in `sub`) | its grant, within that person's role |
| A person's registered device | its grant, within that person's role |
| A machine's own token (no person) | its grant, less a person's own settings, pins and set-asides |
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
  is held to (`security.agent_grant`). For an agent a person signed in, it
  names what HQ offers within that role that the last sign-in did not carry,
  the set a refusal calls `insufficient_scope`, read from the grant HQ
  recorded (`AgentIdentity.granted`), so HQ needs no key to the identity
  provider.

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
- Style elements come only from HQ's own sheets: `style-src-elem` is
  `'self'`, so a `<style>` injected into a page, whose selectors could read
  it, draws nothing. `style-src-attr` admits inline attributes only while
  WebKit reports a violation for every `<select>`; HQ writes none
  ([Design](Design.md), "No page writes a style").
- The policy also requires Trusted Types. Assigning a string to `innerHTML`,
  `outerHTML`, `srcdoc` or a script URL throws instead of parsing, so a
  DOM-based XSS sink cannot execute even if one is introduced.
  `trusted-types 'none'` means no policy can be declared to opt back out. It
  costs nothing, because every dynamic node HQ builds uses
  `createElement`/`textContent`.
- The API reference at `/api/docs/` is the one exception. The vendored Scalar
  bundle (`static/vendor/scalar`, version and SHA-256 in `UPSTREAM`, checked by
  a test) renders through `innerHTML`. Its policy is derived from the main one
  beside its view (`hq/platform/api/reference.py`). It keeps scripts to the
  nonce and `'strict-dynamic'` with no inline code or `eval`, and fetches
  nothing off-origin. It admits inline style elements, with no nonce:
  the bundle's CodeMirror writes a `<style>` that cannot take one, and the
  page shows only HQ's own registry.
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
  - The `receipts:picture` view, the `<img>` a receipt's page draws, answers
    the same way but only for a file stored as an image type a browser draws
    (`PICTURE_CONTENT_TYPES`), inline, and is not audited: it is part of the
    page view, which HQ does not record. A PDF is a link to `receipts:file`,
    never framed, since `frame-ancestors 'none'` and `X-Frame-Options: DENY`
    refuse HQ framing its own responses.
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
- Each request's id is generated by HQ, never taken from the caller: it is
  the operation id of every audit row the request writes, and it is on every
  JSON log line, which records no query string or request body. With a trace
  endpoint set in development, it is also the request's trace id.
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
- Sign out (a POST; a GET is refused) ends Pocket ID's session as well as
  HQ's. The library's `OIDCLogoutView` sends a session signed in through the
  provider to its end-session endpoint (`end_session_url`) with the ID token
  as `id_token_hint`, HQ's `client_id`, and the signed-out login page as
  `post_logout_redirect_uri`; a session with no ID token (a password or CLI
  sign-in) is signed out of HQ alone. So the ID token is kept in the
  database-backed session (`OIDC_STORE_ID_TOKEN`). That is safe because HQ
  accepts no ID token as a credential: it reads one only from its own exchange
  with the provider's token endpoint, and every door a caller presents a token
  to asks for an access token's `typ` (`decode`).
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
  is a 403 `insufficient_scope` naming `scope="read"`, on both doors. An
  identity scope a sign-in asks for (`openid`, `profile`, `email`, `groups`,
  `offline_access`) is never an HQ permission, so a token holding only those
  is refused, and the Agents page counts none of them. The
  MCP SDK's own pieces answer the 401 and serve the metadata
  (`BearerAuthBackend`, `RequireAuthMiddleware`,
  `ProtectedResourceMetadataHandler`); the 403 is HQ's, since the SDK's names
  no scope.
- The 401 names no `scope`. A client asks first for the scope a challenge
  names, so with none it asks for all of `scopes_supported`, which Pocket ID
  needs: it refuses a whole sign-in that asks for one permission the client
  was not given. The SDK's `required_scopes` would also make every name
  listed a requirement of every request.
- No scope is required at the door: what a token may do is decided per call
  in the capability layer. A capability missing for one tool is that call's
  MCP error result, never an HTTP 403, so a client stays connected for the
  tools it may use.
- A call refused only because the token lacks a permission its person holds,
  and an agent's token could carry (`agent_grantable`), is `insufficient_scope`,
  with that permission in `error.details.scope` and a message saying to grant
  it to the client in Pocket ID and sign in again. On MCP it stays the call's
  tool error (a prompt's or resource's protocol error carries it in
  `data.details.scope`); the machine API answers a 403 whose
  `WWW-Authenticate` is `Bearer realm="Severino HQ",
  error="insufficient_scope", scope="<permission>"` (RFC 6750 section 3.1).
  What a new sign-in cannot fix stays as it was: the person's role lacking
  it, a `HELD_ONLY_AT` name, and a `NEEDS_A_PERSON` name on a machine's own
  token, are `forbidden`, and an operator's rule is `denied_by_policy`.
- HQ verifies access tokens only. The header `typ` must be `at+jwt` or
  `application/at+jwt` (RFC 9068), so an ID token the same provider signs for
  the same audience is refused.
- <a id="agents"></a>Every token principal is an agent, on `/api/mcp/` and on the
  machine API (`/api/`) alike, unless it is a person's own device. Both turn a
  verified token into a principal through one function (`token_principal` in
  `hq/platform/application/tokens.py`, then `agent_principal` in
  `hq/platform/application/security.py`). It holds the token's permissions
  (`agent_grant`), and, on a token a person signed in, only those within that
  person's role, so an agent acting for a viewer reads and does nothing else.
  A token naming a person who holds no role is refused. The person is recorded
  on the agent (`AgentIdentity.person`), and a change audited.
- The unit HQ answers for is one client as one person, or a machine's client
  alone (`security.agent_subject`: the client id and the person's sign-in
  key). It is the key of the agent's row (`AgentIdentity`, one per client and
  person), of its rules (`CapabilityRule.subject`), its block, its retry keys
  (`IdempotencyRecord.actor`), its rate and calls at once, and the held ask
  it is answered as a repeat of (`requested_person_key`). So one person's
  "on" never consents for another who signed the same client in, nobody
  replays another's keyed answer, one person's flood leaves another's
  allowance whole, and the Agents page shows a row per person
  (`test_agent_subject.py`). An audit row names the client, so a client's
  recent activity is the same on each of its rows.
- Held requests are read by their client, interface and person together,
  including MCP tasks and the action queue. Queued operations keep the same
  person key, so blocking one person's agent holds its approvals and work
  without stopping another person's use of that client. Older operations
  whose person was never recorded keep a null key: a block on any identity
  of that client still holds them until it is lifted. An empty key instead
  names a machine token, whose block is its own.
- The identity provider's grant is the one declaration of what an agent may
  do: each permission is ticked per client on HQ's Pocket ID API resource, and
  HQ keeps no second list of its own. Removing one there fails that client's
  next refresh, so it ends within the access token's lifetime; blocking the
  agent on the Agents page ends it at once. What HQ still limits is the role,
  the capabilities `HELD_ONLY_AT` keeps for a person or for HQ itself, the
  person a personal setting needs (`NEEDS_A_PERSON`), the default hold and the operator's rules, approvals, the pause, a block and the
  rate limits. An agent refused a write on one surface is refused it on the
  other.

- `SEVERINO_FINDINGS_AUTO_REMEDY` lets HQ queue a repair for a finding with
  nobody asking. It is off by default, so what acts unattended is watched
  proposing before it is trusted acting. Even on, it only queues: the
  controller still claims and runs the repair, so no provider credential
  nears the web process.
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
  bounded by its person's role, held only where a person may hold them
  (`HELD_ONLY_AT`), and never stopped by the Agents pause. Its client is the actor, so the audit log names the device, and
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
    held for a person (`HELD_ONLY_AT`: approving, pausing agents, uploads)
    is in its reach. Nothing it asks for is held for
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
  agent's is. It acts for its person without standing as them: what it keeps
  (`set_preferences`, `pin_favorites`, `set_aside_action_items`, the
  `NEEDS_A_PERSON` capabilities in `security.py`) is that person's, found by
  the token's subject as `person_of` finds a device's, and its every act names
  its client. It is still an agent, so it is held where a hold applies and
  never approves or governs agents (`test_delegated_tokens.py`,
  `test_person_bound_agent.py`).
- A machine's own token has no person, so it is refused those three whatever
  Pocket ID granted it, and the catalog lists no command needing one to it.
  `set_preferences` also covers the dashboard's shared settings (its weather
  point, panel names and machine tiles): an operator's, since a viewer holds
  only reads, so an agent its operator signed in may change them as that
  operator may.
- A held request keeps the person its token named (`requested_person_key`),
  so an approved personal write replays as that person's.
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
- A person can block one agent from the Agents page (`agent.block.set`,
  held only by a person, as the pause is): one client as one person, or a
  machine's client. The block is read on that agent's every call at both
  doors, a person's own device included, and fails closed; a client one
  person blocked still runs for another who signed it in.
  On `/api/v2/` it is a 403 `agent_blocked`, and on `/api/mcp/` a tool result
  naming it, so the client stays connected and its first call after it is
  allowed again runs. Who set the block and when is kept on the client's
  record and in the audit log. A block holds what the client already asked
  for, as the pause does: its held requests can be declined but not approved,
  and its queued work is not handed to the controller until the block is
  lifted.
- A person can remove an agent HQ no longer needs to list, such as a client
  deleted in the sign-in service (`agent.remove`, held as the block is). Its
  record, its devices and its own rules go; the audit log and the changes it
  asked for stay. Removal grants nothing: a client that signs in again is a
  new agent, its first call from each device waits for approval, and its
  grant is what its next token carries. A blocked agent is not removed, so
  only allowing it lifts a block.
- Every agent call is placed on the tailnet device it came from
  (`agent_devices.place`), and does nothing from a device until that device
  is approved for that client and person. Its properties:
  - The device is the one tailscaled names for the caller's address, asked
    per request over the daemon's local API socket
    (`hq.platform.application.tailscaled`, `GET /localapi/v0/whois?addr=`),
    keyed by Tailscale's stable id (`Node.StableID`), never by a name or the
    node key a re-authentication rotates. The answer is the daemon's own
    netmap at that moment, so no polled reading stands between the WireGuard
    peer that opened the connection and the device HQ records: the token is
    bound to a key HQ verifies on every call, which is the sender constraint
    RFC 9700 section 2.2.1 asks of a resource server whose provider cannot
    bind the token itself. The address is the caller as `ForwardedCaller`
    resolved it: through a proxy HQ does not trust it is the peer that opened
    the socket, so a forwarded header never moves a call off the device that
    sent it.
  - Only an agent's admission asks the daemon; a page never does
    (`test_a_page_never_asks_tailscaled`). The polled tailnet reading keeps
    its jobs, the inventory and the Agents page's names, and decides nothing
    about admission.
  - The socket is `SEVERINO_TAILSCALED_SOCKET`, set by `hq-host run-web` on a
    host where tailscaled is enabled (the unit's link, read whether or not
    the daemon is up at that moment), which binds `/var/run/tailscale`
    read-only at the same path (`controller/host/web.go`): the directory,
    since the daemon makes its socket anew on every restart.
    tailscaled answers `whois` to any connection on its socket and keeps the
    socket world-readable where peer credentials exist, so the image's
    unprivileged account reads it: at tailscale v1.104.2,
    `ipn/localapi/localapi.go:557` asks only `PermitRead`,
    `ipn/ipnserver/server.go:351-352` grants read to every Unix-socket actor
    and write only to root or the operator, and `safesocket/unixsocket.go:85-87`
    makes the socket 0666 on such platforms.
  - `agent_access.refusal`, which both doors ask, refuses a device nobody
    approved with `device_awaiting_approval` and a blocked one with
    `device_blocked` (a 403 on `/api/v2/`, a tool result on `/api/mcp/`), and
    each refusal says the device is approved or allowed on the Agents page,
    with the address of the device's own row there. The device is recorded at
    its first call (`AgentDevice`), so it is there to approve.
  - The Agents page lists each agent's devices (name, os, tailnet owner, first
    and last seen, calls over the window, standing) with their approve and
    block controls, and a tailnet address an agent was refused from that the
    reading does not hold yet, which has nothing to approve. A device awaiting
    the reader's approval asks them at the top of the page ("<agent> wants to
    act for you from <device>, owned by <tailnet login>"); a machine agent's
    asks anyone who governs agents.
  - The request inspector's identity step for a token call reads the approval
    of the device it came from, since a token has no session to compare with
    the device's tailnet owner.
  - Only a person approves or blocks a device (`agent.device_approval.set`,
    `agent.device_block.set`, under `govern_agents`, held only at
    `INTERACTIVE_INTERFACES`), so no token approves its own. A device an agent
    called from for a person is approved by that person alone: the approval
    is what links their HQ person to the device's tailnet owner, which HQ
    never infers. A machine agent's device is approved by whoever governs
    agents.
  - The deployment has a tailnet when the host named the socket
    (`tailscaled.socket_path`), never when the socket happens to be there, so
    a daemon away for a restart makes a call wait as `tailnet_unavailable`
    rather than run unplaced. Then admission fails closed: a call the daemon
    holds no device for (HQ's own loopback, the LAN, a tailnet address no
    peer has) is refused `device_unknown` at both doors, and one the daemon
    did not answer for is refused `tailnet_unavailable` with the daemon's
    words and a retry, both stopped where the two doors meet
    (`agent_admission.admit`) so neither door can admit it as the client
    alone. A token copied off a device and presented from a process on HQ's
    host therefore runs nothing, and a fresh database before its first tailnet
    reading places and holds a device like any other. Each refused address
    shows on the Agents page under its agent as refused, with nothing to
    approve. The refusal itself writes nothing past its denial.
  - Only a deployment with no tailnet (no socket: a developer's machine, a
    host without Tailscale) places nothing, and there the client is the finest
    unit and the client's rules alone apply. The Agents page says which of the
    two the deployment is (`data-placement`), naming the socket path.
  - A person's own client (`OwnDevice`, the `device` interface) needs no
    device approval: its registration is already the person's own act.
  - A device block refuses that client from that device alone, at once, while
    the client's other devices run. Like a client's block it holds what was
    asked from there: each held request and queued operation records the
    device that asked (`requested_device`), and `held_askers` withholds those
    from a blocked device.
  - The audit log names the device on every event the call records, held
    ones replayed on approval included: `device` in the event's metadata,
    stored as an indexed column beside `actor`.
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
- Capability policy at `/agents/` sets, per agent and capability, whether a
  call is on, asks a person first, or is off. The identity provider's grant is
  the ceiling, changed rarely and only with a new sign-in; a rule narrows it at
  once, and no rule can reach past it (`set_rule` refuses one). An agent's rule
  beats the default for that agent, over every surface it calls through.
- With no rules, a change to a kind that stands behind access
  (`requires_approval`: the tailnet policy, a container stack) is held from
  anyone but a person, the host's shell included. An agent's change to a kind
  the public internet sees (`public_effect`: a public DNS record, a domain) is
  held, as is anything destructive an agent asks for (a deletion, or stopping
  a container). Adopting a public record is not held, since it makes HQ's copy
  equal what is live. `needs_approval` in `default_hold.py` is the one answer,
  read by the hold, by the use cases' own floor (`consent_gap`), by the
  provider contract and by the policy page's words. A person in the browser,
  or on their own device, is never held. An agent's "on" over a call the
  default would hold is the person's consent, recorded as such
  (`Decision.overrides_a_hold`).
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

## A leaked GitHub App credential

The controller holds the GitHub App's private key and mints installation
tokens from it. Whoever else holds the key, or a token minted from it, can read
what the installation covers, and on a personal account GitHub shows nobody
when that happens:

- The account's security log records `integration_installation.*` events (an
  install, repositories added, permissions updated) and `integration.*` events,
  but no event for an installation token being created, nor for an App
  private key being generated or deleted.
- No REST API lists an App's private keys. An App can have up to 25, listed
  with their fingerprints only on its settings page.
- Secret scanning has a pattern for installation tokens (`ghs_`), with push
  protection, and none for App private keys.

What HQ can tell is the installation's hourly allowance, which everything
holding the key spends together. Each sweep records it, and HQ derives what
was spent between two sweeps of the same hour beyond HQ's own requests
([Architecture](Architecture.md#providers)). Once that passes
`observations.github.OTHERS_SPENT_ATTENTION` in one hour, more than the
deploy pipeline and the dependency update run take, the
`github-others-spent` finding names the requests and when, and that it can
mean a leaked key or token or another program sharing the App (a development
copy of HQ, a script). Its steps compare the keys GitHub lists with the
fingerprint of HQ's own, computed as GitHub's "Managing private keys for
GitHub Apps" gives it, then rotate the key:

```sh
openssl rsa -in PATH_TO_PEM_FILE -pubout -outform DER | openssl sha256 -binary | openssl base64
```

What HQ cannot tell: spend below that line, which the pipeline's own use
hides; spend in an hour HQ did not sweep twice; and GraphQL queries, which
spend a points budget of their own. A quiet allowance is no proof that
nothing leaked.

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
- [ ] If SSO is enabled, the HQ OIDC client's Logout Callback URL is
      `https://hq.example.com/accounts/login/?signed_out=1`, so signing out
      returns to HQ rather than stopping on Pocket ID.
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
- [ ] A destructive capability called by an agent, or its change to public
      DNS, waits for an operator's approval unless a rule explicitly allows
      it for that agent or surface.
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
- [ ] Each agent client on the Pocket ID API resource ([Agents](#agents)) is
      granted only the permissions it actually needs.
- [ ] `SEVERINO_FINDINGS_AUTO_REMEDY` is on only once the repairs it would
      queue have been watched being proposed.
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
