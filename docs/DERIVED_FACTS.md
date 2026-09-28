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

and read by one function registered one of two ways, and no other
(`controller_runtime/test_reader_registration.py` holds it): a core reader in
one of the controller's integration modules (`controller_runtime/cloudflare.py`,
`tailscale.py`, `host_readings.py`), registered beside its definition:

```python
@reads("cloudflare.pages_project")
def list_pages_projects() -> list[dict[str, Any]]:
    ...
```

or, for an integration with a controller adapter, in the adapter's `readings`
map (`control_plane/provider_adapters/`), which admits only a registered kind
read through a connection the integration holds (its definitions' connections,
or `reads_through` for an integration whose resource kinds the controller core
still holds). A reading through such a connection is always the adapter's:

```python
ControllerIntegrationAdapter(..., readings={"adguard.client": read_clients})
```

A reader iterates the provider's connections (`runtime.connection_refs`) and
stamps each record with its `connection_ref`, so a reading is attributed to the
connection that took it.

Rules:

- **The schema is the allowlist.** Only fields the record model names are
  stored. A provider response is never stored whole. Fields that carry secret
  material (tokens, client secrets, environment values, private keys) are never
  named.
- **A refused read raises.** The sweep stores the kind as unreachable with the
  reason, and the page shows the reason with `requires`. Returning `[]` means
  the provider has none.
- **A partial read says so, as a part.** A reading read in pieces declares
  them in `parts` (`ReadingPart(name, label, requires)`, each `requires` a
  subset of the reading's). A reader that cannot read one calls
  `refuse_part(part, exc, scope=..., connection_ref=..., address=...)`
  (`control_plane/provider_adapters/parts.py`); the sweep reports
  the kind's `refused_parts` beside its records and HQ stores them
  (`control_plane.reading_parts`). A refused part is never a record or a record
  field, so a count never includes it. The whole kind on one zone, or on one
  machine (`scope` its name, `address` its address), is the part `""`. Resource kinds swept in parts declare them on their provider, `ProviderSpec.parts` (a zone's
  TLS posture and registration, the tailnet policy's settings, DNS and
  services). A refused part reads as **Partly refused** in credential sight,
  its permissions join the missing-permissions finding and the mint, it is an
  unreadable fact on every subject it could hide something about, and a path
  hop that depends on it says "<part> not read: missing <permissions>". The
  sweep's own result (`kinds`) names each kind's state in the same words,
  `Not connected` for a kind no connection reads.
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
- **A reading can name services and route them.** `names_services` makes each
  of a record's hostnames a service, marked observed when nothing declares it
  (a Pages custom domain, an Access application, a tunnel ingress, a redirect).
  `redirects_to` reads the host a record sends its names to (the
  `cloudflare.redirect` reading of zone redirect rules and forwarding page
  rules), which becomes a `redirects_to` edge between the two services.
  `upstream` reads where a record hands one hostname on (a tunnel's ingress
  service).
- **A reading can say something about the connection that took it.** `facts`
  returns `(key, value)` pairs that land on that connection's topology node (a
  record naming no connection speaks for every connection of its provider),
  where a finding rule reads them: AdGuard's filtering being off, a plain
  upstream, a rewritten name nobody looked up.
- **One reader per kind, one kind per reader.** The contract tests enforce it
  in both directions. `read_by` is `controller` for a reading a controller
  takes through a credential, and `hq` for one HQ takes itself from a keyless
  public registry (see below).

### The DNS query log

