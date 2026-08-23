"""What HQ can reach, and what each of those things can act on.

A connection is a credential, and HQ holds none. It holds the *report* of one:
the controller renders the vault into its own environment, asks each endpoint
whether it still answers and what it can see, and sends that back. So this
module reads a cache and never a secret, and the page it feeds is a view of the
vault that cannot drift from it -- there is no second list to keep in step.

The point of ``reaches`` is that it is the only place some facts exist at all.
Nothing in HQ can know which machines a Portainer holds or which zones a token
may edit; the credential that would have to carry out the work is the only thing
that can say. Every menu asking "which machine" or "which domain" is derived
from it, which is what makes adding a VPS a matter of registering it with
Portainer rather than of editing anything here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import inspect
from typing import Callable

from django.core.exceptions import ImproperlyConfigured
from control_plane.models import ProviderConnection
from control_plane.providers import PROVIDERS

from .contracts import (
    DJANGO_ROUTE,
    DOTTED_NAME,
    EFFECTS,
    SCOPE_NAME,
    endpoint_has_userinfo,
)
from .security import AuthorizationError, Capability, Principal


@dataclass(frozen=True)
class ConnectionReading:
    """One connection, with what HQ would use it for."""

    connection_ref: str
    controller_id: str
    provider: str
    endpoint: str
    reaches: tuple[str, ...]
    reachable: bool
    probed: bool
    detail: str
    observed_at: datetime
    # The machines this reaches, as (name, url). What a credential opens is the
    # most useful thing about it and the page named them as plain text.
    machines: tuple[tuple[str, str], ...] = ()
    # Declarations that name this connection, as (key, url). The reverse of the
    # ref every spec already carries.
    resources: tuple[tuple[str, str], ...] = ()

    @property
    def status(self) -> str:
        if not self.reachable:
            return "unreachable"
        return "reachable" if self.probed else "unprobed"


@dataclass(frozen=True)
class ConnectionAbility:
    """One thing a connection permits HQ to do, without credential material."""

    name: str
    label: str
    summary: str
    effect: str = "read"
    required_scopes: tuple[str, ...] = ()
    capability: str = ""


@dataclass(frozen=True)
class ConnectionLink:
    """A safe relationship from a connection to something HQ can name."""

    label: str
    url: str = ""


@dataclass(frozen=True)
class ConnectionFact:
    """A small provider-owned fact that is useful in a generic connection row."""

    label: str
    value: str


@dataclass(frozen=True)
class ConnectionInstance:
    """One configured connection as its owning domain last observed it."""

    id: str
    label: str
    kind: str
    status: str
    status_label: str
    detail: str = ""
    endpoint: str = ""
    observed_at: datetime | None = None
    granted_scopes: tuple[str, ...] = ()
    scopes_known: bool = False
    ability_names: tuple[str, ...] = ()
    targets: tuple[ConnectionLink, ...] = ()
    dependencies: tuple[ConnectionLink, ...] = ()
    facts: tuple[ConnectionFact, ...] = ()


@dataclass(frozen=True)
class ConnectionSpec:
    """One declaration of a connection family and its cached instance provider."""

    name: str
    label: str
    summary: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    instance_provider: Callable[[], tuple[ConnectionInstance, ...]]
    abilities: tuple[ConnectionAbility, ...] = ()
    web_route: str = "control_plane:connections"
    management_route: str = ""
    setup_route: str = ""
    documentation_url: str = ""
    secret_store: str = ""

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)


@dataclass(frozen=True)
class ConnectionGroup:
    """A permitted spec beside the instances it produced."""

    spec: ConnectionSpec
    connections: tuple["ConnectionView", ...]


@dataclass(frozen=True)
class ConnectionAbilityState:
    ability: ConnectionAbility
    available: bool | None
    missing_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectionView:
    instance: ConnectionInstance
    abilities: tuple[ConnectionAbilityState, ...]


def connection_readings() -> tuple[ConnectionReading, ...]:
    """Every connection every controller last reported, and what ties to it."""

    from django.urls import reverse

    from control_plane.models import ManagedResource

    from .machines import machine_catalog

    known = {item.name.lower(): item for item in machine_catalog()}
    using: dict[str, list[tuple[str, str]]] = {}
    for resource in ManagedResource.objects.filter(enabled=True):
        ref = str(resource.spec.get("connection_ref", "")).strip()
        if ref:
            using.setdefault(ref, []).append(
                (
                    resource.key,
                    reverse("control_plane:detail", kwargs={"key": resource.key}),
                )
            )
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
            observed_at=row.observed_at,
            machines=tuple(
                (name, known[name.lower()].url)
                for name in row.reaches
                if name.lower() in known
            )
            or (
                ((row.connection_ref, known[row.connection_ref.lower()].url),)
                if row.connection_ref.lower() in known
                else ()
            ),
            resources=tuple(sorted(using.get(row.connection_ref, ()))),
        )
        for row in ProviderConnection.objects.all()
    )


def _controller_contract() -> tuple[
    tuple[ConnectionAbility, ...], dict[str, tuple[str, ...]]
]:
    """Derive abilities and their connection kinds in one provider scan."""

    abilities = []
    by_provider: dict[str, list[str]] = {}
    for kind, spec in sorted(PROVIDERS.items()):
        if not spec.connection_providers:
            continue
        abilities.append(
            ConnectionAbility(
                name=kind,
                label=spec.label or kind,
                summary=spec.summary,
                effect="destructive" if spec.destructive else "infrastructure_change",
            )
        )
        for provider in spec.connection_providers:
            by_provider.setdefault(provider, []).append(kind)
    return tuple(abilities), {
        provider: tuple(kinds) for provider, kinds in by_provider.items()
    }


def _controller_instances(
    ability_names: dict[str, tuple[str, ...]],
) -> tuple[ConnectionInstance, ...]:
    instances = []
    readings = connection_readings()
    name_controller = len({item.controller_id for item in readings}) > 1
    for reading in readings:
        targets = (
            tuple(ConnectionLink(name, url) for name, url in reading.machines)
            if reading.machines
            else tuple(ConnectionLink(name) for name in reading.reaches)
        )
        instances.append(
            ConnectionInstance(
                id=f"{reading.controller_id}:{reading.connection_ref}",
                label=reading.connection_ref,
                kind=reading.provider or "unclassified",
                status=(
                    "serious"
                    if not reading.reachable
                    else "good" if reading.probed else "neutral"
                ),
                status_label=reading.status,
                detail=reading.detail,
                endpoint=reading.endpoint,
                observed_at=reading.observed_at,
                ability_names=ability_names.get(reading.provider, ()),
                targets=targets,
                dependencies=tuple(
                    ConnectionLink(key, url) for key, url in reading.resources
                ),
                facts=tuple(
                    fact
                    for fact in (
                        ConnectionFact("Controller", reading.controller_id)
                        if name_controller and reading.controller_id
                        else None,
                    )
                    if fact is not None
                ),
            )
        )
    return tuple(instances)


def _controller_connection_spec() -> ConnectionSpec:
    abilities, ability_names = _controller_contract()
    return ConnectionSpec(
        name="infrastructure.controllers",
        label="Infrastructure connections",
        summary="Credentials rendered to controllers and the systems they can reach.",
        required_capability=Capability.READ,
        instance_provider=lambda: _controller_instances(ability_names),
        abilities=abilities,
        secret_store="1Password",
    )


def _capability_names(spec: ConnectionSpec) -> tuple[str, ...]:
    return tuple(
        item.value if isinstance(item, Capability) else item
        for item in spec.required_capabilities
    )


def _validate_ability(spec: ConnectionSpec, ability: ConnectionAbility) -> None:
    if not isinstance(ability, ConnectionAbility):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} returned a non-ConnectionAbility."
        )
    if not DOTTED_NAME.fullmatch(ability.name):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} has invalid ability {ability.name!r}."
        )
    if not ability.label.strip() or not ability.summary.strip():
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} needs a label and summary."
        )
    if ability.effect not in EFFECTS:
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} has invalid effect {ability.effect!r}."
        )
    if len(ability.required_scopes) != len(set(ability.required_scopes)) or any(
        not SCOPE_NAME.fullmatch(scope) for scope in ability.required_scopes
    ):
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} has invalid required scopes."
        )
    if ability.capability and not DOTTED_NAME.fullmatch(ability.capability):
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} has invalid capability."
        )


def _validate_connection_spec(spec: ConnectionSpec) -> None:
    if not isinstance(spec, ConnectionSpec):
        raise ImproperlyConfigured(
            "A connection provider returned something other than ConnectionSpec."
        )
    if not DOTTED_NAME.fullmatch(spec.name):
        raise ImproperlyConfigured(f"Invalid connection name {spec.name!r}.")
    if not spec.label.strip() or not spec.summary.strip():
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} needs a label and summary."
        )
    required = _capability_names(spec)
    if not required or len(required) != len(set(required)) or any(
        not DOTTED_NAME.fullmatch(item) for item in required
    ):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} must declare unique valid capabilities."
        )
    try:
        inspect.signature(spec.instance_provider).bind()
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} instance provider must take no arguments."
        ) from exc
    for route in (spec.web_route, spec.management_route, spec.setup_route):
        if route and not DJANGO_ROUTE.fullmatch(route):
            raise ImproperlyConfigured(
                f"Connection {spec.name!r} has invalid route {route!r}."
            )
    if spec.documentation_url and not _safe_link_url(spec.documentation_url):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} has an invalid documentation URL."
        )
    for ability in spec.abilities:
        _validate_ability(spec, ability)
    names = [ability.name for ability in spec.abilities]
    if len(names) != len(set(names)):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} repeats an ability name."
        )


def connection_specs() -> tuple[ConnectionSpec, ...]:
    from .plugins import plugin_connection_specs

    specs = (_controller_connection_spec(), *plugin_connection_specs())
    for spec in specs:
        _validate_connection_spec(spec)
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ImproperlyConfigured(
            "Duplicate connection name across HQ core and plugins."
        )
    return specs


def _permitted(spec: ConnectionSpec, principal: Principal) -> bool:
    try:
        for capability in spec.required_capabilities:
            principal.require(capability)
    except AuthorizationError:
        return False
    return True


def _validate_instance(
    spec: ConnectionSpec, instance: ConnectionInstance
) -> ConnectionInstance:
    if not isinstance(instance, ConnectionInstance):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} emitted a non-ConnectionInstance."
        )
    if not instance.id.strip() or not instance.label.strip() or not instance.kind.strip():
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} emitted an incomplete instance."
        )
    if instance.status not in {"good", "attention", "serious", "neutral"}:
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has invalid status {instance.status!r}."
        )
    if not instance.status_label.strip():
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has no status label."
        )
    if instance.observed_at is not None and not isinstance(
        instance.observed_at, datetime
    ):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has an invalid observation time."
        )
    if instance.endpoint and endpoint_has_userinfo(instance.endpoint):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} endpoint contains credential userinfo."
        )
    if len(instance.granted_scopes) != len(set(instance.granted_scopes)) or any(
        not SCOPE_NAME.fullmatch(scope) for scope in instance.granted_scopes
    ):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has invalid granted scopes."
        )
    known = {ability.name for ability in spec.abilities}
    unknown = sorted(set(instance.ability_names) - known)
    if unknown:
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} references unknown abilities: "
            f"{', '.join(unknown)}."
        )
    for collection in (instance.targets, instance.dependencies):
        if any(
            not isinstance(link, ConnectionLink)
            or not link.label.strip()
            or (link.url and not _safe_link_url(link.url))
            for link in collection
        ):
            raise ImproperlyConfigured(
                f"Connection {instance.id!r} has an invalid relationship."
            )
    if any(
        not isinstance(fact, ConnectionFact)
        or not fact.label.strip()
        or not fact.value.strip()
        for fact in instance.facts
    ):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has an invalid fact."
        )
    return instance


def _safe_link_url(url: str) -> bool:
    """Allow explicit web URLs and local paths, never executable schemes."""

    return url.startswith(("http://", "https://")) or (
        url.startswith("/") and not url.startswith("//")
    )


def connection_catalog(*, principal: Principal) -> tuple[ConnectionGroup, ...]:
    """Every permitted connection family and its locally cached instances."""

    groups = []
    for spec in connection_specs():
        if not _permitted(spec, principal):
            continue
        instances = tuple(
            _validate_instance(spec, instance) for instance in spec.instance_provider()
        )
        ids = [instance.id for instance in instances]
        if len(ids) != len(set(ids)):
            raise ImproperlyConfigured(
                f"Connection {spec.name!r} emitted duplicate instance ids."
            )
        abilities = {ability.name: ability for ability in spec.abilities}
        groups.append(
            ConnectionGroup(
                spec,
                tuple(
                    ConnectionView(
                        instance,
                        tuple(
                            _ability_state(abilities[name], instance)
                            for name in instance.ability_names
                        ),
                    )
                    for instance in instances
                ),
            )
        )
    return tuple(groups)


def _ability_state(
    ability: ConnectionAbility, instance: ConnectionInstance
) -> ConnectionAbilityState:
    if not instance.scopes_known:
        return ConnectionAbilityState(ability, None)
    missing = tuple(
        scope
        for scope in ability.required_scopes
        if scope not in instance.granted_scopes
    )
    return ConnectionAbilityState(ability, not missing, missing)


def describe_connections() -> dict:
    return {
        "ok": True,
        "schema_version": 1,
        "connections": [
            {
                "name": spec.name,
                "label": spec.label,
                "summary": spec.summary,
                "required_capabilities": list(_capability_names(spec)),
                "web_route": spec.web_route or None,
                "management_route": spec.management_route or None,
                "setup_route": spec.setup_route or None,
                "documentation_url": spec.documentation_url or None,
                "secret_store": spec.secret_store or None,
                "abilities": [
                    {
                        "name": ability.name,
                        "label": ability.label,
                        "summary": ability.summary,
                        "effect": ability.effect,
                        "required_scopes": list(ability.required_scopes),
                        "capability": ability.capability or None,
                    }
                    for ability in spec.abilities
                ],
            }
            for spec in connection_specs()
        ],
    }


def list_connections(*, principal: Principal) -> dict:
    """Serialize safe connection state for machine adapters; never credentials."""

    groups = connection_catalog(principal=principal)
    return {
        "ok": True,
        "schema_version": 1,
        "groups": [
            {
                "name": group.spec.name,
                "label": group.spec.label,
                "summary": group.spec.summary,
                "secret_store": group.spec.secret_store or None,
                "instances": [
                    _serialize_instance(connection)
                    for connection in group.connections
                ],
            }
            for group in groups
        ],
    }


def _serialize_instance(connection: ConnectionView) -> dict:
    instance = connection.instance
    return {
        "id": instance.id,
        "label": instance.label,
        "kind": instance.kind,
        "status": instance.status,
        "status_label": instance.status_label,
        "detail": instance.detail,
        "endpoint": instance.endpoint or None,
        "observed_at": (
            instance.observed_at.isoformat() if instance.observed_at else None
        ),
        "scopes_known": instance.scopes_known,
        "granted_scopes": list(instance.granted_scopes),
        "abilities": [
            {
                "name": state.ability.name,
                "label": state.ability.label,
                "effect": state.ability.effect,
                "required_scopes": list(state.ability.required_scopes),
                "available": state.available,
                "missing_scopes": list(state.missing_scopes),
                "capability": state.ability.capability or None,
            }
            for state in connection.abilities
        ],
        "targets": [asdict(link) for link in instance.targets],
        "dependencies": [asdict(link) for link in instance.dependencies],
        "facts": [asdict(fact) for fact in instance.facts],
    }


def connections_for(provider: str) -> tuple[ProviderConnection, ...]:
    """The connections that are one of these, reachable ones first.

    Ordering is the whole contract: a menu built from this offers a working
    credential before a broken one, and never silently omits the broken one --
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


