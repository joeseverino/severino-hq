# Severino HQ — machine-client API

The fourth delivery adapter, after the web UI, the CLI, and MCP. It exists so a
phone, a Shortcut, or a cron job can run an HQ capability over HTTP.

It adds **no capability, no domain model, and no business rule**. Every command
it runs is already in `application/capabilities.py`, which is what keeps four
adapters from drifting into four behaviours.

```
hq_api/security.py   verify a token HQ did not issue
hq_api/views.py      the transport
```

## HQ verifies; it does not issue

There is no credential table in this repo, no minting UI, and no token to
rotate. Access tokens are issued by **Pocket ID**, which already owns every
other credential in the fleet, and HQ only checks them.

That asymmetry buys three things a bespoke token store would not:

| | |
|---|---|
| **Nothing to leak** | HQ stores no secret. A database dump yields no working credential. |
| **Revocation is central** | Kill the client in Pocket ID and it is dead everywhere, immediately. |
| **Real scoping** | A credential can be granted `fitness.write` and nothing else. |

The last one is the point. A web operator holds every capability HQ has. A
credential living on a phone should not, and here it does not: the principal's
capabilities are *exactly* the token's granted permissions, never widened.

## Configuration

| Setting | Meaning |
|---|---|
| `SEVERINO_API_RESOURCE` | The Pocket ID API resource URI. **Empty disables the surface.** |
| `SEVERINO_API_LEEWAY_SECONDS` | Clock-skew allowance. Default 30. |

Empty must mean off, and does: without a resource to check `aud` against, a
token minted for any *other* API on the same Pocket ID instance would verify
here on signature alone. Signature is not identity.

## Setting up a client

In Pocket ID, **Administration → APIs**:

1. Create an API. Name it, and set its **Resource** to the value you will put in
   `SEVERINO_API_RESOURCE` (e.g. `https://hq.jseverino.com/api`). This becomes
   the `aud` claim and **cannot be changed later**.
2. Add a **Permission** for each capability the client needs, named *exactly*
   as HQ names it — `fitness.write`, `write_receipts`, `write_expenses`.

The permission keys are HQ's capability names on purpose. A mapping table
between the two systems would be a third home for the authorization model and
the first thing to go stale when a plugin adds a capability.

Then in **Administration → OIDC Clients**, add a client per automation, with
the client credentials grant enabled. One client per automation, not one shared
client: separate secrets rotate independently, and the token's `client_id`
becomes the actor in HQ's audit log, so an import is traceable to the
credential that caused it.

## Using it

Get a token:

```bash
curl -s https://sso.jseverino.com/api/oidc/token \
  -d grant_type=client_credentials \
  -d client_id="$CLIENT_ID" \
  -d client_secret="$CLIENT_SECRET" \
  -d resource="https://hq.jseverino.com/api" \
  -d scope="fitness.write"
```

Ask what it may do:

```bash
curl -s https://hq.jseverino.com/api/v1/ -H "Authorization: Bearer $TOKEN"
```

```json
{"ok":true,"data":{"actor":"health-sync-shortcut","granted":["fitness.write"],...}}
```

Run a capability:

```bash
curl -s https://hq.jseverino.com/api/v1/capabilities/fitness.import/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"payload":{"documents":[{"name":"health.csv","content":"...","encoding":"text"}]}}'
```

### Routes

| Method | Path | |
|---|---|---|
| `GET` | `/api/v1/` | Who you are and what you were granted |
| `GET` | `/api/v1/capabilities/` | Every capability, flagged `permitted` for this token |
| `POST` | `/api/v1/capabilities/<name>/` | Run one |

`/api/` is exempt from the session-login redirect but **not** from
authentication. An anonymous request gets `401` with a `WWW-Authenticate`
header, never a 302 to an HTML login page — a Shortcut cannot fill one in, and
would record the redirect as success while importing nothing.

### Errors

Always `{"ok": false, "error": {"code", "message", "details"}}`.

| Status | Means |
|---|---|
| `401` | No token, or it failed verification. Mint a new one. |
| `403` | Verified, but this client was not granted that capability. Fix its scope. |
| `404` | No such capability on this deployment. |
| `409` | The command ran and the domain refused it. |
| `503` | `SEVERINO_API_RESOURCE` is unset here. |

### No idempotency key

Deliberate. The write this exists for — an import — is already idempotent by
content hash inside the domain, so a Shortcut retried on a dropped connection
reports a duplicate rather than creating one. A key here would be a second
mechanism guarding something already guarded.

## Recipe: one-tap health sync

In Shortcuts on iOS:

1. **Health Export CSV** action → produces the workout CSV.
2. **Get Contents of URL** — `https://sso.jseverino.com/api/oidc/token`, POST,
   `Form` body: `grant_type=client_credentials`, `client_id`, `client_secret`,
   `resource=https://hq.jseverino.com/api`, `scope=fitness.write`.
3. **Get Dictionary Value** `access_token` from the result.
4. **Get Contents of URL** —
   `https://hq.jseverino.com/api/v1/capabilities/fitness.import/`, POST,
   header `Authorization: Bearer <the value from step 3>`, `JSON` body:

   ```json
   {"payload": {"documents": [{"name": "health.csv", "content": "<CSV text from step 1>", "encoding": "text"}]}}
   ```

5. **Show Result** — `changed` is how many workouts actually landed.

The client secret sits in the Shortcut. That is a real exposure and the reason
the client is scoped to `fitness.write`: worst case, someone who extracts it
can import workouts. They cannot read an expense, touch a project, or delete
anything.
