"""What HQ can reach, and what each of those things can act on.

HQ holds no credential, only each connection's report: the controller renders
the vault into its own environment and reports whether each endpoint answers
and what it sees. So this module reads a cache, never a secret, and the page is
a view of the vault with no second list to drift from it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, NotRequired, TypedDict

from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS, connection_credential
from hq.domains.control_plane.models import ProviderConnection
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS, observer_abilities, registry_label, resource_home
lazy from hq.domains.control_plane.names import normalized_hostname

from .answer_fields import Permitted, Pinned, RequiredPermissions, SchemaVersion, WebRoute
from .connection_catalog import CONTROLLER_CONNECTIONS

# Declared next to the domains that emit them, so a gateway can import the
# record without importing this reader. Re-exported here as the one name
# callers already use.
from .connection_contracts import (
    GRANT_MODELS,
    ConnectionAbility,
    ConnectionFact,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
)
from .connection_lifecycle import Lifecycle, probed, standing
from .connection_setup import guide_url
from .contracts import EFFECT_MEANINGS
from .describe import Describe, listed
from .entity_links import entity_link
from .integration_validation import required_capability_names
from .integrations import integration_graph
from .moments import ago
from .security import Capability, Principal
from .ui import ended
lazy from . import machines
lazy from .infrastructure import enabled_resources
lazy from .locate import index_of
lazy from .projection import read_once


@dataclass(frozen=True, slots=True)
class ConnectionReading:
    """One connection, with what HQ would use it for."""

    connection_ref: str
    controller_id: str
    provider: str
    endpoint: str
    # What the credential says it can act on: the only source of which machines
    # a Portainer holds or which zones a token may edit. Every "which machine"
    # or "which domain" menu derives from it, so adding a VPS is registering it
    # with Portainer, never an edit here.
    reaches: tuple[str, ...]
    reachable: bool
    probed: bool
    detail: str
    # When a controller last reported it. See ``probed_at``.
    observed_at: datetime
    # When it was last probed, where that trails the report: an SSH connection
    # is probed on its own clock and carried between probes.
    probed_at: datetime | None = None
    # The machines this reaches, as (name, url): what a credential opens.
    machines: tuple[tuple[str, str], ...] = ()
    # Declarations that name this connection, as (key, url). The reverse of the
    # ref every spec already carries.
    resources: tuple[tuple[str, str], ...] = ()
    # Those declarations that are one named thing, as (name, key, home url): a
    # domain by its zone, a device by its name.
    named: tuple[tuple[str, str, str], ...] = ()
    # Work that went through this and could not finish, as the last pass found
    # it. A connection can answer every probe and refuse every operation, and
    # `reachable` only ever describes the probe.
    failing_steps: tuple[tuple[str, str], ...] = ()
    # When its controller's report first came with no connections at all. The
    # row is kept so the page still shows what was there.
    unreported_at: datetime | None = None

    @property
    def status(self) -> Lifecycle:
        return standing(
            probed(reachable=self.reachable, was_probed=self.probed),
            unreported=self.unreported_at is not None,
        )

    @property
    def status_label(self) -> str:
        """The status in words. The age shown beside it is when the controller
        reported, which is not when anything was probed."""

        return self.status.label


def _machines_reached(row, known, located) -> tuple[tuple[str, str], ...]:
    """The machines one connection opens, each linked to its page.

    Tried in order of how directly the credential says so: the machines it
    reports reaching, its own name when that is a machine, and last the address
    it points at. None when nothing lands, so the endpoint column says only
    what HQ knows.
    """

    by_url = {known[name.lower()].url: name for name in row.reaches if name.lower() in known}
    # And every machine the catalog says this connection reaches, such as the
    # devices a tailnet connection read.
    for item in known.values():
        if row.connection_ref in item.reached_by:
            by_url.setdefault(item.url, item.name)
    if by_url:
        return tuple((name, url) for url, name in by_url.items())
    if row.connection_ref.lower() in known:
        found = known[row.connection_ref.lower()]
        return ((found.name, found.url),)
    name = located.at(row.endpoint)
    if name and name.lower() in known:
        return ((name, known[name.lower()].url),)
    return ()


def _one_name(resource) -> str:
    """The one name a declaration stands for, or "" when it is not one thing.

    Read from the provider: its hostnames, else an identity of a single value.
    """

    provider = PROVIDERS.get(resource.kind)
    if provider is None:
        return ""
    spec = resource.spec or {}
    try:
        if provider.hostnames is not None:
            names = tuple(provider.hostnames(spec))
        elif provider.identity is not None:
            names = tuple(provider.identity(spec))
        else:
            return ""
    except KeyError, TypeError, ValueError:
        return ""
    return normalized_hostname(str(names[0])) if len(names) == 1 else ""


def _depends(reading: ConnectionReading) -> tuple[tuple[ConnectionLink, ...], tuple[ConnectionLink, ...]]:
    """Targets and dependencies, with a declaration that is a target shown once.

    A declaration naming the same thing a target names folds into the target:
    the name stays, linked to the target's page or else the declaration's home,
    and carries the declaration's key.
    """

    targets = (
        tuple(ConnectionLink(name, url) for name, url in reading.machines)
        if reading.machines
        else tuple(ConnectionLink(name) for name in reading.reaches)
    )
    by_name = {name: (key, url) for name, key, url in reading.named}
    folded: set[str] = set()
    merged = []
    for link in targets:
        match = by_name.get(normalized_hostname(link.label))
        if match is None or match[0] in folded:
            merged.append(link)
            continue
        key, home = match
        folded.add(key)
        merged.append(ConnectionLink(link.label, link.url or home, resource_key=key))
    dependencies = tuple(
        ConnectionLink(key, url, resource_key=key) for key, url in reading.resources if key not in folded
    )
    return tuple(merged), dependencies


def machines_once() -> tuple:
    """The machine catalog, read once per projection and shared."""

    return read_once("machines.catalog", machines.machine_catalog)


def machines_by_name(catalog: tuple[machines.Machine, ...]) -> dict[str, machines.Machine]:
    """Every name the board has for a machine, lowercased: its own and the
    ones it folded in, so a credential named after an alias still links to
    the machine."""

    known = {item.name.lower(): item for item in catalog}
    # A kept name is never displaced by somebody else's alias: the board chose
    # the kept name deliberately, and an alias that happens to collide with one
    # would otherwise send that machine's row to a different page.
    for item in catalog:
        for alias in item.aliases:
            known.setdefault(alias.lower(), item)
    return known


def connection_rows() -> tuple:
    """Every reported connection row, read once per projection and shared."""

    return read_once("connections.rows", lambda: tuple(ProviderConnection.objects.all()))


def connection_names() -> Mapping[str, str]:
    """What a person calls each reported connection, by its ref.

    The ref is the credential's own name, so a connection is called by the
    service it reaches; the ref is added only where two share a service, the
    one place it tells them apart.
    """

    def build() -> dict[str, str]:
        refs: dict[str, set[str]] = {}
        for row in connection_rows():
            if row.provider in CONNECTION_LABELS:
                refs.setdefault(row.provider, set()).add(row.connection_ref)
        return {
            ref: CONNECTION_LABELS[provider] if len(shared) == 1 else f"{CONNECTION_LABELS[provider]} ({ref})"
            for provider, shared in refs.items()
            for ref in shared
        }

    return read_once("connections.names", build)


def connection_readings() -> tuple[ConnectionReading, ...]:
    """Every connection every controller last reported, and what ties to it."""

    catalog = machines_once()
    known = machines_by_name(catalog)
    # And by address, through the same resolver every other surface uses, so a
    # credential pointing at a machine HQ knows names it rather than printing a
    # bare endpoint. The catalog's own addresses are the evidence: a machine
    # is whatever the board decided it was, joined on a fact rather than on the
    # label a template happens to render.
    located = index_of(declared=[{"name": item.name, "addresses": item.addresses} for item in catalog])

    using: dict[str, list[tuple[str, str]]] = {}
    named: dict[str, list[tuple[str, str, str]]] = {}
    for resource in enabled_resources():
        ref = str(resource.spec.get("connection_ref", "")).strip()
        if ref:
            using.setdefault(ref, []).append(
                (
                    resource.key,
                    entity_link("resource", resource.key).url,
                )
            )
            name = _one_name(resource)
            if name:
                named.setdefault(ref, []).append((name, resource.key, resource_home(resource)))
    return tuple(
        ConnectionReading(
            connection_ref=row.connection_ref,
            controller_id=row.controller_id,
            provider=row.provider,
            endpoint=row.endpoint,
            reaches=tuple(row.reaches),
            reachable=row.reachable,
            probed=row.probed,
            detail=row.detail,
            observed_at=row.reported_at or row.observed_at,
            probed_at=(row.observed_at if row.reported_at and row.reported_at > row.observed_at else None),
            machines=_machines_reached(row, known, located),
            resources=tuple(sorted(using.get(row.connection_ref, ()))),
            named=tuple(sorted(named.get(row.connection_ref, ()))),
            failing_steps=_failing_steps(row),
            unreported_at=row.unreported_at,
        )
        for row in connection_rows()
    )


def _failing_steps(row) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(item.get("step", "")), str(item.get("reason", "")))
        for item in (row.failing_steps or ())
        if isinstance(item, dict) and item.get("step")
    )


def unfinished_work() -> dict[tuple[str, str], tuple[str, ...]]:
    """``(controller id, connection ref)`` to each step the last pass could not
    finish through that connection, as "step (reason)"."""

    found = {}
    for row in connection_rows():
        steps = _failing_steps(row)
        if steps:
            found[(row.controller_id, row.connection_ref)] = tuple(f"{step} ({reason})" for step, reason in steps)
    return found


def _controller_contract() -> tuple[tuple[ConnectionAbility, ...], dict[str, tuple[str, ...]]]:
    """Derive abilities and their connection kinds in one provider scan."""

    abilities = []
    by_provider: dict[str, list[str]] = {}
    for kind, spec in sorted(PROVIDERS.items()):
        if not spec.connection_providers:
            continue
        abilities.append(
            ConnectionAbility(
                name=kind,
                label=registry_label(kind),
                summary=spec.summary,
                effect="destructive" if spec.destructive else "infrastructure_change",
                governs_kinds=(kind,),
                subject_resource="infrastructure",
            )
        )
        for provider in spec.connection_providers:
            by_provider.setdefault(provider, []).append(kind)

    # Readers, which derive from no kind. Appended rather than merged into the
    # loop above because they are a different claim: a kind says what a
    # credential may change, an observer says what it may look at.
    for observer in observer_abilities():
        abilities.append(
            ConnectionAbility(
                name=observer.name,
                label=observer.label,
                summary=observer.summary,
                effect="read",
                subject_resource=observer.subject_resource,
            )
        )
        by_provider.setdefault(observer.provider, []).append(observer.name)

    # Every reading a provider feeds is something its credential lets HQ see.
    for kind, reading in sorted(OBSERVATIONS.items()):
        abilities.append(
            ConnectionAbility(
                name=kind,
                label=reading.label,
                summary=" ".join(
                    (
                        ended(f"Reads {reading.label}"),
                        *((ended(f"Needs {', '.join(reading.requires)}"),) if reading.requires else ()),
                    )
                ),
                effect="read",
            )
        )
        by_provider.setdefault(reading.provider, []).append(kind)

    return tuple(abilities), {provider: tuple(kinds) for provider, kinds in by_provider.items()}


def _controller_instances(
    ability_names: dict[str, tuple[str, ...]],
) -> tuple[ConnectionInstance, ...]:
    instances = []
    readings = connection_readings()
    name_controller = len({item.controller_id for item in readings}) > 1
    for reading in readings:
        targets, dependencies = _depends(reading)
        instances.append(
            ConnectionInstance(
                id=f"{reading.controller_id}:{reading.connection_ref}",
                label=reading.connection_ref,
                kind=reading.provider or "unclassified",
                status=("serious" if not reading.reachable else "good" if reading.probed else "neutral"),
                status_label=reading.status_label,
                detail=reading.detail,
                endpoint=reading.endpoint,
                observed_at=reading.observed_at,
                ability_names=ability_names.get(reading.provider, ()),
                targets=targets,
                dependencies=dependencies,
                facts=tuple(
                    fact
                    for fact in (
                        ConnectionFact("Controller", reading.controller_id)
                        if name_controller and reading.controller_id
                        else None,
                        ConnectionFact("Tested", ago(reading.probed_at)) if reading.probed_at else None,
                        *(
                            ConnectionFact("Could not finish", f"{step} ({reason})")
                            for step, reason in reading.failing_steps
                        ),
                    )
                    if fact is not None
                ),
                controller_id=reading.controller_id,
                connection_ref=reading.connection_ref,
                # The controller reports reach, not permission. What kind of
                # credential this is comes from the provider's own model,
                # declared once beside the providers.
                credential_model=connection_credential(reading.provider),
            )
        )
    return tuple(instances)


def _controller_connection_spec() -> ConnectionSpec:
    abilities, ability_names = _controller_contract()
    return ConnectionSpec(
        name=CONTROLLER_CONNECTIONS,
        label="Infrastructure",
        summary="What HQ connects to.",
        required_capability=Capability.READ,
        instance_provider=lambda: _controller_instances(ability_names),
        abilities=abilities,
        secret_store="1Password",  # noqa: S106 - the store's name, no credential
        # The one family fed by sweeps rather than by its own configuration,
        # so the one whose emptiness means a report has not arrived.
        empty_message="Each is a vault item the controller is given; it appears here once the controller reports.",
        documentation_url=guide_url(),
    )


def connection_specs() -> tuple[ConnectionSpec, ...]:
    """The controller-observed family emitted by the Connections domain."""

    return (_controller_connection_spec(),)


# The shape describe_connections answers in; it moves when that shape does.
CONNECTIONS_SCHEMA_VERSION = 1


class AbilityDescription(TypedDict):
    """One ability a connection family offers, as ``describe_connections`` names it."""

    name: Annotated[str, Describe("The name of what it lets HQ do, unique within its type of connection.")]
    label: Annotated[str, Describe("What it lets HQ do, in a few words.")]
    summary: Annotated[str, Describe("What it lets HQ do, in a sentence.")]
    effect: Annotated[str, Describe(ended(f"What using it does: {listed(EFFECT_MEANINGS)}"))]
    required_scopes: Annotated[
        list[str], Describe("The permissions the credential must carry for it, in the service's own names.")
    ]
    grant: Annotated[
        str | None,
        Describe(
            f"How HQ proves the credential allows it: {listed(GRANT_MODELS)}, where `scoped` is checked against "
            "`required_scopes`; null where the connection says nothing."
        ),
    ]
    capability: Annotated[str | None, Describe("The command that uses it, by name; null for none.")]
    governs_kinds: Annotated[list[str], Describe("The types of record it lets HQ manage; empty for none.")]
    subject_resource: Annotated[
        str | None,
        Describe("The list those records are read from, by the name `describe_resources` gives it; null for none."),
    ]


class ConnectionDescription(TypedDict):
    """One connection family as every adapter describes it."""

    name: Annotated[str, Describe("The name of this type of connection.")]
    label: Annotated[str, Describe("What a person calls this type of connection.")]
    summary: Annotated[str, Describe("What this type of connection lets HQ reach, in a sentence.")]
    required_capabilities: RequiredPermissions
    web_route: WebRoute
    management_route: Annotated[
        str | None, Describe("The page where its settings are changed, by its route name; null for none.")
    ]
    setup_route: Annotated[str | None, Describe("The page where one is set up, by its route name; null for none.")]
    documentation_url: Annotated[str | None, Describe("The service's own documentation for it; null for none.")]
    secret_store: Annotated[str | None, Describe("Where its credential is kept, such as `1Password`; null for none.")]
    abilities: Annotated[list[AbilityDescription], Describe("What it lets HQ do, one entry for each thing.")]
    permitted: NotRequired[Permitted]


class ConnectionCatalog(TypedDict):
    """What ``describe_connections`` answers."""

    schema_version: Annotated[SchemaVersion, Pinned(CONNECTIONS_SCHEMA_VERSION)]
    connections: Annotated[list[ConnectionDescription], Describe("Every type of connection HQ knows.")]


def _ability(ability: ConnectionAbility) -> AbilityDescription:
    return {
        "name": ability.name,
        "label": ability.label,
        "summary": ability.summary,
        "effect": ability.effect,
        "required_scopes": list(ability.required_scopes),
        "grant": ability.grant or None,
        "capability": ability.capability or None,
        "governs_kinds": list(ability.governs_kinds),
        "subject_resource": ability.subject_resource or None,
    }


def _connection(spec: ConnectionSpec, principal: Principal | None) -> ConnectionDescription:
    described: ConnectionDescription = {
        "name": spec.name,
        "label": spec.label,
        "summary": spec.summary,
        "required_capabilities": list(required_capability_names(spec)),
        "web_route": spec.web_route or None,
        "management_route": spec.management_route or None,
        "setup_route": spec.setup_route or None,
        "documentation_url": spec.documentation_url or None,
        "secret_store": spec.secret_store or None,
        "abilities": [_ability(ability) for ability in spec.abilities],
    }
    if principal is not None:
        described["permitted"] = principal.permits(*spec.required_capabilities)
    return described


def describe_connections(*, principal: Principal | None = None) -> ConnectionCatalog:
    """Every connection family and its abilities; given ``principal``, each
    entry says whether it is ``permitted``: whether the principal's
    capabilities admit the family."""

    return {
        "schema_version": CONNECTIONS_SCHEMA_VERSION,
        "connections": [_connection(spec, principal) for spec in integration_graph().connections.values()],
    }


def connections_for(provider: str) -> tuple[ProviderConnection, ...]:
    """The connections that are one of these, reachable ones first.

    Ordering is the whole contract: a menu built from this offers a working
    credential before a broken one, and never silently omits the broken one,
    an operator whose token expired needs to see the connection they already
    have, marked, rather than an empty list that reads as "you never set it up".
    """

    return tuple(
        sorted(
            ProviderConnection.objects.filter(provider=provider),
            key=lambda row: (not row.reachable, row.connection_ref),
        )
    )


def reachable_through(provider: str) -> tuple[tuple[str, str], ...]:
    """Everything the connections of one kind can act on, as (name, connection).

    A machine behind two Portainers is listed once, under the first that can
    reach it, because the question a form is asking is "where does this run",
    not "by which route".
    """

    seen: dict[str, str] = {}
    for connection in connections_for(provider):
        for name in connection.reaches:
            seen.setdefault(name, connection.connection_ref)
    return tuple(sorted(seen.items()))
