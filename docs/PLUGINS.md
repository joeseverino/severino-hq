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
from hq_sdk.plugin import NavigationItem, PluginManifest

plugin = PluginManifest(
    id="example.notes",
    name="Notes",
    version="1.0.0",
    distribution="example-notes",
    source_repository="example/example-notes",
    source_workflow=".github/workflows/admit-plugin.yml",
    django_apps=("example_notes",),
    url_prefix="notes/",
    urlconf="example_notes.urls",
    navigation=(NavigationItem("Notes", "notes:list", "notes"),),
    dashboard_provider="example_notes.projections:dashboard_cards",
    overview_provider="example_notes.projections:domain_overview",
    capability_provider="example_notes.capabilities:specs",
    search_provider="example_notes.projections:search_definitions",
    health_provider="example_notes.health:ready",
    token_authenticated_routes=("api/v1/",),
    operator_capabilities=("notes.read", "notes.write"),
    mcp_read_capabilities=("notes.read",),
    mcp_write_capabilities=("notes.write",),
)
```

Set `SEVERINO_HQ_PLUGINS=example_notes.plugin:plugin`, install the package in
the deployment image, and run its migrations. `python manage.py plugins` emits
the effective, machine-readable inventory and validates compatibility.

## The golden path

A plugin imports host behavior only through `hq_sdk`. Its repository owns the
domain; the SDK owns integration mechanics:

| Need | Import from |
| --- | --- |
| Manifest and navigation | `hq_sdk.plugin` |
| Capabilities, principals, strict JSON commands | `hq_sdk.capabilities` |
| Capability-gated Django views | `hq_sdk.web` |
| Audit attribution and summary events | `hq_sdk.audit` |
| Tables, forms, UI projections, global search | matching `hq_sdk.*` module |
| Synthetic siblings and style checks | `hq_sdk.testing` |

Imports from `application`, `core`, or another host application are unsupported:
they couple a private plugin to public implementation details. Run
`python -m hq_sdk.validation src` locally to enforce the boundary.

Capability input models should inherit `StrictCommand`; unknown JSON keys then
fail at the boundary instead of being silently discarded. Class-based views
inherit `CapabilityRequiredMixin` and declare `required_capability`; function
views use `@capability_required(...)`. Bulk work uses `audit_operation` plus
`record_operation`, so adapter and actor attribution are consistent without a
domain-owned wrapper.

Private repositories call the reusable `plugin-checks.yml` workflow with only:

```yaml
with:
  plugin-reference: example_notes.plugin:plugin
  django-app: example_notes
```

The host-owned check syncs and lints the package, enforces SDK-only imports,
runs Django checks, migration drift checks, and plugin tests, then builds the
wheel and installs it with `--no-deps` into a clean host environment. That last
step exactly reproduces production's dependency boundary and catches a missing
host pin before composition. Existing plugin-owned `scripts/check.sh` remains
a temporary compatibility path while repositories migrate.

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
bundle, and the host composes them. When a plugin shipped its own image,
deploying one replaced the others and silently dropped them.

A plugin cannot trigger the composition. Signalling one repository from another
needs a credential, and a private repository holding a long-lived token that can
start builds in this public one is a worse trade than a few minutes of latency.
So the host asks rather than being told: **the composition workflow runs on a
schedule and rebuilds only when its inputs changed.**

What a composition is made of — host image digest, plugin wheel digests, and the
admission policy — is hashed into a fingerprint and published as a
`composition:fp-…` tag beside the image. A scheduled run whose fingerprint is
already published stops before building, so a tick with nothing to do costs one
resolution and deploys nothing. The registry holds that state because it already
holds the only copy that matters.

```
merge a plugin → its CI admits the wheel and publishes the bundle
               → the host's scheduled composition sees new wheel digests
               → build → verify → scan → publish → deploy
