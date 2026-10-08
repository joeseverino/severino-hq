# Plugin Architecture

Severino HQ exposes a versioned extension contract for trusted, installable
Django packages. A plugin declares its integration once; HQ derives application
installation, routing, navigation, dashboard projections, global search,
readiness, authorization, and capability-adapter exposure from that manifest.

Plugins are disabled by default. Deployment explicitly allowlists each trusted
entry point with `SEVERINO_HQ_PLUGINS`, using comma-separated
`package.module:attribute` references. Importable code is never discovered and
executed automatically.

```python
from hq_sdk.plugin import NavigationItem, PluginIntegration, PluginManifest


def integration():
    return PluginIntegration(
        capabilities=capability_specs,
        resources=resource_specs,
        connections=connection_specs,
        dashboard=dashboard_cards,
        overview=domain_overview,
        attention=attention_items,
        calendars=calendar_sources,
        outbound=outbound_work,
        health=ready,
    )


plugin = PluginManifest(
    id="example.notes",
    name="Notes",
    version="1.0.0",
    distribution="example-notes",
    source_repository="example/example-notes",
    source_workflow=".github/workflows/admit-plugin.yml",
    api_version=4,
    integration_provider="example_notes.plugin:integration",
    django_apps=("example_notes",),
    url_prefix="notes/",
    urlconf="example_notes.urls",
    navigation=(NavigationItem("Notes", "notes:list", "notes"),),
    token_authenticated_routes=("webhook/",),
    operator_capabilities=("notes.read", "notes.write"),
    mcp_read_capabilities=("notes.read",),
    mcp_write_capabilities=("notes.write",),
)
```

Set `SEVERINO_HQ_PLUGINS=example_notes.plugin:plugin`, install the package in
the deployment image, and run its migrations. `python manage.py plugins` emits
the effective, machine-readable inventory and validates compatibility.

Capabilities a manifest declares are the plugin's own: naming one of the host's
fails at startup. Every plugin view needs a session wherever it is mounted; a
route that authenticates its own requests is listed in
`token_authenticated_routes`.

## The golden path

A plugin imports host behavior only through `hq_sdk`. Its repository owns the
domain; the SDK owns integration mechanics:

| Need | Import from |
| --- | --- |
| Manifest and navigation | `hq_sdk.plugin` |
| Capabilities, principals, strict JSON commands | `hq_sdk.capabilities` |
| Claim-resolution plans and workflow actions | `hq_sdk.workflows` |
| Read resources, strict filters, search projection | `hq_sdk.resources` |
| Capability-gated Django views | `hq_sdk.web` |
| Audit attribution and summary events | `hq_sdk.audit` |
| Work that reaches a network or a provider | `hq_sdk.outbound` |
| Tables, forms, UI projections, global search | matching `hq_sdk.*` module |
| Synthetic siblings and style checks | `hq_sdk.testing` |

Imports from `application`, `core`, or another host application are unsupported:
they couple a private plugin to public implementation details. Run
`python -m hq_sdk.validation src` locally to enforce the boundary.

The shape of that surface is committed as `hq_sdk/contract.json`: every
export's parameters, fields, enum members and public methods, without
annotations so it is identical across the interpreter matrix. A test fails when
the exports drift from the file, because an extension's binding to a renamed
field or a new required parameter is a change this repository cannot otherwise
see. Regenerate it with `python manage.py sdk_contract`, read the diff as the
review, and decide there whether `PLUGIN_API_VERSION` moves;
`python manage.py sdk_contract --check` is the CI form.

Capability input models should inherit `StrictCommand`; unknown JSON keys then
fail at the boundary instead of being silently discarded. Class-based views
inherit `CapabilityRequiredMixin` and declare `required_capability`; function
views use `@capability_required(...)`. Bulk work uses `audit_operation` plus
`record_operation`, so adapter and actor attribution are consistent without a
domain-owned wrapper.

A private repository's `.github/workflows/admit-plugin.yml` is the same in
every extension, because it names nothing about the extension:

```yaml
jobs:
  checks:
    uses: OWNER/severino-hq/.github/workflows/plugin-checks.yml@main
  admit:
    name: Admit
    if: github.event_name != 'pull_request'
    needs: checks
    runs-on: ubuntu-24.04
    environment: admission
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: OWNER/severino-hq/.github/actions/admit-plugin@main
        with:
          app-client-id: ${{ secrets.HQ_APP_CLIENT_ID }}
          app-private-key: ${{ secrets.HQ_APP_KEY }}
```

The extension's identity is declared once, by the package:
`scripts/plugin-identity.py` reads the distribution from `pyproject.toml` and
the plugin id and Django app from the `plugin = PluginManifest(...)` in
`src/<package>/plugin.py`, where `<package>` is the distribution with
underscores. It fails when the two disagree, before anything is built or signed.

The same check runs locally from an extension's checkout, with nothing to
pass: `../severino-hq/scripts/check-plugin.sh`. It syncs and lints the package,
enforces SDK-only imports, runs Django checks, migration drift checks, and
plugin tests, then builds the wheel and installs it with `--no-deps` into a
clean host environment. That last step reproduces production's dependency
boundary and catches a missing host pin before composition. In CI it runs
against host `main`, or against the host branch named like the extension's
branch when one exists, so a coordinated change is checked against its other
half.

## Cordon admission

Production loading is fail-closed. When plugins are enabled with `DJANGO_DEBUG`
off, HQ requires `SEVERINO_HQ_PLUGIN_LOCK` and
`SEVERINO_HQ_PLUGIN_POLICY_SHA256`. The lock inventory must exactly equal the
enabled manifest inventory. Every entry binds the plugin ID, distribution,
installed version, host API version, immutable source commit, wheel SHA-256,
and Cordon policy SHA-256.

The canonical policy is `policy/plugin-admission-v1.json`. Admission requires
the plugin contract and package tests, a dependency lock and audit, secret
scan, wheel SBOM, and wheel vulnerability scan with no fixable high or critical
findings. Private composition CI signs the statement with GitHub OIDC through
Sigstore. Cordon verifies the signature bundle against the exact repository and
workflow identity before installation, then emits the lock entry embedded in
the composed image.

The lock is evidence of a specific artifact under a specific policy. It is not
a claim that arbitrary plugin code or future versions are safe. A changed
wheel, version, workflow, policy, host API, or enabled inventory requires a new
approval and image build.

Admission tests one extension against its pinned host contract. Composition CI
then runs Django's checks and the complete test suite from the assembled image,
with every admitted wheel installed together. The two gates answer different
questions: admission proves an artifact is independently acceptable;
composition proves the accepted set is one coherent application. Duplicate
routes or capabilities, missing host-owned runtime dependencies, migration
conflicts, and tests that assume no sibling exists fail before publication and
deployment.

## Composition

Production runs **one image carrying every admitted plugin**. Plugins do not
build or deploy images: each verifies and admits itself and publishes its signed
bundle, and the host composes them, so deploying one never replaces the others.

A plugin's admission starts the composition itself, and nothing polls for it.
The last step of the host's `admit-plugin` action mints a token as HQ's
GitHub App, asking only for Actions write on this repository, and
dispatches **Compose** with an input that names no
plugin. The token lasts an hour and can do nothing but start a workflow here;
the app's key is the only credential a plugin repository holds, as a secret on
an `admission` environment limited to `main`. A deploy
still waits for a person's approval in the `production` environment, which no
app can give. A dispatch that fails fails the admission, in red, where it
happened, so nothing is missed silently and no schedule backs it up.

```
merge a plugin → its CI admits the wheel and publishes the bundle
               → the admission dispatches the host's composition
               → build → verify → scan → publish → approve → deploy
               → HQ marks the plugin's commit live and comments on its pull request
```

What a composition is made of (host image digest, plugin wheel digests, and the
admission policy) is hashed into a fingerprint and published as a
`composition:fp-…` tag beside the image. An admission's dispatch whose
fingerprint is already published (the same commit admitted twice) stops before
building. Every other trigger rebuilds: a host build, a pull request, and a
hand-run **Compose** (`workflow_dispatch`), which is how
you rebuild the current set on purpose.

