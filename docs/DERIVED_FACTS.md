# Derived facts

HQ learns the estate from its connections. An operator adds credentials; HQ
reads what they can see, joins it, and says where each fact came from. Anything
HQ can read is not typed in.

## Kinds of fact

| | Where it lives | Who writes it |
|---|---|---|
| Resource | `control_plane.providers.PROVIDERS` | Declared or adopted; HQ can reconcile it |
| Reading | `control_plane.observations.OBSERVATIONS` | Observed only; HQ never changes it |
| Setting | `config/settings.py` | Deployment-local; derived where HQ holds the fact |
| Registry data | Projects, assets | Derived where a connection knows it, else imported once |

## Readings

A reading is registered once, in the module for its provider under
`control_plane/observations/`:

```python
class PagesProjectRecord(ObservationRecord):
    name: str
    subdomain: str = ""
    domains: tuple[str, ...] = ()

ObservationSpec(
    "cloudflare.pages_project",
    "cloudflare_api",
    "Pages project",
    PagesProjectRecord,
    requires=("Cloudflare Pages Read (account)",),
    hostnames=lambda record: (*record.get("domains", ()), record.get("subdomain", "")),
    title=lambda record: record.get("name", ""),
    relation="Served by Pages project",
)
```

and read by one function in `controller_runtime/providers.py`, registered beside
its definition:

```python
@reads("cloudflare.pages_project")
def list_pages_projects() -> list[dict[str, Any]]:
    ...
```

Rules:

- **The schema is the allowlist.** Only fields the record model names are
  stored. A provider response is never stored whole. Fields that carry secret
  material (tokens, client secrets, environment values, private keys) are never
  named.
- **A refused read raises.** The sweep stores the kind as unreachable with the
  reason, and the page shows the reason with `requires`. Returning `[]` means
  the provider has none.
- **A partial read says so.** A record that could not read one part carries
  `unread: <reason>` for that part rather than an empty value.
- **Join keys are declared.** `hostnames` and `addresses` are how a reading
  attaches to a machine, a service or a zone. Nothing else parses records to
  join them.
- **A reading says what it is to its subject.** `relation` is a short
  present-tense phrase shown beside the record's title on a subject's page:
  "Served by Pages project", "Behind Access". `address_relation` replaces it
  when the record joined through `addresses` rather than `hostnames` (a tunnel
  publishes a hostname; a machine at its origin address runs its connector).
  Blank falls back to `relation`, then to the label. Every registered reading
  sets one; a test enforces it.
- **A reading can link out.** `console` builds the provider console page for a
  record from ids the record stores (an account id, a name), never from a
  secret. A record without them gets no link. A resource kind declares the same
  hook on its provider (`ProviderSpec.console`): a tailnet device links to the
  Tailscale admin console by its tailnet address.
- **A reading can depend on what fronts a name.** `fronted_by` names a resource
  kind whose record must front a hostname for the reading to join it by name:
  an edge certificate serves a name only through a proxied Cloudflare record,
  which says so through `ProviderSpec.fronts`. A zone subject is not narrowed:
  a domain holds its certificates whatever each record does.
- **A reading can say what it supplies.** `facet` is one of the service facets
  (`runtime`, `dns`, `proxy`, `certificate`) or `registration` or `network`: an
  edge certificate supplies a name's certificate, a Pages project its runtime,
  an address registration who holds the network. `expires` and `issuer` read
  those two facts off a record; an issuer is named through
  `control_plane.certificate_authorities`. `short_label` is the label under a
  column that already names the facet ("Edge" under Certificate).
- **One reader per kind, one kind per reader.** The contract tests enforce it
  in both directions. `read_by` is `controller` for a reading a controller
  takes through a credential, and `hq` for one HQ takes itself from a keyless
  public registry (see below).

## Facts about a subject

`application.facts` is the join engine. Every page that attaches a reading to
a subject calls it; nothing else parses a record to join it.

```python
from application.facts import Subject, readings, inventory_about

subject = Subject.of(hostnames=("app.example.com",))      # a service
subject = Subject.of(hostnames=names, addresses=addrs)     # a machine
subject = Subject.of(zones=("example.com",))               # a domain: every name under it

index = readings()                        # every connected reading, indexed once per projection
index.about(subject, facets=("certificate",))   # Joined records: relation, title, expires, issuer, age
index.unread(facets=("certificate",))           # refused kinds, with the reason and requires
inventory_about("cloudflare.zone", subject)     # resource inventory records joined the same way
```

