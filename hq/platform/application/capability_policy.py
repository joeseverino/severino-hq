"""Whether a credential's call runs, waits for approval, or is refused.

How the identity provider's grant, the default and the rules combine is
docs/Security.md#what-hq-enforces. For an agent, anything destructive (a
deletion, a stop) waits for a person by default: a caller that may have read
untrusted text is the one whose deletes and stops most deserve a second look.
Operators are never subject to policy.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.core.audit import record_event
from hq.platform.core.models import AgentIdentity, AuditLog
lazy from hq.domains.control_plane.providers import PROVIDERS

from .approvals import (
    AGENT_SURFACES,
    DESTRUCTIVE_EFFECT,
    READ_EFFECT,
    held_by_default,
    is_destructive,
    may_be_held_by_default,
)
from .command_fields import Describe
from .labels import effect_label
from .security import (
    AuthorizationError,
    Principal,
    could_hold,
    is_interactive,
    mcp_principal,
    person,
)
lazy from . import capabilities
lazy from .entity_links import kind_label
lazy from .integrations import integration_graph

SURFACES = AGENT_SURFACES
AUDIT_LABEL = "Capability policy"

Rule = CapabilityRule.Rule
Scope = CapabilityRule.Scope

_RESTRICTIVENESS = {Rule.ALLOW: 0, Rule.APPROVE: 1, Rule.DENY: 2}


@dataclass(frozen=True, slots=True)
class Decision:
    rule: str
    source: str
    default: str

    @property
    def overrides_a_hold(self) -> bool:
        return self.rule == Rule.ALLOW and self.default == Rule.APPROVE


def default_rule(spec, command, target) -> str:
    return Rule.APPROVE if held_by_default(spec, command, target) else Rule.ALLOW


def decide(spec, principal: Principal, command, target) -> Decision:
    """At most one query. ``command`` is the validated command, which is what runs."""

    default = default_rule(spec, command, target)
    if is_interactive(principal):
        return Decision(Rule.ALLOW, "operator", default)
    if principal.interface not in SURFACES:
        return Decision(default, "default", default)
    if is_destructive(spec, command):
        default = Rule.APPROVE

    rules = dict(
        CapabilityRule.objects.filter(capability=spec.name)
        .filter(Q(scope=Scope.SURFACE, subject=principal.interface) | Q(scope=Scope.AGENT, subject=principal.actor))
        .values_list("scope", "rule")
    )
    decision = (
        Decision(rules[Scope.SURFACE], f"{principal.interface} policy", default)
        if Scope.SURFACE in rules
        else Decision(default, "default", default)
    )
    agent_rule = rules.get(Scope.AGENT)
    if agent_rule and (
        # An explicit rule for this agent beats the default outright (it is
        # how one agent is allowed what the rest still wait for) and against
        # an explicit surface rule, only the stricter of the two survives.
        decision.source == "default" or _RESTRICTIVENESS[agent_rule] > _RESTRICTIVENESS[decision.rule]
    ):
        decision = Decision(agent_rule, f"{principal.actor} policy", default)
    return decision


def rules() -> dict[tuple[str, str, str], str]:
    return {(row.scope, row.subject, row.capability): row.rule for row in CapabilityRule.objects.all()}


def set_rule(
    *,
    scope: str,
    subject: str,
    spec,
    rule: str | None,
    principal: Principal,
    user,
) -> bool:
    """Set a rule, or clear it with None. Returns whether anything changed.

    Refuses non-operators, approval on a read, and an agent rule for a capability
    its grant does not carry. The rule and its audit row commit together.
    """

    if not is_interactive(principal):
        raise AuthorizationError(
            f"{principal.interface} principal {principal.actor!r} cannot change capability policy."
        )
    if scope == Scope.SURFACE and subject not in SURFACES:
        raise ValueError(f"{subject!r} is not a surface.")
    if scope not in Scope.values:
        raise ValueError(f"{scope!r} is not a policy scope.")
    if rule is not None and rule not in Rule.values:
        raise ValueError(f"{rule!r} is not a rule.")
    if not agents_may_hold(spec):
        raise ValueError(f"Only a person in the browser can {spec.title.lower()}, so no agent has a rule for it.")
    if rule == Rule.APPROVE and spec.effect == READ_EFFECT:
        raise ValueError(f"{spec.title} only reads. It can be allowed or blocked, not held for approval.")
    if scope == Scope.AGENT and rule is not None:
        granted = AgentIdentity.objects.filter(client_id=subject).values_list("granted", flat=True).first()
        if granted is None:
            raise ValueError(f"No agent named {subject} has connected yet.")
        if not _held_by_grant(spec, granted):
            raise ValueError(f"Pocket ID does not allow {subject} to use {spec.title}.")

    with transaction.atomic():
        existing = (
            CapabilityRule.objects.select_for_update()
            .filter(scope=scope, subject=subject, capability=spec.name)
            .first()
        )
        before = existing.rule if existing else None
        if before == rule:
            return False
        if rule is None:
            existing.delete()
        elif existing is None:
            CapabilityRule.objects.create(
                scope=scope, subject=subject, capability=spec.name, rule=rule, changed_by=user
            )
        else:
            existing.rule = rule
            existing.changed_by = user
            existing.changed_at = timezone.now()
            existing.save(update_fields=["rule", "changed_by", "changed_at"])
        record_event(
            action=AuditLog.Action.SETTINGS_CHANGED,
            type_label=AUDIT_LABEL,
            message=f"{subject} · {spec.name}: from {_name(before)} to {_name(rule)}",
            metadata={
                "scope": scope,
                "subject": subject,
                "capability": spec.name,
                "from": before,
                "to": rule,
            },
            user=user,
            required=True,
        )
    return True


def agents_may_hold(spec) -> bool:
    """Whether a token on some agent surface could ever be granted ``spec``."""

    return any(could_hold(surface, spec.required_capabilities) for surface in SURFACES)


def _held_by_grant(spec, granted) -> bool:
    """Asked through Principal.permits, the one definition of holding a capability."""

    return Principal("grant", "internal", frozenset(granted)).permits(*spec.required_capabilities)


# What a rule is called on the page. The stored values keep their names.
RULE_LABELS = {Rule.ALLOW: "Allow", Rule.APPROVE: "Ask me first", Rule.DENY: "Block"}


def _name(rule: str | None) -> str:
    return "Default" if rule is None else RULE_LABELS[Rule(rule)]


# Form field names are produced and parsed only here.
_FIELD_SEPARATOR = "|"

# What each kind of action does, beside its name. A read changes nothing and
# says nothing.
EFFECTS = {
    "read": "",
    "remote_write": effect_label("remote_write"),
    "destructive": effect_label("destructive"),
    "infrastructure_change": effect_label("infrastructure_change"),
}
_EFFECT_ORDER = tuple(EFFECTS)


def field_name(scope: str, subject: str, capability: str) -> str:
    return _FIELD_SEPARATOR.join(("rule", scope, subject, capability))


def _parse_field(name: str) -> tuple[str, str, str] | None:
    parts = name.split(_FIELD_SEPARATOR)
    if len(parts) != 4 or parts[0] != "rule":
        return None
    return parts[1], parts[2], parts[3]


def submitted_rules(post) -> list[dict[str, str]]:
    """The policy form's cells as ``SET_POLICY``'s ``rules``: every field the form names, by what it names."""

    found = []
    for name, value in post.items():
        parsed = _parse_field(name)
        if parsed is not None:
            scope, subject, capability = parsed
            found.append({"scope": scope, "subject": subject, "capability": capability, "rule": value})
    return found


@dataclass(frozen=True, slots=True)
class Column:
    scope: str
    subject: str
    label: str
    detail: str
    identity: AgentIdentity | None = None


@dataclass(frozen=True, slots=True)
class PolicyCell:
    field: str
    scope: str
    column: str
    rule: str | None
    options: tuple[tuple[str, str], ...]
    unavailable: str = ""
    # Settable, but not in force until the deployment allows it over MCP.
    dormant: bool = False

    @property
    def rule_label(self) -> str:
        return _name(self.rule)


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    label: str
    effect: str
    effect_label: str
    default: str
    cells: tuple[PolicyCell, ...]

    @property
    def everyone(self) -> tuple[PolicyCell, ...]:
        return tuple(cell for cell in self.cells if cell.scope == Scope.SURFACE)

    @property
    def agents(self) -> tuple[PolicyCell, ...]:
        return tuple(cell for cell in self.cells if cell.scope == Scope.AGENT)

    @property
    def overrides(self) -> tuple[PolicyCell, ...]:
        return tuple(cell for cell in self.cells if cell.rule)


@dataclass(frozen=True, slots=True)
class Group:
    label: str
    rows: tuple[Row, ...]


def split_by_default(groups: tuple[Group, ...]) -> tuple[tuple[Group, ...], tuple[Group, ...]]:
    """``(changed, at_default)``: the groups with only the rows where some rule
    differs from the default, then with only the rest. A group with no row on
    a side is left out of it, and every row is on exactly one side."""

    def kept(*, changed: bool) -> tuple[Group, ...]:
        rows = (
            Group(group.label, tuple(row for row in group.rows if bool(row.overrides) is changed)) for group in groups
        )
        return tuple(group for group in rows if group.rows)

    return kept(changed=True), kept(changed=False)


def columns() -> tuple[Column, ...]:
    surfaces = (
        Column(Scope.SURFACE, "mcp", "All MCP agents", "MCP"),
        Column(Scope.SURFACE, "api", "All API clients", "API"),
    )
    agents = tuple(
        Column(
            Scope.AGENT,
            identity.client_id,
            identity.client_id,
            " · ".join(identity.interfaces).upper(),
            identity,
        )
        for identity in AgentIdentity.objects.all()
    )
    return surfaces + agents


def matrix() -> tuple[tuple[Column, ...], tuple[Group, ...]]:
    """Every capability against every column, grouped by what it acts on."""

    cols = columns()
    grants = dict(AgentIdentity.objects.values_list("client_id", "granted"))
    ceiling = mcp_principal()
    current = rules()
    grouped: dict[str, list] = {}
    for spec in capabilities.capability_registry().values():
        if spec.effect in EFFECTS and agents_may_hold(spec):
            grouped.setdefault(_subject_label(_subject(spec)), []).append(spec)
    groups = []
    for label in sorted(grouped):
        specs = sorted(grouped[label], key=lambda spec: (_EFFECT_ORDER.index(spec.effect), spec.name))
        rows = tuple(
            Row(
                spec.name,
                # The command's own title, whole: a title cut down to fit its
                # group stops saying what the command does.
                spec.title,
                spec.effect,
                EFFECTS[spec.effect],
                _default_label(spec),
                tuple(_cell(spec, column, current, grants, ceiling) for column in cols),
            )
            for spec in specs
        )
        groups.append(Group(label, rows))
    return cols, tuple(groups)


def _subject(spec) -> str:
    return spec.subject_resource or spec.name.split(".", 1)[0]


def _subject_label(subject: str) -> str:
    """What a group of commands acts on, by the name its own page has."""

    resource = integration_graph().resources.get(subject)
    if resource is not None:
        return resource.label
    words = subject.removesuffix(".resources").replace(".", " ").replace("_", " ")
    return words[:1].upper() + words[1:]


def _default_label(spec) -> str:
    """What happens with no rule set: a destructive call always asks, and a
    change to a type that needs approval asks for that type."""

    if spec.effect == DESTRUCTIVE_EFFECT:
        return RULE_LABELS[Rule.APPROVE]
    if may_be_held_by_default(spec):
        return f"{RULE_LABELS[Rule.APPROVE]} for {_approved_types()}"
    return RULE_LABELS[Rule.ALLOW]


def _approved_types() -> str:
    """The types whose changes wait for a person, by the names their pages use."""

    names = sorted(kind_label(kind).lower() for kind, provider in PROVIDERS.items() if provider.requires_approval)
    if not names:
        return "types that need approval"
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _cell(spec, column: Column, current, grants, ceiling) -> PolicyCell:
    field = field_name(column.scope, column.subject, spec.name)
    if column.scope == Scope.AGENT and not _held_by_grant(spec, grants[column.subject]):
        return PolicyCell(field, column.scope, column.label, None, (), "Pocket ID does not allow this")
    dormant = column.subject == "mcp" and not ceiling.permits(*spec.required_capabilities)
    options = [("", "Default"), (Rule.ALLOW, RULE_LABELS[Rule.ALLOW])]
    if spec.effect != READ_EFFECT:
        options.append((Rule.APPROVE, RULE_LABELS[Rule.APPROVE]))
    options.append((Rule.DENY, RULE_LABELS[Rule.DENY]))
    return PolicyCell(
        field,
        column.scope,
        column.label,
        current.get((column.scope, column.subject, spec.name)),
        tuple(options),
        dormant=dormant,
    )


SET_POLICY = "agents.policy.set"


@dataclass(frozen=True, slots=True)
class RuleCell:
    """One cell of the policy: the rule for one capability, on one surface or agent; empty for the default."""

    scope: str
    subject: str
    capability: str
    rule: str = ""


@dataclass(frozen=True, slots=True)
class PolicyCommand:
    """The cells a person submitted; those already as they hold change nothing."""

    rules: Annotated[tuple[RuleCell, ...], Describe("The policy cells to set, each one rule.")]


def apply_changes(command: PolicyCommand, *, principal: Principal) -> dict[str, Any]:
    """Apply the cells that differ from what holds, each on its own.

    ``changed`` counts what moved; ``problems`` says why each refused cell was
    left as it was, so one refusal does not undo the rest.
    """

    user = person(principal)
    registry = capabilities.capability_registry()
    current = rules()
    changed, problems = 0, []
    for cell in command.rules:
        spec = registry.get(cell.capability)
        if spec is None:
            problems.append(f"{cell.capability} is no longer something HQ can do.")
            continue
        wanted = cell.rule or None
        if current.get((cell.scope, cell.subject, cell.capability)) == wanted:
            continue
        try:
            if set_rule(scope=cell.scope, subject=cell.subject, spec=spec, rule=wanted, principal=principal, user=user):
                changed += 1
        except ValueError as exc:
            problems.append(str(exc))
    return {"ok": True, "changed": changed, "problems": problems}