The composition reads the plugins' admissions as the same app, through a token
minted for each run that asks only for Actions and Contents read. There is no
personal token to renew.

The composition workflow is the only path to production. It verifies each
signature itself, against the identity built from the declared repository and
workflow, so a plugin cannot widen who may sign for it by editing its own
repository. Entries are merged into one lock by Cordon's lock tool, which
accepts several entries: the host does not reimplement it.
`SEVERINO_HQ_PLUGINS` is derived from the merged lock, because the enabled and
approved inventories must be identical or the host refuses to start.

### Coordinated changes

A change that needs both the host and an extension (a plugin API bump, say)
lands as branches with the same name in each repository, verified together
before either merges:

- An extension pull request's `plugin-checks.yml` resolves the host ref to the
  host branch of the same name when one exists, and to `main` otherwise. An
  explicit `hq-ref` input still wins.
- The host pull request's composition builds each extension's same-named branch
  into a candidate image and runs the composed checks and suite in it. The
  candidate is verify-only: unadmitted wheels, no lock, a local tag, and never
  published, signed or deployed. Extensions without such a branch are reused
  from their verified admissions. The app's Contents read covers
  reading the branches.

Merge the host first; each extension then checks and admits against `main`.

The declared set lives in a repository variable rather than a committed file:
this repository is public and the extensions it composes are not.
`composition/extensions.json` documents the shape.

## Contract boundary

The plugin API is for trusted code that ships with an HQ deployment. External
or untrusted systems integrate through authenticated HTTP or MCP adapters, not
through runtime code loading. Providers contain projection logic only; domain
rules remain in the plugin's application services so web pages, commands,
search, MCP, and future native clients cannot develop conflicting behavior.

`api_version` is required and must be authored as a literal by the extension;
it must never default to the host's current constant. That lets a host loading
an older wheel report the incompatible epoch instead of silently relabeling the
wheel or failing with an unexplained constructor error. Additive manifest fields
remain compatible within a version; removals or semantic changes require the
next API version. Plugin identifiers are stable, reverse-DNS-style names and
must not be reused.

### Public host, private first-party domains

HQ publishes the generic SDK and composition mechanism; a private first-party
package owns its domain language, models, migrations, fixtures, repository
identity, and business rules. The host must not import a private package by
name or commit the production extension inventory. It learns the admitted set
only from deployment-supplied composition metadata and verifies that set
against its signed lock. Public tests use synthetic extensions, while the
assembled private image runs the real suites together.

This boundary also prevents premature abstractions. Code moves into HQ only
when it is genuinely host policy or a reusable primitive: authorization,
capability execution, audit attribution, table behavior, UI vocabulary,
composition, or testing infrastructure. Domain-specific calculations stay in
their private package even if the host is their only current consumer.

Integration providers return one frozen `PluginIntegration` containing lazy,
typed callables for every runtime contribution: capabilities, resources,
connections, dashboard cards, overview, attention, search, calendars, outbound
work, and health. HQ calls
only the projection a surface needs. A plugin manifest carries static install,
routing, navigation, and authority metadata plus exactly one executable entry
point; an extension never registers runtime surfaces independently.

Dashboard cards and their destination pages must describe the same reporting
window. Include the actual window or data cutoff in the card's existing `detail`
when it differs from today or coverage is incomplete; a render timestamp does
not establish data freshness. The domain owns that meaning, so the host never
infers dates from a card label or recalculates an extension's totals.
Attention providers should emit `serious` or `attention` only for a decision
that needs the operator. Use `neutral` or `good` for observations. The title
states what is wrong, with its count where it stands for several, and the body
says what that means. Supply the help the shared card shows: `Insight.actions`
for what can be pressed, `Insight.action` with `Insight.url` for the page the
work is done on (a short label; a sentence is shown as a sentence),
`Insight.workflow` for commands and their check, and `Insight.since` when the
moment it began is known.

