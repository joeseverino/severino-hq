# AI and HQ

Agents work on HQ's code, and agents operate a running HQ. Both use what
this repository offers anyone: the engineering contract, the mise tasks, the
MCP server and the machine API.

## Working on HQ

An agent reads [`AGENTS.md`](../AGENTS.md), the engineering contract
(`CLAUDE.md` links to it), and runs the same mise tasks a person and CI run,
so a change that passes on a laptop passes in CI
([The gates](Development.md#the-gates)):

| To | Run |
| --- | --- |
| Try a change in a browser, beside servers in other checkouts | `mise run dev:scratch`, on loopback with a database of its own ([Development servers](Development.md#development-servers)) |
| See what a page change looks like | `mise run dev:look`, screenshots as the browser gates draw them |
| Check a change while editing | `mise run fast`, the tests it reaches and the quick gates |
| Check a branch before it is pushed | `mise run check`, every gate CI runs |

Heavy tasks take turns on one lock, so agents in several checkouts never run
two suites at once
([Heavy gates take turns](Development.md#heavy-gates-take-turns)).

Tests and lint rules hold the architecture: the `test_architecture*.py`
modules, `lint:ast-grep` and
`test_the_host_source_never_names_an_installed_extension`. The rest of the bar
is written in `AGENTS.md`:
[Rules that eliminate bug classes](../AGENTS.md#rules-that-eliminate-bug-classes)
and the [definition of done](../AGENTS.md#definition-of-done).

## Operating HQ with agents

An agent reaches HQ through MCP at `/api/mcp/` or through the machine API at
`/api/v2/`. Both are delivery adapters over the same capability layer as the
web UI and the CLI: the API "adds no capability, no domain model and no
business rule" ([API](API.md)).

![An agent's call: its own token, MCP or the machine API, the pause switch, its grant, the operator's policy, then a run, or a hold until a person approves it in the web UI](images/ai/agent-call.png)

<sup>Source: [`docs/images/ai/agent-call.fig`](images/ai/agent-call.fig), pre-rendered with [`brand`](https://github.com/joeseverino/branding-engine) by `mise run fix:figures`.</sup>

### Reads and one write

HQ's MCP server offers one tool for each handler in `HANDLERS`
(`hq/platform/mcp/declarations.py`). All but one read what HQ already holds:
capabilities, resources, connections, topology, findings, recent activity and
health. `export_year_summary` also records its export in the audit log. The
one write, `execute_capability`, runs a capability the same way the API's
`POST /api/v2/capabilities/<name>/` does.

An agent is listed only the tools its grant runs. Each tool declares who may
run it beside its handler (`requires` in `hq/platform/mcp/services.py`), and
that one rule both refuses a call and leaves the tool out of `tools/list`.
Every derived read is one `ResourceSpec` that serves the API, MCP, the CLI and
the SDK alike ([What each surface exposes](API.md#what-each-surface-exposes)).

Every write goes through `execute_capability` in
`hq/platform/application/capabilities.py`:

| Step | Where |
| --- | --- |
| Authorized against the caller's grant, before its payload or target is read | `_authorized_run` calls `authorize_capability` first |
| Validated against the command's schema; an unknown field is an error | `_authorized_run` then calls `validate_command`, and a Pydantic command inherits `StrictCommand` |
| Run once, with every audit event naming the caller and the capability | `_run` sets the actor, interface and operation for the handler |
| Safe to retry | an `Idempotency-Key` header on the API, or `idempotency_key` in the payload on every interface ([Safe retries](API.md#safe-retries)) |

### An agent is its own identity

Each agent signs in with an identity-provider token of its own, so it is an
attributable, revocable principal, and the token's client is the actor in the
audit log ([Security](Security.md#agents),
[API](API.md#setting-up-a-client)).

In the example estate, the operator has blocked the agent from allowing
Tailscale connections. It asked to allow one, and the audit log records the
refusal under its name, beside the change it asked for next:

![The audit log: example-agent's held request to change a DNS rewrite, and its call to allow a blocked Tailscale connection, refused by policy](images/ai/audit.png)

### Holds, approvals and the pause switch

The **Agents** page (`/agents/`) lists each agent HQ has seen, with its
person, their role, what of its grant is in force and what is beyond that
role. Beside each action, a column per agent sets it on, ask me first, or
off, over the default. A rule only narrows the identity provider's grant. An
approval takes a person, never the agent that asked. The operator's switch
pauses every agent and fails closed
([Security](Security.md#what-hq-enforces)).

![The Agents page: agents allowed, a button to block all agents, example-assistant needing approval to act from a laptop, each agent with its person, its permissions and one its last sign-in did not grant, example-agent holding 1 of 16 update streams open and example-assistant all 16, each agent's devices approved, needing approval, refused from an address no tailnet device sent, or blocked, and example-agent's three rules: stopping a container on, record changes ask first, and allowing a Tailscale connection off](images/ai/agents.png)

A held call waits in the audit log's approvals queue (`/audit/?awaiting=1`)
and on the "Needs you" list, where a person approves or rejects it. In the
example estate, the operator asks to see every record the agent changes
first, and the agent asked to answer the photos library's name with the
server's tailnet address. The request shows that address beside the one it
replaces:

![The held request: example-agent asks to change the DNS rewrite for photos.example.com, its answer shown before and after](images/ai/approvals.png)

A client sees each call allowed, held or refused;
[What policy can do to a call](API.md#what-policy-can-do-to-a-call) says how
each is answered.

Tests that hold these promises:

| Promise | Tests |
| --- | --- |
| An agent's delete waits, and happens only once approved; a rule binds its own agent alone, on every surface, and never past the identity provider's grant; nothing governed by policy may set it | `hq/platform/application/tests/test_capability_policy.py` |
| No token approves its own request, even one holding every capability | `hq/platform/application/tests/test_approvals.py` |
| The pause switch fails closed, and no credential can change it | `hq/platform/application/tests/test_agent_access.py` |
| Pausing agents refuses an agent on every surface through one helper, and never pauses a person | `hq/platform/application/tests/test_one_pause.py` |
| Pausing agents or blocking one holds what it already asked for until the brake lifts: its held request stays declinable, and its queued work is not claimed | `hq/platform/application/tests/test_brakes_hold_asked_work.py` |
| A held request is its asker's: a second agent asking the same change holds its own, the change applies once, and an agent reads only its own held requests | `hq/platform/application/tests/test_holds_belong_to_their_asker.py` |
| An agent a person signed in keeps that person's own settings, pins and set-asides, and a machine's own token keeps none | `hq/platform/application/tests/test_person_bound_agent.py` |
| A write the token does not grant is refused on MCP and the machine API alike, and one it grants runs on both with nothing set in HQ | `hq/platform/api/tests/test_agent_grant.py` |

### How much an agent may ask

Each agent is held to a call rate, a number of calls at once on both doors,
and a number of listen streams open on MCP. So one busy or broken agent cannot
slow the others or the operator's brakes. The limits are the
`SEVERINO_AGENT_*` settings in `hq/config/settings.py`, read by
`hq/platform/application/agent_limits.py`. How each door answers a call over
a limit is in [API](API.md#an-agents-limits).

A timed-out call is answered and logged (`mcp.call.timed_out`), but its
thread cannot be stopped: it runs on, may still finish what it was asked, and
holds one of its agent's calls at once until it ends. A write is retried with
the same `idempotency_key`, so a late finish and a retry act once.

One agent's call never queues another's. The counts live in the serving
process, so they hold because the image serves from one process
([Architecture](Architecture.md#an-agents-call)). The Agents page shows each
agent's open streams beside its calls and refusals.

| Promise | Tests |
| --- | --- |
| One agent's slow call never holds up another agent's call | `hq/platform/mcp/tests/test_calls_at_once.py` |
| Work outside a request runs on a thread of its own, its connections closed after it and never inside an open transaction | `hq/platform/core/tests/test_outside_requests.py` |
| Each limit is answered on both doors in their own protocol | `hq/platform/application/tests/test_agent_limits_at_the_doors.py` |
| A flood of refusals is one row a window, with its count | `hq/platform/application/tests/test_limited_denials.py` |
| Two writes at once with one retry key act once | `hq/platform/mcp/tests/test_retry_key_race.py` |
| The image serves from one process | `hq/platform/application/tests/test_one_serving_process.py` |
| An open listen stream holds no thread | `hq/platform/mcp/tests/test_listen.py` |
| A stream held back while its agent is paused hears once the brake lifts | `hq/platform/mcp/tests/test_listen_resume.py` |
| The Agents page counts each agent's open streams against its cap, and says nothing for one holding none | `hq/platform/application/tests/test_agent_activity.py` |

### What an agent can and cannot do

It can do what its token was granted and no more
([API](API.md#hq-verifies-it-does-not-issue)). On MCP and the machine API
alike, a token a person signed in is held within that person's role. What it
keeps (a theme, calendar choices, pins and set-asides) is that person's, and a
machine's own token keeps none ([Security](Security.md#agents),
[API](API.md#what-each-surface-exposes)).

It cannot approve its own held request, change capability policy, or pause
and resume agents. MCP serves none of what
[What HQ does not do](Security.md#what-hq-does-not-do) lists, such as a
shell, raw SQL or a runbook's body.
