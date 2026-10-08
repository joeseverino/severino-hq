"""The connections page's reading of every connection.

Each connection grouped by family, what it may do and why, and the evidence
behind each ability. The API serialises the same view.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .action_links import (
    ActionLink,
    capability_action_link,
    connection_action_links,
    connection_relationship_link,
    recommend_connection_action,
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
from .contracts import SCOPE_NAME, endpoint_has_private_parts
from .derivations import passed, present
from .integration_validation import safe_connection_url
from .integrations import integration_graph
from .moments import ago
from .projection import read_once
from .security import Principal

# The family the controller observes on HQ's behalf. It leads every inventory
# because it is the one the page is about; the gateways beside it are the
# exceptions that reach out on their own.
CONTROLLER_CONNECTIONS = "infrastructure.controllers"


@dataclass(frozen=True, slots=True)
class ConnectionGroup:
    """A permitted spec beside the instances it produced."""

    spec: ConnectionSpec
    connections: tuple[ConnectionView, ...]


# What proves a connection may perform an ability. Each is a different claim,
# and the page says which one it is making rather than folding them into one
# "available".
EVIDENCE_LABELS = {
    "verified": "Permissions checked",
    "coarse": "Full account access",
    "not_applicable": "No key needed",
    "unverified": "Permissions not checked",
    "undeclared": "Permissions not checked",
    "unknown": "Permissions not reported",
    "missing": "Permission missing",
    "revoked": "Credential refused",
}
# The sentence behind each label, for the place that has room for one.
EVIDENCE_DETAILS = {
    "verified": "The service confirmed this credential has every permission this needs.",
    "coarse": "This credential has full account access. The service offers nothing narrower.",
    "not_applicable": "No key is needed for this.",
    "unverified": "HQ has not checked this credential's permissions.",
    "undeclared": "HQ has not checked this credential's permissions.",
    "unknown": "The service has not said which permissions this credential has.",
    "missing": "This credential lacks a permission this needs.",
    "revoked": "The service refused this credential.",
}
# Evidence that settles the question: the ability may be performed and HQ can
# say why. The rest either cannot be performed or has not been shown.
PROVEN_EVIDENCE = frozenset({"verified", "coarse", "not_applicable"})

# Where a connection is in its life, from the last observation of it. The one
# word the State column shows.
LIFECYCLE_LABELS = {
    "configured": "Set up",
    "unreachable": "Not answering",
    "reachable": "Working",
    "ready": "Working",
    "unauthorized": "Permission missing",
    "stale": "Out of date",
    "revoked": "Credential refused",
}
# What HQ can say about the credential's permissions, as the one line under
# the state. Blank where the owner can do nothing about it: a connection type
# that never says how its permissions are checked is HQ's gap, not a state of
# the connection.
AUTHORITY_LABELS = {
    "proven": "Permissions checked",
    "whole_account": "Credential has full account access",
    "undeclared": "",
    "unknown": "Permissions not reported",
    "missing": "Permission missing",
    "none": "HQ reads nothing through it",
}
# The same line for a connection whose every ability needs no key.
KEYLESS_LABEL = EVIDENCE_LABELS["not_applicable"]
# Authority the row shows only when it is expanded: true of the connection,
# and nothing to act on.
QUIET_AUTHORITY = frozenset({"whole_account"})


def grant_evidence(ability: ConnectionAbility, instance: ConnectionInstance) -> tuple[str, tuple[str, ...]]:
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
        missing = tuple(scope for scope in ability.required_scopes if scope not in instance.granted_scopes)
        return ("missing", missing) if missing else ("verified", ())
    if instance.credential_model == "scoped":
        return "unverified", ()
    return "undeclared", ()


def connection_authority(states: tuple[ConnectionAbilityState, ...]) -> str:
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
    if passed(observed + timedelta(hours=stale_after_hours), now=now):
        return "stale"
    if authority == "missing":
        return "unauthorized"
    if instance.status == "serious":
        return "unreachable"
    if instance.status == "neutral":
        return "configured"
    return "ready" if authority in {"proven", "whole_account"} else "reachable"


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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
        if self.abilities and all(state.evidence == "not_applicable" for state in self.abilities):
            return KEYLESS_LABEL
        return AUTHORITY_LABELS[self.authority]

    @property
    def state_line(self) -> str:
        """The one line under the state word, or "" when the word says it all.

        Since when for a state that has an age, the missing permissions for a
        credential that lacks them, and otherwise what HQ knows about the
        credential's permissions.
        """

        observed = self.instance.observed_at
        if self.lifecycle == "revoked":
            return ""
        if self.lifecycle == "stale":
            return f"Last read {ago(observed)}"
        if self.lifecycle == "unreachable":
            return f"Last tried {ago(observed)}" if observed else ""
        if self.lifecycle == "unauthorized":
            missing = tuple(dict.fromkeys(scope for state in self.abilities for scope in state.missing_scopes))
            return f"Needs {', '.join(missing)}" if missing else ""
        label = self.authority_label
        if self.lifecycle == "configured" and (label != KEYLESS_LABEL and self.authority != "none"):
            return "Not tested"
        return label

    @property
    def state_line_quiet(self) -> bool:
        """Whether ``state_line`` is shown only in the expanded row."""

        return self.lifecycle in ("ready", "reachable") and self.authority in QUIET_AUTHORITY

    @property
    def name_link(self):
        """The connection's name, linked to what it reaches and what uses it.

        The link builder's own mention of the connection, pointed at its
        relationships when it has them: on the connections page its own row
        is where the reader already is.
        """

        from dataclasses import replace

        from .entity_links import entity_link

        link = entity_link("connection", self.instance.label)
        relationships = next((action for action in self.actions if action.name == "relationships"), None)
        return replace(link, url=relationships.url) if relationships else link

    @property
    def mixed_evidence(self) -> bool:
        """Whether the abilities differ in what proves them.

        When they agree, the connection's authority line already says it once,
        and repeating it under every ability would say one thing several times.
        """

        return len({state.evidence for state in self.abilities}) > 1

    @property
    def recommended_action(self) -> ActionLink | None:
        return next((action for action in self.actions if action.recommended), None)

    @property
    def other_actions(self) -> tuple[ActionLink, ...]:
        """Useful row actions, excluding this page and the promoted next move."""

        return tuple(action for action in self.actions if action.name != "open" and not action.recommended)

    @property
    def row_actions(self) -> tuple[ActionLink, ...]:
        """What is true of this connection rather than of its whole family.

        Documentation, Manage and Set up belong to the family, once. What stays
        on the row is the command this connection can run and its topology node.
        """

        return tuple(
            action
            for action in self.other_actions
            if action.name not in FAMILY_ACTIONS and action.name != "relationships"
        )


def _validate_instance(spec: ConnectionSpec, instance: ConnectionInstance) -> ConnectionInstance:
    if not isinstance(instance, ConnectionInstance):
        raise ImproperlyConfigured(f"Connection {spec.name!r} emitted a non-ConnectionInstance.")
    if not instance.id.strip() or not instance.label.strip() or not instance.kind.strip():
        raise ImproperlyConfigured(f"Connection {spec.name!r} emitted an incomplete instance.")
    if instance.status not in {"good", "attention", "serious", "neutral"}:
        raise ImproperlyConfigured(f"Connection {instance.id!r} has invalid status {instance.status!r}.")
    if not instance.status_label.strip():
        raise ImproperlyConfigured(f"Connection {instance.id!r} has no status label.")
    if instance.observed_at is not None and not isinstance(instance.observed_at, datetime):
        raise ImproperlyConfigured(f"Connection {instance.id!r} has an invalid observation time.")
    if instance.endpoint and endpoint_has_private_parts(instance.endpoint):
        raise ImproperlyConfigured(f"Connection {instance.id!r} endpoint contains private URL parts.")
    if not isinstance(instance.controller_id, str) or (
        instance.controller_id and instance.controller_id != instance.controller_id.strip()
    ):
        raise ImproperlyConfigured(f"Connection {instance.id!r} has an invalid controller id.")
    if len(instance.granted_scopes) != len(set(instance.granted_scopes)) or any(
        not SCOPE_NAME.fullmatch(scope) for scope in instance.granted_scopes
    ):
        raise ImproperlyConfigured(f"Connection {instance.id!r} has invalid granted scopes.")
    if instance.credential_model not in ("", *CREDENTIAL_MODELS):
        raise ImproperlyConfigured(
            f"Connection {instance.id!r} has invalid credential model {instance.credential_model!r}."
        )
    if instance.credential_model == "none" and (instance.granted_scopes or instance.scopes_known):
        raise ImproperlyConfigured(f"Connection {instance.id!r} is keyless but reports grants.")
    known = {ability.name for ability in spec.abilities}
    unknown = sorted(set(instance.ability_names) - known)
    if unknown:
        raise ImproperlyConfigured(f"Connection {instance.id!r} references unknown abilities: {', '.join(unknown)}.")
    for collection in (instance.targets, instance.dependencies):
        if any(
            not isinstance(link, ConnectionLink)
            or not link.label.strip()
            or (link.url and not safe_connection_url(link.url))
            or not isinstance(link.resource_key, str)
            or (link.resource_key and link.resource_key != link.resource_key.strip())
            for link in collection
        ):
            raise ImproperlyConfigured(f"Connection {instance.id!r} has an invalid relationship.")
    if any(
        not isinstance(fact, ConnectionFact) or not fact.label.strip() or not fact.value.strip()
        for fact in instance.facts
    ):
        raise ImproperlyConfigured(f"Connection {instance.id!r} has an invalid fact.")
    return instance


def connection_catalog(*, principal: Principal) -> tuple[ConnectionGroup, ...]:
    """Every permitted connection family and its locally cached instances."""

    groups = []
    for spec in integration_graph().connections.values():
        if not principal.permits(*spec.required_capabilities):
            continue
        instances = _connection_instances(spec)
        abilities = {ability.name: ability for ability in spec.abilities}
        groups.append(
            ConnectionGroup(
                spec,
                tuple(_connection_view(spec, instance, abilities, principal) for instance in instances),
            )
        )
    # Composition order is domain order, which puts the Connections domain
    # last. The inventory keeps the controller-observed family first and the
    # rest in the order they were composed.
    groups.sort(key=lambda group: group.spec.name != CONTROLLER_CONNECTIONS)
    return tuple(groups)


def _connection_instances(spec: ConnectionSpec) -> tuple[ConnectionInstance, ...]:
    """One family's instances, asked of its provider once per request."""

    def load() -> tuple[ConnectionInstance, ...]:
        instances = tuple(_validate_instance(spec, instance) for instance in spec.instance_provider())
        ids = [instance.id for instance in instances]
        if len(ids) != len(set(ids)):
            raise ImproperlyConfigured(f"Connection {spec.name!r} emitted duplicate instance ids.")
        return instances

    return read_once(f"connections.instances:{spec.name}", load)


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
    states = tuple(_ability_state(abilities[name], instance, principal) for name in instance.ability_names)
    actions = connection_action_links(spec)
    relationship = connection_relationship_link(spec.name, instance.id)
    if relationship is not None:
        actions = (*actions, relationship)
    # One row action per destination: abilities performed by one command
    # share its action.
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
        now=present(),
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


def serialize_connection(connection: ConnectionView) -> dict:
    instance = connection.instance
    return {
        "id": instance.id,
        "label": instance.label,
        "kind": instance.kind,
        "status": instance.status,
        "status_label": instance.status_label,
        "detail": instance.detail,
        "endpoint": instance.endpoint or None,
        "observed_at": (instance.observed_at.isoformat() if instance.observed_at else None),
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