Capabilities fail closed too. HQ validates every contributed
`CapabilitySpec` before describing or invoking the registry: names, effects,
required permissions, target kinds and labels, command JSON Schema, duplicate
names, and the handler call signature are all part of the host contract. MCP grants must
also be a subset of the plugin's operator grants. A typo therefore prevents a
composition from passing its checks instead of becoming a production-only
request failure or an accidental authority gap.

Resource providers follow the same pattern. Each `ResourceSpec` may expose a
list handler with a `ResourceQuery` subclass, a detail handler with one stable
identifier, a `SearchDefinition`, or any useful combination. The host validates
names, permissions, query and handler compatibility, identifier contracts,
duplicate resources, and duplicate search scopes at composition startup. API,
MCP, and global search then derive their surfaces from that spec. A standalone
search projection, when it cannot belong to a resource, is emitted from the
same `PluginIntegration.search` callable rather than another manifest hook.
Set `pass_principal=True` when the answer depends on what the caller may see;
both handlers then also receive `principal=`. Set `web_route` to the resource's
list route to make it directly reachable from the Command Center. It must reverse without arguments; HQ checks that contract
at startup and renders plain discovery text as a fail-safe if checks were
bypassed. A capability may set `subject_resource` to that resource's name; HQ
then connects the operation to its domain in both machine discovery and the
operator UI, without a second plugin-owned menu or command inventory. The
Command Center also derives a browser execution form from the command schema.
For targeted commands, set `target_label` and `target_help` to explain the
identifier in operator language (for example, `Record slug`), while
`target_kind` continues to define its machine type. Set `target_query` when the
subject resource can list eligible targets locally; HQ checks the filter against
that resource's strict query contract and turns the result into an authorized
choice control. `execution_notes` may describe the registered read, queue, and
provider boundary shown in the live, zero-network execution preview. Fields named
`idempotency_key` are generated and hidden in the browser; HQ separately wraps
all state-changing browser submissions in durable replay protection. Every
capability whose effect is not `read` accepts an optional `idempotency_key` in
its payload and replays the first result for a repeated key, so a command type
declares that field only when its handler keeps the key itself; HQ then fills
it in when the caller sends none. Plugins
use the authorized `list_resource` and `get_resource` SDK functions for reads;
the host's raw registry and handler callables are intentionally not exported.
For a replacement-style targeted command, `target_initial_fields` names command
fields that HQ should hydrate from the selected resource detail. Selection
performs one authorized local read, carries the record's `updated_at` into the
concurrency safeguard, and never contacts the provider.

Connection providers complete the same declaration chain for external systems.
Each `ConnectionSpec` names a family, its abilities and provider scopes, the
capability needed to inspect it, and a zero-argument provider of cached
`ConnectionInstance` observations. HQ derives the Connections workspace,
Command Center entry, HTTP catalog, and MCP tools from that one spec. The
provider is invoked only after authorization and must read local state: it must
not make a network request merely because a user opened discovery. Instances
may expose status, safe endpoint text, granted scope names, targets,
dependencies, and small facts, but never tokens, credentials, authorization
headers, secret values, or private endpoint URL parts. An endpoint is
display-only metadata, so URL userinfo, query strings, and fragments are all
rejected rather than filtered by a fallible list of secret parameter names.
`observed_at` is a Python `datetime` and serializes as ISO 8601. Links accept
only local paths and explicit HTTP(S) URLs. Import these contracts and
`describe_connections` from `hq_sdk.connections`; the mutable registry
and raw provider inventory are deliberately host-only.

Treat this provider as part of adding any external API, token, consent, or
keyless data gateway, not as optional Connections-page decoration. The owning
package emits the connection and maps every useful operation to its registered
capability or resource. This keeps new gateways self-describing and makes their
relationships and processes immediately available to Command Center, API, MCP,
and topology without host-specific registration work. Emit a truthful reduced
mode when the gateway still works anonymously; do not hide a usable integration
solely because a token is absent.