```

Only scheduled runs stop early. Running **Compose and deploy extensions**
by hand (`workflow_dispatch`) always rebuilds, which is how you deploy a plugin
immediately instead of waiting for the next tick.

The workflow also listens for a `repository_dispatch` of type
`extension-admitted`, for a plugin that is ever given a credential to announce
itself. **Nothing sends it today.** It is kept because the schedule makes it
safe to have an unused fast path — a missed signal is picked up on the next tick
rather than leaving production a release behind.

The composition workflow is the only path to production. It verifies each
signature itself, against the identity built from the declared repository and
workflow, so a plugin cannot widen who may sign for it by editing its own
repository. Entries are merged into one lock by Cordon's lock tool, which
already accepts several entries — the host does not reimplement it.
`SEVERINO_HQ_PLUGINS` is derived from the merged lock, because the enabled and
approved inventories must be identical or the host refuses to start.

The declared set lives in a repository variable rather than a committed file:
this repository is public and the extensions it composes are not.
`composition/extensions.json` documents the shape.

## Contract boundary

The plugin API is for trusted code that ships with an HQ deployment. External
or untrusted systems integrate through authenticated HTTP or MCP adapters, not
through runtime code loading. Providers contain projection logic only; domain
rules remain in the plugin's application services so web pages, commands,
search, MCP, and future native clients cannot develop conflicting behavior.

`api_version` fails closed on incompatibility. Additive manifest fields remain
compatible within a version; removals or semantic changes require the next API
version. Plugin identifiers are stable, reverse-DNS-style names and must not be
reused.

### Public host, private first-party domains

HQ publishes the generic SDK and composition mechanism; a private first-party
package owns its domain language, models, migrations, fixtures, repository
identity, and business rules. The host must not import a private package by
name or commit the production extension inventory. It learns the admitted set
only from deployment-supplied composition metadata and verifies that set
against its signed lock. Public tests use synthetic extensions, while the
assembled private image runs the real suites together.

This boundary also prevents premature abstractions. Code moves into HQ only
when it is genuinely host policy or a reusable primitive—authorization,
capability execution, audit attribution, table behavior, UI vocabulary,
composition, or testing infrastructure. Domain-specific calculations stay in
their private package even if the host is their only current consumer.

Capability providers fail closed too. HQ validates every contributed
`CapabilitySpec` before describing or invoking the registry: names, effects,
required permissions, target kinds, command JSON Schema, duplicate names, and
the handler call signature are all part of the host contract. MCP grants must
also be a subset of the plugin's operator grants. A typo therefore prevents a
composition from passing its checks instead of becoming a production-only
request failure or an accidental authority gap.

## Shared UI contract

Installable modules inherit HQ's design system and should not ship a parallel
stylesheet for ordinary application structure. API v1 guarantees these host
templates:

| Template | Contract |
| --- | --- |
| `base.html` | Authenticated shell, navigation, messages, static assets, and security metadata |
| `partials/_page_head.html` | Page title, lede, and optional primary action |
| `partials/_kpi_grid.html` | Responsive linked or static KPI collection |
| `partials/_timeline.html` | Chronological linked events from `hq_sdk.ui.Timeline` |
| `partials/_stacked_bar_chart.html` | Accessible chart from `hq_sdk.ui.StackedBarChart` |
| `partials/_empty_state.html` | Consistent empty state and optional action |
| `partials/_form_field.html` | Label, control, help text, and validation errors |
| `partials/_pagination.html` | Query-preserving paginated navigation |

Standard cards, section headings, data tables, list rows, forms, buttons, tags,
and two-column layouts use the classes demonstrated by `example_hq_plugin`.
Plugin templates supply domain content while HQ owns layout behavior, tokens,
responsive rules, accessibility states, and visual evolution. A new shared
pattern belongs in HQ first; copying host CSS or markup into every plugin is a
contract failure.

## Routes that authenticate themselves

`token_authenticated_routes` names paths under a plugin's own `url_prefix` that
carry their own request authentication — a bearer token, a signed body — rather
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
