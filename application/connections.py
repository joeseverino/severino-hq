"""What HQ can reach, and what each of those things can act on.

A connection is a credential, and HQ holds none. It holds the *report* of one:
the controller renders the vault into its own environment, asks each endpoint
whether it still answers and what it can see, and sends that back. So this
module reads a cache and never a secret, and the page it feeds is a view of the
vault that cannot drift from it: there is no second list to keep in step.

The point of ``reaches`` is that it is the only place some facts exist at all.
Nothing in HQ can know which machines a Portainer holds or which zones a token
may edit; the credential that would have to carry out the work is the only thing
that can say. Every menu asking "which machine" or "which domain" is derived
from it, which is what makes adding a VPS a matter of registering it with
Portainer rather than of editing anything here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from control_plane.models import ProviderConnection
from control_plane.observations import OBSERVATIONS
from control_plane.providers import (
    PROVIDERS,
    connection_credential,
    observer_abilities,
    registry_label,
)

from .contracts import (
    SCOPE_NAME,
    endpoint_has_private_parts,
)
# Declared next to the domains that emit them, so a gateway can import the
# record without importing this reader. Re-exported here as the one name
# callers already use.
from .connection_contracts import (
    CREDENTIAL_MODELS,
    ConnectionAbility,
    ConnectionFact,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
)
from .entity_links import entity_link
from .integrations import integration_graph
from .integration_validation import required_capability_names, safe_connection_url
from .action_links import (
    ActionLink,
    capability_action_link,
    connection_action_links,
    connection_relationship_link,
    recommend_connection_action,
)
from .security import AuthorizationError, Capability, Principal

# The family the controller observes on HQ's behalf. It leads every inventory
# because it is the one the page is about; the gateways beside it are the
# exceptions that reach out on their own.
CONTROLLER_CONNECTIONS = "infrastructure.controllers"


# A reading's status in words. The age shown beside it is when the controller
# reported, which is not when anything was probed.
_READING_STATUS_LABELS = {
    "unreachable": "Unreachable",
    "reachable": "Reachable",
    "unprobed": "Not probed",
}


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

    @property
    def status(self) -> str:
        if not self.reachable:
            return "unreachable"
        return "reachable" if self.probed else "unprobed"

    @property
    def status_label(self) -> str:
        return _READING_STATUS_LABELS[self.status]


@dataclass(frozen=True)
class ConnectionGroup:
    """A permitted spec beside the instances it produced."""

    spec: ConnectionSpec
    connections: tuple["ConnectionView", ...]


# What proves a connection may perform an ability. Each is a different claim,
# and the page says which one it is making rather than folding them into one
# "available".
EVIDENCE_LABELS = {
    "verified": "Scopes verified",
    "coarse": "Whole-account credential",
    "not_applicable": "Keyless",
    "unverified": "Not verified",
    "undeclared": "Proof undeclared",
    "unknown": "Grants not reported",
    "missing": "Scope missing",
    "revoked": "Credential rejected",
}
# The sentence behind each label, for the place that has room for one.
EVIDENCE_DETAILS = {
    "verified": "The provider reported every grant this ability needs.",
    "coarse": "Full-account credential. The provider offers no narrower scope.",
    "not_applicable": "No credential involved.",
    "unverified": "Scoped credential. The grants this ability needs are not declared.",
    "undeclared": "Neither the ability nor the credential declares how access is proven.",
    "unknown": "The provider has not reported which grants this credential holds.",
    "missing": "A grant this ability needs is missing.",
    "revoked": "The provider rejected this credential.",
}
# Evidence that settles the question: the ability may be performed and HQ can
# say why. The rest either cannot be performed or has not been shown.
PROVEN_EVIDENCE = frozenset({"verified", "coarse", "not_applicable"})

# Where a connection is in its life, from the last observation of it.
LIFECYCLE_LABELS = {
    "configured": "Configured",
    "unreachable": "Unreachable",
    "reachable": "Reachable",
    "ready": "Ready",
    "unauthorized": "Access missing",
    "stale": "Stale",
    "revoked": "Revoked",
}
# The one-word answer to "may HQ do what this connection is held for".
AUTHORITY_LABELS = {
    "proven": "Authority proven",
    "whole_account": "Whole-account credential",
    "undeclared": "Proof undeclared",
    "unknown": "Grants unknown",
    "missing": "Access missing",
    "none": "No abilities",
}


def grant_evidence(
    ability: ConnectionAbility, instance: ConnectionInstance
) -> tuple[str, tuple[str, ...]]:
    """What proves this connection may perform this ability, and what is absent.

    Two declarations meet here: the ability's grant model, which says what proof
    it needs, and the instance's credential model, which says what kind of
    credential was observed. A rejected credential proves nothing for anything.
    A keyless ability or credential has nothing to prove. A whole-account
    credential satisfies any ability and is worth naming as such, because it is
    the opposite of least privilege. Only a scoped requirement is checked scope
    by scope, and only when the provider reported grants. Anything left is a
    requirement nobody declared: said as "unverified" when the provider could
    have been asked, and "undeclared" when nothing is known either way.
    """

    if instance.credential_model == "rejected":
        return "revoked", ()
    if ability.grant == "none" or instance.credential_model == "none":
        return "not_applicable", ()
    if ability.grant == "coarse" or instance.credential_model == "coarse":
        return "coarse", ()
    if ability.required_scopes:
        if not instance.scopes_known:
            return "unknown", ()
        missing = tuple(
            scope
            for scope in ability.required_scopes
            if scope not in instance.granted_scopes
        )
        return ("missing", missing) if missing else ("verified", ())
    if instance.credential_model == "scoped":
        return "unverified", ()
    return "undeclared", ()


def connection_authority(states: tuple["ConnectionAbilityState", ...]) -> str:
    evidence = {state.evidence for state in states}
    if not evidence:
        return "none"
    if evidence & {"missing", "revoked"}:
        return "missing"
    if "unknown" in evidence:
        return "unknown"
    if evidence & {"undeclared", "unverified"}:
        return "undeclared"
    # It works, and it is the opposite of least privilege: said as such rather
    # than folded into "proven".
    if "coarse" in evidence:
        return "whole_account"
    return "proven"


def connection_lifecycle(
    instance: ConnectionInstance,
    authority: str,
    *,
    stale_after_hours: int,
    now: datetime,
) -> str:
    """One state from the observation, its age, reachability and authority.

    Ordered by what matters most: a rejected credential is revoked whatever
    else is true; nothing observed is merely configured; an old observation is
    stale before it is anything else, because every later claim rests on it;
    missing access fails closed ahead of reachability; and only a reached
    connection with proven authority is ready.
    """

    if instance.credential_model == "rejected":
        return "revoked"
    observed = instance.observed_at
    if observed is None:
        return "configured"
    if timezone.is_naive(observed):
        observed = timezone.make_aware(observed)
    if now - observed > timedelta(hours=stale_after_hours):
        return "stale"
    if authority == "missing":
        return "unauthorized"
    if instance.status == "serious":
        return "unreachable"
    if instance.status == "neutral":
        return "configured"
    return "ready" if authority in {"proven", "whole_account"} else "reachable"


@dataclass(frozen=True)
class ConnectionAbilityState:
    ability: ConnectionAbility
    available: bool | None
    missing_scopes: tuple[str, ...] = ()
    action: ActionLink | None = None
    evidence: str = "undeclared"

    @property
    def evidence_label(self) -> str:
        return EVIDENCE_LABELS[self.evidence]

    @property
    def evidence_detail(self) -> str:
        return EVIDENCE_DETAILS[self.evidence]

    @property
    def proven(self) -> bool:
        return self.evidence in PROVEN_EVIDENCE


# Actions derived from the family's spec rather than from one connection.
FAMILY_ACTIONS = frozenset({"open", "manage", "set_up", "documentation"})


@dataclass(frozen=True)
class ConnectionView:
    instance: ConnectionInstance
    abilities: tuple[ConnectionAbilityState, ...]
    actions: tuple[ActionLink, ...] = ()
    lifecycle: str = "configured"
    authority: str = "none"

    @property
    def lifecycle_label(self) -> str:
        return LIFECYCLE_LABELS[self.lifecycle]

    @property
    def authority_label(self) -> str:
        return AUTHORITY_LABELS[self.authority]

    @property
    def mixed_evidence(self) -> bool:
        """Whether the abilities differ in what proves them.

        When they agree, the connection's authority line already says it once,
        and repeating it under every ability said one thing four times.
        """

        return len({state.evidence for state in self.abilities}) > 1

    @property
    def recommended_action(self) -> ActionLink | None:
        return next((action for action in self.actions if action.recommended), None)

    @property
    def other_actions(self) -> tuple[ActionLink, ...]:
        """Useful row actions, excluding this page and the promoted next move."""

        return tuple(
            action
            for action in self.actions
            if action.name != "open" and not action.recommended
        )

    @property
    def row_actions(self) -> tuple[ActionLink, ...]:
        """What is true of this connection rather than of its whole family.

        Documentation, Manage and Set up belong to the family, once. What stays
        on the row is the command this connection can run and its topology node.
        """

        return tuple(
            action for action in self.other_actions if action.name not in FAMILY_ACTIONS
        )


def _machines_reached(row, known, located) -> tuple[tuple[str, str], ...]:
    """The machines one connection opens, named wherever HQ honestly can.

    Three ways a credential names a machine, tried in order of how directly it
    says so: the machines it reports reaching, its own name when that turns out
    to be a machine, and last the address it points at.

    The last is the one this page was missing. A credential that opens a shell
    on a machine HQ was told about printed the address and nothing else, on the
    one page whose subject is what HQ can reach, while the machine's own page
    sat one click away under a name.

    Silence when none of the three lands, which leaves the endpoint column
    saying exactly what HQ knows.
    """

    by_url = {
        known[name.lower()].url: name for name in row.reaches if name.lower() in known
    }
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

    from control_plane.providers import normalized_hostname

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
    except (KeyError, TypeError, ValueError):
        return ""
    return normalized_hostname(str(names[0])) if len(names) == 1 else ""


def _depends(reading: ConnectionReading) -> tuple[
    tuple[ConnectionLink, ...], tuple[ConnectionLink, ...]
]:
    """Targets and dependencies, with a declaration that is a target shown once.

    A declaration naming the same thing a target names folds into the target:
    the name stays, linked to the target's page or else the declaration's home,
    and carries the declaration's key.
    """

    from control_plane.providers import normalized_hostname

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
        ConnectionLink(key, url, resource_key=key)
        for key, url in reading.resources
        if key not in folded
    )
    return tuple(merged), dependencies


def machines_once() -> tuple:
    """The machine catalogue, read once per projection and shared."""

    from .machines import machine_catalog
    from .projection import read_once

    return read_once("machines.catalog", machine_catalog)


def connection_rows() -> tuple:
    """Every reported connection row, read once per projection and shared."""

    from .projection import read_once

    return read_once(
        "connections.rows", lambda: tuple(ProviderConnection.objects.all())
    )


def connection_readings() -> tuple[ConnectionReading, ...]:
    """Every connection every controller last reported, and what ties to it."""


    from .infrastructure import enabled_resources
    from .locate import index_of

    catalog = machines_once()
    # Every name the board has for a machine, its own and the ones it folded
    # in, so a credential named after an alias still links to the machine.
    known = {item.name.lower(): item for item in catalog}
    # A kept name is never displaced by somebody else's alias: the board chose
    # the kept name deliberately, and an alias that happens to collide with one
    # would otherwise send that machine's row to a different page.
    for item in catalog:
        for alias in item.aliases:
            known.setdefault(alias.lower(), item)
    # And by address, through the same resolver every other surface uses, so a
    # credential pointing at a machine HQ knows names it rather than printing a
    # bare endpoint. The catalogue's own addresses are the evidence: a machine
    # is whatever the board decided it was, joined on a fact rather than on the
    # label a template happens to render.
    located = index_of(
        declared=[{"name": item.name, "addresses": item.addresses} for item in catalog]
    )
    from control_plane.providers import resource_home

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
                named.setdefault(ref, []).append(
                    (name, resource.key, resource_home(resource))
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
            machines=_machines_reached(row, known, located),
            resources=tuple(sorted(using.get(row.connection_ref, ()))),
            named=tuple(sorted(named.get(row.connection_ref, ()))),
            failing_steps=_failing_steps(row),
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
            found[(row.controller_id, row.connection_ref)] = tuple(
                f"{step} ({reason})" for step, reason in steps
            )
    return found


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
                label=registry_label(kind),
                summary=spec.summary,
                effect="destructive" if spec.destructive else "infrastructure_change",
                governs_kinds=(kind,),
                subject_resource="infrastructure.resources",
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
                        f"Reads {reading.label} records.",
                        *((f"Needs {', '.join(reading.requires)}.",) if reading.requires else ()),
                    )
                ),
                effect="read",
            )
        )
        by_provider.setdefault(reading.provider, []).append(kind)

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
        targets, dependencies = _depends(reading)
        instances.append(
            ConnectionInstance(
                id=f"{reading.controller_id}:{reading.connection_ref}",
                label=reading.connection_ref,
                kind=reading.provider or "unclassified",
                status=(
                    "serious"
                    if not reading.reachable
                    else "good"
                    if reading.probed
                    else "neutral"
                ),
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
        label="Infrastructure connections",
        summary="Controller credentials and the systems they reach.",
        required_capability=Capability.READ,
        instance_provider=lambda: _controller_instances(ability_names),
        abilities=abilities,
        secret_store="1Password",
        # The one family fed by sweeps rather than by its own configuration,
        # so the one whose emptiness means a report has not arrived.
        empty_message="No controller has reported yet.",
    )


def connection_specs() -> tuple[ConnectionSpec, ...]:
    """The controller-observed family emitted by the Connections domain."""

    return (_controller_connection_spec(),)


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
    if (
        not instance.id.strip()
        or not instance.label.strip()
        or not instance.kind.strip()
    ):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} emitted an incomplete instance."
        )
    if instance.status not in {"good", "attention", "serious", "neutral"}:
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has invalid status {instance.status!r}."
        )
    if not instance.status_label.strip():
        raise ImproperlyConfigured(f"Connection {instance.id!r} has no status label.")
    if instance.observed_at is not None and not isinstance(
        instance.observed_at, datetime
    ):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has an invalid observation time."
        )
    if instance.endpoint and endpoint_has_private_parts(instance.endpoint):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} endpoint contains private URL parts."
        )
    if not isinstance(instance.controller_id, str) or (
        instance.controller_id
        and instance.controller_id != instance.controller_id.strip()
    ):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has an invalid controller id."
        )
    if len(instance.granted_scopes) != len(set(instance.granted_scopes)) or any(
        not SCOPE_NAME.fullmatch(scope) for scope in instance.granted_scopes
    ):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has invalid granted scopes."
        )
    if instance.credential_model not in ("", *CREDENTIAL_MODELS):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has invalid credential model "
            f"{instance.credential_model!r}."
        )
    if instance.credential_model == "none" and (
        instance.granted_scopes or instance.scopes_known
    ):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} is keyless but reports grants."
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
            or (link.url and not safe_connection_url(link.url))
            or not isinstance(link.resource_key, str)
            or (link.resource_key and link.resource_key != link.resource_key.strip())
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
        raise ImproperlyConfigured(f"Connection {instance.id!r} has an invalid fact.")
    return instance


def connection_catalog(*, principal: Principal) -> tuple[ConnectionGroup, ...]:
    """Every permitted connection family and its locally cached instances."""

    groups = []
    for spec in integration_graph().connections.values():
        if not _permitted(spec, principal):
            continue
        instances = _connection_instances(spec)
        abilities = {ability.name: ability for ability in spec.abilities}
        groups.append(
            ConnectionGroup(
                spec,
                tuple(
                    _connection_view(spec, instance, abilities, principal)
                    for instance in instances
                ),
            )
        )
    # Composition order is domain order, which puts the Connections domain
    # last. The inventory keeps the controller-observed family first and the
    # rest in the order they were composed.
    groups.sort(key=lambda group: group.spec.name != CONTROLLER_CONNECTIONS)
    return tuple(groups)


def _connection_instances(spec: ConnectionSpec) -> tuple[ConnectionInstance, ...]:
    instances = tuple(
        _validate_instance(spec, instance) for instance in spec.instance_provider()
    )
    ids = [instance.id for instance in instances]
    if len(ids) != len(set(ids)):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} emitted duplicate instance ids."
        )
    return instances


def _ability_state(
    ability: ConnectionAbility, instance: ConnectionInstance, principal: Principal
) -> ConnectionAbilityState:
    # At call time: capabilities compose plugin specs, which may declare
    # connections. Same reason action_links defers it.
    from .capabilities import capability_title

    evidence, missing = grant_evidence(ability, instance)
    # Unknown stays undecided; only absent or rejected proof closes the door.
    # Undeclared proof leaves the ability usable under HQ's own authorization,
    # and the evidence says so beside it rather than the page pretending
    # otherwise.
    available = None if evidence == "unknown" else evidence not in ("missing", "revoked")
    return ConnectionAbilityState(
        ability,
        available,
        missing,
        # Named for the command it opens, not for the ability that led here.
        # Several abilities are commonly performed by one command, and a button
        # labelled with the ability lands on a page that says something else.
        capability_action_link(
            ability.capability,
            ability.effect,
            capability_title(ability.capability),
            principal=principal,
        )
        if available
        else None,
        evidence,
    )


def _connection_view(
    spec: ConnectionSpec,
    instance: ConnectionInstance,
    abilities: dict[str, ConnectionAbility],
    principal: Principal,
) -> ConnectionView:
    states = tuple(
        _ability_state(abilities[name], instance, principal)
        for name in instance.ability_names
    )
    actions = connection_action_links(spec)
    relationship = connection_relationship_link(spec.name, instance.id)
    if relationship is not None:
        actions = (*actions, relationship)
    # One row action per destination. Four vehicle abilities are performed by
    # one refresh command, which offered the same URL four times under four
    # labels.
    seen: set[str] = {action.url for action in actions}
    for state in states:
        if state.action is not None and state.action.url not in seen:
            seen.add(state.action.url)
            actions = (*actions, state.action)
    authority = connection_authority(states)
    lifecycle = connection_lifecycle(
        instance,
        authority,
        stale_after_hours=spec.stale_after_hours,
        now=timezone.now(),
    )
    return ConnectionView(
        instance,
        states,
        recommend_connection_action(
            actions,
            unhealthy=instance.status != "good" or lifecycle in ("stale", "revoked"),
            missing_scope_count=sum(len(state.missing_scopes) for state in states),
            unknown_scope_count=sum(state.available is None for state in states),
        ),
        lifecycle,
        authority,
    )


def describe_connections() -> dict:
    return {
        "ok": True,
        "schema_version": 1,
        "connections": [
            {
                "name": spec.name,
                "label": spec.label,
                "summary": spec.summary,
                "required_capabilities": list(required_capability_names(spec)),
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
                        "grant": ability.grant or None,
                        "capability": ability.capability or None,
                        "governs_kinds": list(ability.governs_kinds),
                        "subject_resource": ability.subject_resource or None,
                    }
                    for ability in spec.abilities
                ],
            }
            for spec in integration_graph().connections.values()
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
                    _serialize_instance(connection) for connection in group.connections
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
        "credential_model": instance.credential_model or None,
        "lifecycle": connection.lifecycle,
        "authority": connection.authority,
        "abilities": [
            {
                "name": state.ability.name,
                "label": state.ability.label,
                "effect": state.ability.effect,
                "required_scopes": list(state.ability.required_scopes),
                "grant": state.ability.grant or None,
                "evidence": state.evidence,
                "available": state.available,
                "missing_scopes": list(state.missing_scopes),
                "capability": state.ability.capability or None,
                "subject_resource": state.ability.subject_resource or None,
                "action": asdict(state.action) if state.action else None,
            }
            for state in connection.abilities
        ],
        "targets": [asdict(link) for link in instance.targets],
        "dependencies": [asdict(link) for link in instance.dependencies],
        "facts": [asdict(fact) for fact in instance.facts],
        "controller_id": instance.controller_id or None,
        "actions": [asdict(action) for action in connection.actions],
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


def consoles() -> tuple[tuple[str, str, str], ...]:
    """Connections that are a thing you can open, as (label, sub, url).

    A console and an API base are both URLs and only one is worth a link. Told
    apart by the shape a credential's endpoint already has: an API is reached at
    a path (a version, a prefix) and a console is reached at the host
    itself. So a proxy's web interface is offered and a DNS API is not, without
    a list here naming either.

    Nothing is hand-authored. A URL written into this repository is one
    deployment's address published to everyone who clones it, and stale for the
    deployment it belonged to.
    """

    from urllib.parse import urlsplit

    from control_plane.providers import CONNECTION_LABELS

    from .labels import human_label

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
                CONNECTION_LABELS.get(connection.provider)
                or human_label(connection.provider)
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
        {
            "label": "Health endpoint",
            "sub": "liveness",
            "href": reverse("health_ready"),
        },
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
    return [item for item in offered if item["href"].lower() in chosen] or offered, True


def link_choices(user=None) -> list[dict[str, object]]:
    """Every outward link, each marked with whether it has been chosen.

    The same list the panel shows, so the chooser cannot offer something the
    panel would not render or miss something it would.
    """

    from .pins import DASHBOARD_LINK, pinned

    chosen = pinned(user, DASHBOARD_LINK)
    offered, _ = outward_links(None)
    return [{**item, "chosen": item["href"].lower() in chosen} for item in offered]


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