Say how authority is proven, not only what it permits. Each ability declares a
`grant` model: `scoped` when the provider issues narrow permissions and the
ability lists the ones it needs in `required_scopes`; `coarse` when the
credential is the whole account and the provider offers nothing narrower;
`none` when the call is keyless. Each instance reports its `credential_model`
from the same vocabulary, plus `rejected` for a credential the provider
refused. HQ derives one evidence state per ability and instance: verified,
whole-account, keyless, unverified, undeclared, unknown, missing or revoked.
Only missing and revoked close the ability; unknown leaves it undecided;
undeclared leaves it usable under HQ's own authorization and says so. The same
derivation yields each connection's lifecycle (configured, reachable, ready,
unauthorized, stale, revoked) against the family's `stale_after_hours`. An
ability that declares nothing is reported as undeclared proof rather than
counted as authorized, so the debt is visible where it is owed.

An ability may set `subject_resource` to the `ResourceSpec` it governs and list
its provider kinds in `governs_kinds`. Command Center then discovers registered
commands against that resource whose target filters include one of those kinds.
For an operation that does not map through a resource, set `capability` to the
exact `CapabilitySpec` name. Composition refuses unknown resource and capability
references. `ConnectionAbility` is the sole authored connection-to-capability
edge: `CapabilitySpec` deliberately does not repeat a reciprocal list that could
drift. Scope coverage decides whether the observed connection can perform
the declared ability; it never fabricates an executable command from token text
alone, so every offered mutation still has a schema, handler, authorization,
audit, and idempotency boundary. Command discovery remains declaration-driven,
so a temporarily missing or stale observation does not erase a supported
workflow; Connections reports its current readiness separately. Searching never
contacts the provider.

Those same instances join the derived topology automatically. An ability's
explicit `governs_kinds` connect it to managed resources, `targets` become
observed destinations, and dependencies with an explicit `resource_key` become
declared-use relationships. Set the optional
`controller_id` only when the instance is an observation emitted by a distinct
controller; direct account integrations leave it empty. It is observer identity,
not credential identity, and must never contain secret material. A plugin needs
no topology template, route, callback, or host edit.

## Shared UI contract

Installable modules inherit HQ's design system and should not ship a parallel
stylesheet for ordinary application structure. The plugin API guarantees these
host templates:

| Template | Contract |
| --- | --- |
| `base.html` | Authenticated shell, navigation, messages, static assets, and security metadata |
| `partials/_page_head.html` | Page title, lede, and optional primary action |
| `partials/_page_navigation.html` | Sticky, responsive local navigation from `hq_sdk.ui.PageNavigation` |
| `partials/_kpi_grid.html` | Responsive linked or static KPI collection |
| `partials/_timeline.html` | Chronological linked events from `hq_sdk.ui.Timeline` |
| `partials/_stacked_bar_chart.html` | Accessible chart from `hq_sdk.ui.StackedBarChart` |
| `partials/_empty_state.html` | Consistent empty state and optional action |
| `partials/_form_field.html` | Label, control, help text, and validation errors |
| `partials/_pagination.html` | Query-preserving paginated navigation |
| `partials/_referenced_by.html` | What names a thing, drawn by `{% referenced_by %}` |

Standard cards, section headings, data tables, list rows, forms, buttons, tags,
and two-column layouts use the classes demonstrated by `example_hq_plugin`.
Plugin templates supply domain content while HQ owns layout behavior, tokens,
responsive rules, accessibility states, and visual evolution. A new shared
pattern belongs in HQ first; copying host CSS or markup into every plugin is a
contract failure. The rules those classes follow (one frame per thing, tables
sized to content, one head, menu, disclosure and filter bar) are in
`docs/DESIGN.md`.

An amount is written by `{{ amount|money }}` in a template and
`hq_sdk.money.money` in Python: "$1,234.50", a true minus sign before the
dollar, `money:"whole"` or `cents=False` for a figure that is scanned. An
overview says what it is built from with `hq_sdk.pages.built_from(plugin.id)`,
a link to the project that names the extension's repository. An extension's
suite checks its templates for a `{# #}` comment left open with
`hq_sdk.testing.unclosed_template_comments`.