`adguard.query_summary` is the only reading taken from a log of what people do,
so it is reduced on the controller, in memory, before anything is returned: one
record per name AdGuard rewrites (the estate's own names), with the count, how
many were blocked, the distinct clients (the busiest ten kept), when it was last
seen and the span covered (up to a day, bounded by pages read). A rewritten name
nobody looked up gets a record with a count of zero. No other queried name, no
per-query time, answer or upstream leaves the controller, and the schema admits
nothing else. It joins services by name only, never a device by address, so no
page lists what one device looks up. An anonymized log keeps the counts and
refuses the `clients` part; a disabled log is a refused read. A posture
endpoint `adguard.dns` cannot read is that part refused (`upstreams`,
`filtering`, `querylog`, `rewrites`), the rest kept.

### Docker and proxy readings

The Portainer credential feeds `portainer.environment` (each environment as a
machine: type, status, agent and Docker versions), and per reachable
environment `portainer.network`, `portainer.volume` (named volumes and bind
mounts, with the containers mounting each), `portainer.image` (tags, digests
and the containers running each, with the reference each was started from) and
`portainer.compose_project` (Portainer stacks and compose labels). Each record
names `host` and `host_address`, so it joins the machine by either. One
container list per environment feeds all of them within a sweep
(`control_plane/provider_adapters/portainer_readings.py`, declared by the
Portainer adapter). An environment that cannot be read is the whole reading
refused on that machine (`scope` the machine, `address` its address), so it
shows on that machine's page; every environment refusing raises.

The NPM login feeds `npm.certificate` (joined to the names NPM serves with it,
not every name it covers), `npm.redirect` (the same `redirects_to` as
`cloudflare.redirect`, answered at the ingress: `facet` `proxy`),
`npm.stream`, `npm.access_list` (address rules and login names, never
passwords) and `npm.dead_host` (`control_plane/provider_adapters/npm_readings.py`,
declared by the NPM adapter). `requires` names NPM's own permission areas
(`certificates: view`). A host list the login may not see is a refused part:
of `npm.certificate` (which names each certificate serves) and of
`npm.access_list` (which names it guards). A 401 is a refused credential and a
403 a missing permission (`control_plane/provider_adapters/refusals.py`).

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

## Request paths

`paths.path_to(hostname)` walks what a request for a name meets, one route per
DNS answer that names it: the public record (proxied or not) or the internal
rewrite, then the provider edge, a redirect, a Pages project or a tunnel, the
proxy host or Caddy route, and the machine and container behind it. Which kinds
answer each hop is read from the registries (a provider's `facet`, `origin`,
`fronts` and `served_certificate`; a reading's `facet`, `fronted_by`,
`redirects_to` and `upstream`). Each hop names the reading and connection it
came from; a kind no connection reads falls back to its declarations, marked
declared. A hop HQ cannot see reads "not read: <kind>, because <reason>".

A redirect reading with the `proxy` facet (NPM) answers at the machine's
ingress; one with no facet (Cloudflare) at the edge. A `proxy` reading that
neither redirects nor forwards (an NPM 404 host) ends the path at "Answers 404".
A reading with `upstream` and no facet (an NPM stream) is a route of its own
beside the web route, one per forward on the same connection's ingress, with
its `port`; `depends_on` leaves those routes out, since a second way in is not
a part the name needs.

The certificate is per hop: a proxied name shows the edge certificate, then the
origin certificate the proxy behind it serves; a tailnet name shows the
certificate the proxy serves. A Caddy route states the certificate it serves
when the edge loads one from a file whose names cover the route (the edge
target's read-only `certificate` operation hands over the public leaf only), and
otherwise why it cannot (`ServedCertificate.unread`). A container name an
ingress forwards to resolves on the ingress's own machine first. HQ's own names end at HQ, with the address and
port the request reached it on. `paths.hq_path(request)` starts from the
caller's device and joins each hop to the request (`request_path.joined`): the
address it came from, the proxy that forwarded it and its own headers, the name
it asked for, an Access assertion. Each hop is proven, contradicted (a finding
with its fix) or not shown by the request, and carries the admission layers
decided there. `request_path.request_path(request)` is the connection page's
one projection and the `request.path` read. HQ also records which devices
reached it (`hq.request_path`, read by `request`: per source device, how it
arrived, last seen and a seven-day count, written at most once per source per
`SEVERINO_REQUEST_PATH_SECONDS`); machine pages show it. The `paths` read resource returns
the same path to every adapter, and the service page renders it: a summary, the
path hop by hop, what it depends on and what depends on it, then the parts and
raw readings.

## Relationships

Entity pages are views of one relation graph. `topology.relation_graph` builds
the topology's machines, services, domains, declarations and connections with
the topology's own node and edge code, once per projection, without what only
the topology page needs (health, actions, traffic).
`relationships.relationships_for(node_id)` returns one node's edges in both
directions, grouped by the phrase each says from that node: a service "Runs on"
a machine, the machine "Serves" it, from one edge. `topology_model.RELATIONS` states
each structural edge kind's phrase, inverse and rank once; a reading edge takes
its phrase from the reading's `relation` and its rank from its facet
(`READING_RANKS`), so what serves a name comes first and an overlay with no
facet (Access) last. Each item is a linked entity, with the connection that
read it and when.

A reading can name containers as well as hostnames and addresses (`containers`
on its `ObservationSpec`, keyed by `contract.container_key`), and a declared
container is a subject with that key, so every Docker reading that names one
(its networks, mounts, image, runtime and compose project) joins to it as it
joins to its machine, phrased from the container's side (`container_relation`:
"On network"). A reading may describe a record in a few words (`describe`: a
network's driver and subnets), shown beside it on every relationship row. Two
declared containers on a user-defined Docker network (not `bridge`, `host` or
`none`) also have a `talks_to` edge naming the network
(`application/docker_estate.py`). The machine page's Docker bands (environment,
compose projects, networks, where data lives, images) and the service page's
"Who is allowed" band read the same joined records
(`application/docker_sections.py`, `application/npm_sections.py`).

Findings from these readings: `container-image-behind` (the machine's own tag
now names a different image than the container runs; no registry is asked),
`container-image-untagged`, and `certificate-expiring` for any
certificate-facet reading within 21 days of expiry, by `expiry.days_until`,
on the connection that read it (`application/certificate_expiry.py`).

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
`application.public_registry.refresh` runs from `manage.py refresh_public_registry`,
never from a page: the host starts it once a day, and at once when HQ rings
its doorbell (`/run/severino-hq/registry-doorbell`, watched by
`severino-hq-public-registry.path`) because a sweep found an image or a digest
HQ has not read. It reads only subjects that are due, a bounded number at a
time, and stores them through the same ingest as a sweep, so a run with nothing
due makes no request. Each reading stands as long as what it reads is slow to
change (`READ_EVERY`): who holds an address or registers a domain, a week; tags,
releases and vulnerabilities, a day; a digest's attestations, forever, since a
digest never changes. A subject that could not be read is retried after an
hour. Each record carries `read_at`, which is its age. An unconfigured registry is stored as not
connected; one that cannot be reached, as a refused read.

Address subjects are service origins, declared machine addresses and the
public addresses a machine's tailnet client reports among its `endpoints`
(not private, tailnet or documentation). The machine page names each one's
holder. The service list names the holder of a public origin address from this
reading, and the domain page falls back to it for a registration's expiry when
the registrar is not read. Auto-renew is then unknown, and the page says so.

The same refresh reads what a running container's image is and whether it is
current, also keyless. `registry.image` is each image a container runs, read
from its own registry over the OCI distribution API with an anonymous pull
token (`application.oci_registry`): the tags that carry a version, and the
build labels naming the repository it is built from. `registry.upstream` is
each GitHub repository an image names that way, or lives under in `ghcr.io`:
its recent releases and its published security advisories with their version
ranges and fixes (`application.github_public`). Upstreams are chosen from the
images just read, and fewer are read per run, because GitHub's anonymous limit
is shared with Watching. Every host a registry read touches is named by someone
else (the image, the registry's token challenge, a redirect to a CDN), so each
request is HTTPS to a name resolving only to public addresses, a token never
follows a redirect to another host, and a response is size-capped.

`application.containers` joins them per running container: the version it runs
(the tag, or the tag its digest was pulled as), newer tags of the same shape
(`1.31.3-alpine` is compared only with `N.N.N-alpine`), and the advisories whose
range holds that version, where a stated fix at or below it clears an
open-ended range and a range that cannot be read is "not known", never "not
affected". HQ's own image answers from the GitHub App's reading of its
repository instead: the production deploy and whether it passed its checks.
The containers page, each container's page and each machine's container table
show one `Standing`; the action queue holds one item per affected image and
version, and one for every image with a newer release. The same refresh
resolves, with a `HEAD` of the manifest, the digest each running tag names now
and the digest of the newest tag of its shape: what an upgrade would pin, and
whether a running tag has been rebuilt since it was pulled (`moved_to`). A
digest that could not be read makes its image due again on the next run.

`registry.digest` is what the publisher attached to each of those digests, read
once (`application.oci_registry.attestations`, `application.attestations`): the
in-toto statements BuildKit puts beside a platform's manifest, from which HQ
keeps the SBOM's package URLs and the SLSA provenance's source, commit, builder
and base images. Metadata beside the image, never a layer of it: nothing is
pulled and nothing runs. The statements are the publisher's word and unsigned,
and the page says "states", not "proves". `registry.vulnerabilities` is those
packages checked against OSV (`application.osv`), keyless, one batch per
thousand packages, each vulnerability's detail read once and kept until OSV
modifies it: the id, the package and version installed, the versions that fix
it, and a severity where the database gives one.

What an image is built from is known, most trusted first, from the container's
declaration (`source`, for an image that does not say), the image's label, its
provenance's stated source, then the GitHub registry it lives in
(`containers.source_of`); the page says which. The upstream reads follow it.
An image is vulnerable when an advisory on its source matches its version, or
when a critical or high vulnerability in its packages has a fix published; an
unrated or unfixed one is shown, never raised.

`portainer.runtime` is how each container is run, from Docker's inspect through
Portainer: user, privilege, capabilities, host namespaces, security options,
devices, mounts, port bindings, limits, restart policy and health check. It is
built field by field, so the environment, the command line and labels, which
the inspect document also carries, are never stored. `application.container_standard`
holds each container to a standard over it (`application.standards`, the same
primitive as the GitHub posture): reach over the machine (privilege, the Docker
socket, the host's process namespace, confinement off, a writable system path,
a machine-level capability) is serious and queued, one item per check naming
every container that fails it; hardening (a non-root user, no-new-privileges,
its own network, bound ports, a memory limit, a health check) is shown and not
queued. `application.supply_chain` holds the image to a second standard on the
same primitive: pinned to a digest, its source known, its build described, its
packages listed, no fixable serious vulnerability, no advisory against its
version, its tag still naming what runs. A check HQ could not read is
unavailable, never failed.

`application.upgrades` plans an upgrade for every container something newer is
published for, and changes nothing: the target by digest, the size of the move,
the advisories and package vulnerabilities it clears or would bring (the two
digests' OSV readings compared), the writable mounts that would be
snapshotted, what would verify it (its health check, a request to each name it
serves), the steps an upgrade would take, and every reason it cannot go ahead
yet as an id and a reason (`not-declared`, `runtime-unread`, `no-target-digest`,
`no-apply-path`, `target-affected`, `own-pipeline` for HQ's own image). A
separate list says what keeps it from applying without a person: what is not
known about the target (`target-unread`, `no-provenance`, `no-package-list`,
`not-scanned`), and anything but a vetted patch waits.

Both are registered read resources, so an agent asks what the pages ask:
`containers` (`machine:container`) and `upgrades` (the same address), through
the API and the MCP like every other resource.

## GitHub

The controller reads `github.repository` through HQ's GitHub App: one
installation token per repository, read-only permissions only, minted for each
read. A record is a repository's head and its checks, open pull requests,
workflows waiting on an approval, deployments and the steps each ran before
deploying whose names begin "Verify", branch rules, environments, runners,
alert counts by severity, admission artifacts, who has access (collaborators,
deploy keys, the default workflow token, Actions settings), and the names of its
Actions variables. Each part is refused on its own where the repository's plan
offers none, and says so.

`application.github_estate` joins a project to its repository by URL and puts
what waits on a person on the action queue: a deploy held for approval, a
failing default branch, a deploy that failed its own verification, serious
alerts, an admission about to lapse.

`application.github_posture` holds every repository to a standard derived from
the same record: what every repository is held to (only you have access, deploy
keys read-only and in use, a read-only workflow token that approves nothing,
actions pinned to a commit, Dependabot security fixes, no Actions variables),
and, for a public one, what GitHub gives it besides (pull requests required,
the default branch protected from force pushes and deletion, secret scanning,
push protection, code scanning). What a plan does not offer is "not
available", never a failure. The action queue holds one item per check not
met, naming every repository that misses it.

Watching reads the signed-in person's own GitHub profile and the repositories
they star from GitHub's public API, credential-free: whose profile is the login
their sign-in claims (`application.linked_accounts`), never one typed into HQ.

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
that read it (a tailnet device is a subject by its addresses too), labelled
with the reading's relation, carrying the reading kind,
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
today: it is how `application.published_sites.projects_by_hostname` ties a project to
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
bare scope name. A part of a reading names the subset it needs in `parts`.
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
- **Partly refused**: read, with a declared part refused; the missing
  permissions and the parts they would show are named.
- **Unreadable**: the read failed for another reason, shown with its error.
- **Not connected**: the controller holds no connection that can read it.
- **Never swept**.

A readable kind older than its cadence allows reads **Out of date**
(`application.freshness`). The services page's provider readings use the same
words, through `credential_sight.standing`.

A connection whose probe fails reports why, as `failure`, classified where the
request failed (`contracts.failure_of`): `credential` or `permission` (HTTP 401
or 403, or the provider's own refusal), `address` (the address answered with a
sign-in page, a web page or a redirect elsewhere, not the API), or `network`
(nothing answered). The `connection-not-answering` finding's fix follows it: the
mint command, the item field that holds the direct API address, or the machine
and route to check.

Providers with no connection are listed once under "Not connected", with the
readings a connection would let HQ see. Each reading is also listed under the
connection's "Can do".