A hostname key joins exactly, a wildcard key joins one label below it, and a
zone joins every name under it. `facts_about` projects the same joins into
facts. The domain page's cards, the service list and pages, and
the topology's reading edges read `readings()`.

`facts_about` projects every reading and resource onto a subject (a
machine, a hostname, a zone) through the declared join keys. Each fact carries
its source kind, the connection that read it, and when. Where two sources
disagree the page shows both. A kind that could not be read appears as not
readable, with its reason. A kind no connection could read (`connected` false on
its inventory row) yields nothing: it is not a failed read.

Only the join keys that name the subject are echoed as facts: a resolver list
naming two machines gives each only its own address.

A covering certificate declaration applies to a name only where the ingress
serving the name names it (`ProviderSpec.certificate`, today a proxy host's
`certificate_resource`). An ingress that names none leaves every certificate
covering the name.

A machine in the tailnet device reading is reached through the tailnet
connection that read it: the record's `connection_ref`, else the tailnet
connections of the controller that took the reading. `machine_catalog` derives
this once; the machine list, the machine page and the connections page read it.

Each kind's inventory is read once per page, not once per subject.

## Relationships

Entity pages are views of one relation graph. `topology.relation_graph` builds
the topology's machines, services, domains, declarations and connections with
the topology's own node and edge code, once per projection, without what only
the topology page needs (health, actions, traffic).
`relationships.relationships_for(node_id)` returns one node's edges in both
directions, grouped by the phrase each says from that node: a service "Runs on"
a machine, the machine "Serves" it, from one edge. `topology.RELATIONS` states
each structural edge kind's phrase, inverse and rank once; a reading edge takes
its phrase from the reading's `relation` and its rank from its facet
(`READING_RANKS`), so what serves a name comes first and an overlay with no
facet (Access) last. Each item is a linked entity, with the connection that
read it and when.

The machine, service, domain and declaration pages render the section from
it, with "See in topology" (`?focus=<node>`), one "Not readable" line linking
to the connections page, and each source's raw records under a disclosure.

## Links

`entity_links.entity_link(kind, identity)` names a thing for every page: its
label, its HQ page when its kind has one (`NODE_KINDS`), otherwise its provider
console (`console` on the reading or resource kind), marked external. The
`{% entity %}` tag renders its answer and marks the mention `data-entity`.
`kind_label` names a kind in a sentence; a raw kind never renders. A connection
links to its row on the connections page. A tailnet policy alias links to the
machine answering at its address (`policy_links`).

## Readings HQ takes itself

RDAP needs no credential, so HQ reads it rather than a controller:
`registry.address` (who holds a public address) and `registry.domain` (a
domain's registrar and expiry), in `control_plane/observations/public_registry.py`.
`application.public_registry.refresh` runs from `manage.py refresh_public_registry`
on an hourly timer, never from a page: it looks at most once an hour, reads only
subjects with no record or one older than a day, a bounded number at a time,
and stores them through the same ingest as a sweep. Each record carries
`read_at`, which is its age. An unconfigured registry is stored as not
connected; one that cannot be reached, as a refused read.

Address subjects are service origins, declared machine addresses and the
public addresses a machine's tailnet client reports among its `endpoints`
(not private, tailnet or documentation). The machine page names each one's
holder. The service list names the holder of a public origin address from this
reading, and the domain page falls back to it for a registration's expiry when
the registrar is not read. Auto-renew is then unknown, and the page says so.

## Machine roles

What a machine does for the estate is derived from the tailnet readings by the
rules in `application.machine_roles.ROLES`: an exit node offers and has approved
both default routes; the tailnet DNS server holds an address the policy names as
a nameserver. The machine list, the machine page's Serves card and search read
`Machine.roles`.

## The estate at a glance

`application.estate.estate_reading` reads the machine catalogue, the service
catalogue, the zone names, the connection rows and the joined readings once per
projection. The dashboard's estate card, the estate action items (offline
machines, managed certificates in their renewal window) and the command
center's estate results read it.

## Machine surfaces

Each derived read is also a registered resource (`application.derived_reads`):
`estate`, `action.items`, `machines`, `domains`, `relationships`, `readings`,
`credentials` and `search`. Each handler calls the function its page calls, so
the API, MCP, CLI and SDK answer what the page shows. See `docs/API.md`.

## Topology

Machines, services and domains are nodes of their own (`application.topology_estate`).
A controller or target that names one folds into it; anything else a
connection reaches stays a target. A machine is reached through the
connections the machine catalogue names. A domain's join keys are every name
under it. Each reading joined to an estate node is an edge from the connection
that read it, labelled with the reading's relation, carrying the reading kind,
its age and each record as a linked entity. It exists only while the reading
does. A reading HQ takes itself comes from its public registry's node; one
whose connection no controller reports now comes from a node naming that
connection.