Wrap a `.data-table` in `.table-scroll`; HQ preserves horizontal scrolling and
keeps its headings visible through long result sets. The enhancement is visual
only and inert, so the real table remains the single semantic and interactive
source, including sorting, selection, and assistive-technology navigation.

Dense pages expose their information architecture with
`PageNavigation((PageSection("overview", "Overview"), ...))`, include
`partials/_page_navigation.html`, and put the corresponding stable `id` plus
`data-page-section` on each section. HQ then owns compact horizontal overflow,
sticky positioning, scroll-aware current state, and fragment history. Labels
may change; section IDs are durable links.

## Outbound work

A request never waits on anything outside the process. While one is being
served HQ refuses every connection, name lookup, subprocess and sleep, whichever
library makes it, so a view or a capability handler that calls a provider
raises `OutboundInRequest` before the call leaves. An extension does not start
threads, poll, or expose a status endpoint to get round that. It declares the
work, and HQ runs it:

```python
from hq_sdk.outbound import Failed, OutboundWork, ask


def look_up(progress, *, subject, principal):
    progress("Asking the registry.")
    listed = registry.read(subject)  # the network call
    if listed is None:
        raise Failed("The registry lists nothing for this note.")
    Note.objects.filter(slug=subject).update(listed=listed)
    progress(f"The registry lists {len(listed)} entries.", force=True)
    return {"seen": len(listed)}


def outbound_work():
    return (
        OutboundWork(
            "notes.lookup",
            "Look up",
            "Ask the registry what it lists for one note.",
            "notes.write",
            look_up,
            subject_label="Note",
        ),
    )
```

`PluginIntegration(outbound=outbound_work)` is the one declaration. HQ derives
the rest:

| Derived | What it is |
| --- | --- |
| The job | `run` is called on a job's own thread, where reaching out is allowed. One job of a name is live at a time, held by the database, so a second press or a second caller starts nothing. |
| The capability | A capability named `notes.lookup`, with the subject as its target. The API, MCP, the command centre and `hq_call` ask through it with the same authorization, approval policy and denial record as any command, and are answered at once with the job's id. A command line waits for the work, because nothing there outlives the command. |
| The route and status | The control posts to `jobs:ask` and follows `jobs:status`. The extension has no URL, view, template or script for either. |
| The control | `ask("notes.lookup", note.slug)` is the shared Ask (`partials/_ask.html`), standing as the stored job does: live work is followed, failed work says why until it is asked for again. It may stand among a page's actions. One query. |
| The audit entries | Who asked, how it ended, how long it took, and the `seen`, `created`, `updated` and `skipped` counts the work returns. |

The page shows what is stored and puts the control beside it:

```python
context["lookup"] = ask("notes.lookup", note.slug, refresh="#note-listing")
```

```django
{% include "partials/_ask.html" with ask=lookup %}
```

`refresh` names the part of the page the result shows in; it is fetched again
when the work ends. Without one the page loads again.

Three rules keep the work honest:

- **Store, then say.** The work writes its result where the page reads it and
  ends with a sentence (`progress(..., force=True)`), which is what the control
  says when it is done. A failure the operator can act on is `Failed("...")`:
  the sentence is shown where the work was asked for and nothing stored is
  touched. Any other exception is a fault, kept with its traceback.
- **Refuse from what HQ holds.** `refuse(subject)` returns why the work cannot
  be asked for right now, or `""`. The control is drawn unusable with that
  reason and an ask is refused with it before a job is recorded. It runs inside
  requests, so it reads the database and nothing else.
- **One at a time is the duplicate guard.** Work that costs money or is not
  idempotent at the provider relies on the one-live-job rule rather than on a
  check of its own. A name is the unit: declare two names for two things that
  may run together.

A management command a timer runs calls `run_now("notes.lookup", principal=...)`:
the same job row, rule and audit entry, run to its end on the calling thread.
It is refused inside a request.