def consoles() -> tuple[tuple[str, str, str], ...]:
    """Connections that are a thing you can open, as (label, sub, url).

    A console and an API base are both URLs and only one is worth a link. Told
    apart by the shape a credential's endpoint already has: an API is reached at
    a path -- a version, a prefix -- and a console is reached at the host
    itself. So a proxy's web interface is offered and a DNS API is not, without
    a list here naming either.

    Nothing is hand-authored. A URL written into this repository is one
    deployment's address published to everyone who clones it, and stale for the
    deployment it belonged to.
    """

    from urllib.parse import urlsplit

    found = []
    for connection in ProviderConnection.objects.all():
        endpoint = connection.endpoint.strip()
        if not endpoint or "://" not in endpoint:
            continue
        parsed = urlsplit(endpoint)
        if parsed.path.strip("/"):
            continue
        found.append(
            (
                connection.provider.replace("_", " ").title()
                or connection.connection_ref,
                connection.connection_ref,
                endpoint,
            )
        )
    return tuple(sorted(found))


def outward_links(user=None) -> tuple[list[dict[str, str]], bool]:
    """Everything HQ can open, and whether the operator has chosen a subset.

    Chosen rather than configured: which of these is worth a shortcut is a
    preference, and a preference belongs with the operator rather than in the
    deployment's environment. Nothing chosen means everything, because a panel
    that starts empty teaches nobody that it can be filled.
    """

    from .pins import DASHBOARD_LINK, pinned

    from django.urls import reverse

    from .services import public_sites

    offered = [
        {"label": "Health endpoint", "sub": "liveness", "href": reverse("health_ready")},
        *(
            {"label": label, "sub": sub or "console", "href": href}
            for label, sub, href in consoles()
        ),
        *(
            {"label": hostname, "sub": sub or "published", "href": href}
            for hostname, sub, href in public_sites()
        ),
        *operator_links(),
    ]
    chosen = pinned(user, DASHBOARD_LINK)
    if not chosen:
        return offered, False
    return [
        item for item in offered if item["href"].lower() in chosen
    ] or offered, True


def link_choices(user=None) -> list[dict[str, object]]:
    """Every outward link, each marked with whether it has been chosen.

    The same list the panel shows, so the chooser cannot offer something the
    panel would not render or miss something it would.
    """

    from .pins import DASHBOARD_LINK, pinned

    chosen = pinned(user, DASHBOARD_LINK)
    offered, _ = outward_links(None)
    return [
        {**item, "chosen": item["href"].lower() in chosen} for item in offered
    ]


def operator_links() -> list[dict[str, str]]:
    """Extra dashboard links this deployment wants, from its own environment.

    A status page or a public site is a fact about one installation and belongs
    with its other deployment facts. Malformed input is ignored rather than
    fatal: a dashboard is where an operator goes to fix things, and refusing to
    render it over a bad link is the least useful moment to fail.
    """

    import json

    from django.conf import settings

    raw = str(getattr(settings, "SEVERINO_DASHBOARD_LINKS", "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [
        {
            "label": str(item.get("label", ""))[:80],
            "sub": str(item.get("sub", ""))[:80],
            "href": str(item.get("href", ""))[:500],
        }
        for item in parsed
        if isinstance(item, dict) and str(item.get("href", "")).startswith("http")
    ]