An estate node's last observed time is the newest of what describes it: the
zone record for a domain, the device reading, telemetry and containers for a
machine, and for every estate node its joined readings and the declarations
that name it. A node no sweep or reading can observe says so; one that can but
has not been read says it is not observed yet.

HQ's own service is a service node too, running on the machine the machine
catalogue names (`application.hq_self`), the same answer as the machine page
and the services list.

## Trust

A derived value that decides access (the OIDC issuer, trusted networks and
proxies, allowed hosts) is a suggestion until an operator pins it, and a
derived set may only narrow what is configured. Drift from a pinned value is a
finding. Everything else is derived and shown directly.

## Credentials

Each connection's credential is least privilege, read-only unless the
connection acts, and HQ manages through it only when its item says so (see
Adoption). `requires` on each reading is the list of what a credential can
be given to see more. Probing is how HQ learns what a credential can read: a
refused kind is a missing permission.

## Adoption

A connection observes unless its 1Password item has a `manages` field set to
`1`, rendered as `<PREFIX>_MANAGES=1`. The controller reports it with each
connection. Whether a credential can write is not read from the provider:
Cloudflare says so only with API Tokens Read.

Only a record read through a connection that manages is adopted, by a sweep or
by an operator. A record that names its connection needs that connection to
manage; one that names none needs every connection of its kind's providers to
manage. Anything else shows everywhere as observed and is never declared.

Stopping managing an adopted record ("Stop managing") stores a `NotManaged` row:
kind, record token, who, when. Sweeps skip it. Managing it again ("Manage this
domain", or adopting the record) clears the row. Both are audited.

A deployment that relies on a sweep adopting sets `manages` to `1` on each
connection it adopts through; for Cloudflare that is the production Cloudflare
DNS connection. Without it nothing new is adopted. Declarations that already
exist are kept.

## One-time import

Facts no connection knows (costs, purchase dates, notes) enter through a single
import: atomic, audited per record, idempotent by slug, with a dry run.

The `hq.import` capability, or `manage.py import_registry`, takes
`{"projects": [...], "assets": [...]}`. Each record carries the fields of
`project.upsert` or `asset.upsert` and a slug. The whole document is validated
before anything is written, every record is upserted through those use cases
in one transaction, and a record refused while writing rolls back the rest.
`--check-only` runs the import and rolls it back. A field a record leaves out
keeps its stored value; an unchanged record is not saved. Each record gets one
`imported` audit event, and the import one more with the counts. It requires
what both upserts require.

### Precedence of derived fields

A derived value wins over an imported one. `Project.public_url` is the case
today: it is how `application.services.projects_by_hostname` ties a project to
a service and how `content.content_sync.index_project` finds the site that
serves the content index, and a Pages project can supply it.

- The import sets a derivable field only when the stored value is blank or
  already equal.
- A stored value that differs is kept. The import reports it per record as
  `kept` (field, kept value, offered value), in the summary and in the audit
  event, and does not fail.
- A derivation writes over an imported value. Where a reading and a stored
  value disagree, the facts panel shows both.

Derivable fields are listed in `application.registry_import.DERIVED_FIELDS`.
A field a connection starts to derive is added there.

## Credential lists

`requires` is a tuple of exact permission names, as the minting scripts use
them: Cloudflare as `<permission group name> (<account|zone>)`, Tailscale as the
bare scope name. A part of a reading that needs more names it in `unread`.
`scripts/cloudflare-observer-permissions.txt` and
`scripts/tailscale-observer-scopes.txt` hold every reading's `requires` plus
the reads in `control_plane.credential_reads` that are not readings yet; a test
keeps them equal.

## What a credential can see

Each connection's row folds in what its provider's credential can see: every
reading it feeds and every resource kind a sweep reads. A kind with an
`unobserved_reason` is read by no sweep and is not listed. Each kind is one of:

- **Readable**, with its record count and age.
- **Refused**. The controller reports `refusal` on the kind: `permission` when
  the provider refused one permission, and the page offers "Add <requires> to
  see <label>"; `credential` when it refused the credential itself (invalid,
  expired, locked out, used from a refused location), and the row says so once
  for the connection.
- **Unreadable**: the read failed for another reason, shown with its error.
- **Not connected**: the controller holds no connection that can read it.
- **Never swept**.

Providers with no connection are listed once under "Not connected", with the
readings a connection would let HQ see. Each reading is also listed under the
connection's "Can do".