The SDK exports nothing that enters one of HQ's declared outbound exceptions
or leaves the request, and a test holds that. Work reaches out because HQ runs
it off the request, and only there. A credential the work uses is still read
by the web process; running as a job changes when the call is made, not who
holds the credential.

In an extension's tests, `hq_sdk.testing.held_jobs` records each job a request
starts and holds its work until the test says `run()`, and
`hq_sdk.testing.reaches_out()` called from a network double makes the double
subject to the rule, so a test that drives a view proves the view answered
without reaching it. `example_hq_plugin` and
`hq/platform/application/tests/test_outbound_work.py` show both.

## Derived reads

What an extension contributes to a page many domains share is derived once per
change, by the host. HQ asks `attention`, `dashboard`, `overview` and each
calendar source's `events` through a derivation of its own, named
`extension.<plugin id>.<provider>`: the answer is kept until a table the
provider was seen to read is written, every request in between is answered
from what is stored, and after a write the answer is derived again before the
next request asks. An extension does nothing to be kept, and keeps three rules
so that what is stored is true:

- **Answer from rows, the clock and the demo switch.** HQ sees the tables a
  provider reads and counts every write to them. It cannot see a module-level
  cache, a file, the environment or the signed-in person, so an answer that
  depends on one of those is served to a request it is wrong for.
- **Return a value that pickles and compares equal.** Dataclasses, dicts,
  tuples, dates and decimals do; a lambda, a generator held inside the value,
  an open file or a lazy queryset does not. An answer that cannot be kept is
  derived on every request, and the log says so.
- **Expect the clock a minute late at most.** HQ cannot see where an extension
  reads the clock, so a kept answer stands one minute and never past local
  midnight. A count of days turns on time; an age worded in minutes may be a
  minute behind.

`hq_sdk.testing.providers_derived_again(plugin_id)` holds an extension to
them: with its records in place, it names every provider that is not answered
from what is stored.

```python
def test_every_provider_is_answered_from_what_is_stored(self):
    self.assertEqual(providers_derived_again("example.notes"), [])
```

`hq_sdk.reads` is for an extension's own pages. `read_once` shares one read
among the functions assembling a page. `derivation` keeps a fact of the
extension's own across requests, by the same rules:

```python
from hq_sdk.reads import derivation, today


@derivation("example.overview", reads=("example.Session", "example.Goal"))
def overview():
    return _summarise(as_of=today())
```

HQ counts writes to every model table in the writing transaction, an
extension's included, so nothing is invalidated by hand and nothing is served
older than its rows. Asking `today`, `reached`, `passed`, `since` or `whole`
also derives the answer again at the moment one of them would change. An
extension does not keep a process-level cache, a `cache.set` or a timestamp
table beside this.

## References

A record names a thing in HQ one way, whoever owns either end. A reference is
the text `kind:identity`: `machine:lab-1`, `zone:example.com`,
`asset:<slug>`, `project:<slug>`, `expense:<id>`, a certificate by its registry
kind and key, or a row of a model by that model's label and key. `hq_sdk.references`
is the whole surface:

```python
from hq_sdk.references import CERTIFICATE, Referable, ReferenceField


class Note(models.Model):
    title = models.CharField(max_length=200)
    review_on = models.DateField()
    # What the note is about, and what it was called when this was saved.
    about = ReferenceField(
        kinds=("asset", "zone", CERTIFICATE),
        heading="Notes",
        shows=("title", "review_on"),
        note="review_note",
    )
    about_name = models.CharField(max_length=200, blank=True, default="")

    # Other records may refer to a note.
    referable = Referable(shows=("title",), requires="notes.read")
```

- **A reference is two columns.** The `ReferenceField` and a text column of the
  same name ending `_name`. A migration sees two plain text columns. The system
  check fails a field without its name column or its `heading`.
- **What it may name.** `kinds` lists them; `role` takes any model that declares
  that role; neither takes anything HQ names, less the kinds in `but`.
- **Being referred to.** A model with `referable = Referable(...)` and a
  `get_absolute_url` is a kind of its own, named by its model label. `requires`
  is the capability a viewer needs before a row is named to them: without it
  the picker does not offer the kind and a page shows nothing for the reference.
  `role` lets a host field take the model without naming it.
- **Showing one.** `{% reference note "about" as thing %}` then
  `{% entity thing %}`. A reference that names nothing is its stored name as
  plain text, never an error.
- **Both ends.** `{% referenced_by note %}` on the note's page lists every row
  of every installed model that names it, each under its field's `heading`, in
  one statement. `shows` are the columns a line is built from: its link is
  `str(row)` to `row.get_absolute_url()`, and `note` an attribute that says a
  few words beside it.
- **Writing one.** A `ModelForm` field for it is the shared picker; the view
  takes `ReferencePickerMixin`. A save through `full_clean` stores the name and
  refuses a new reference that names nothing. A sync that copies records in
  stores what `as_stored(text)` answers, which keeps one that names nothing.
- **Reporting one that names nothing.** Return `dangling(Note)` from the
  domain's attention provider.

## Calendar sources

Every domain can put what it holds on HQ's calendar. `calendars` returns
`hq_sdk.calendar.CalendarSource` values; each names a stream of dated things
the domain already holds and answers `events(first, last)` with every
`CalendarEvent` touching those days. The calendar composes every domain's
sources, stores none of them, and shows each in the operator's list of
calendars, checked or unchecked by their own choice.

```python
from hq_sdk.calendar import CalendarEvent, CalendarSource


def calendar_sources():
    return (
        CalendarSource(
            id="notes.reviews",
            label="Note reviews",
            events=reviews_between,
        ),
    )


def reviews_between(first, last):
    for note in Note.objects.filter(review_on__range=(first, last)):
        yield CalendarEvent(f"note:{note.pk}", f"Review {note.title}", note.review_on, url=note.get_absolute_url())
```

A source keeps four rules:

- **Derive, never copy.** Read what the domain holds; the calendar is a view,
  not a store. An event links to the record it came from.
- **One pass per window.** `events` is called once per view; a query per day
  is a bug. Hold it with a query-count test over a short and a long window.
- **Say only what is true.** An event's `state` (`done`, `planned`, `missed`)
  is a claim; a day the domain cannot speak for emits nothing.
- **Mind the noise.** History (what happened, rather than what is coming)
  starts unchecked (`shown=False`); a dot (`mark=True`) is for what is many a
  day and read by colour.

`ends` is exclusive, as in iCalendar: an all-day event over the 3rd to the 5th
ends on the 6th. A timed event's `starts` is an aware datetime. `slot` keeps a
colour the domain's charts already use; otherwise the calendar deals one.

The calendar page is the host's. An extension may place a host page in its
own navigation group by naming its route, `NavigationItem("Calendar",
"calendar:month", "calendar", 11, "Notes")`; where none does, the host lists it
in its own place.

## Routes that authenticate themselves

`token_authenticated_routes` names paths under a plugin's own `url_prefix` that
carry their own request authentication (a bearer token, a signed body) rather
than the session cookie. They are exempted from the session login **redirect**,
not from authentication.

The distinction matters because a 302 to an HTML login page is the wrong answer
for a native or machine client: it cannot render one, and it cannot tell that
response from success. Those routes answer 401 instead, and the view remains
responsible for authenticating the request.

Routes are declared relative and joined to `url_prefix` by the host, so a plugin
can only ever say "these paths of mine". An absolute path, a traversing one, or
one declared without a `url_prefix` to anchor it fails closed at load.

## Native and machine clients

Server composition and remote synchronization are intentionally separate. A
native client should consume a versioned JSON API derived from the same command
and query services used by the web and MCP adapters. Resource identifiers,
idempotency keys, pagination cursors, change cursors, and scoped authorization
belong to that transport contract; they do not belong in templates or plugin
registration. This preserves one domain implementation while allowing web,
automation, and phone clients to evolve independently.
