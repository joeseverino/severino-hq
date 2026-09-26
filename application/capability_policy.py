"""Whether a credential's call runs, waits for approval, or is refused.

The identity provider's grant is checked first and can only be narrowed. Then
the default, the surface rule and the agent rule: an explicit rule beats the
default, and between explicit rules the stricter wins. Operators are never
subject to policy.

For an agent, anything destructive waits for a person by default: a caller that
may have read untrusted text is the one whose deletes most deserve a second
look. An operator lifts it for one agent, or a whole surface, with an explicit
Allow.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from control_plane.models import CapabilityRule
from core.audit import record_event
from core.models import AgentIdentity, AuditLog

from .approvals import (
    DESTRUCTIVE_EFFECT,
    READ_EFFECT,
    held_by_default,
    may_be_held_by_default,
)
from .security import AuthorizationError, Principal, is_interactive, mcp_principal

SURFACES = ("mcp", "api")
AUDIT_LABEL = "Capability policy"

Rule = CapabilityRule.Rule
Scope = CapabilityRule.Scope

_RESTRICTIVENESS = {Rule.ALLOW: 0, Rule.APPROVE: 1, Rule.DENY: 2}


@dataclass(frozen=True)
class Decision:
    rule: str
    source: str
    default: str

    @property
    def overrides_a_hold(self) -> bool:
        return self.rule == Rule.ALLOW and self.default == Rule.APPROVE


def default_rule(spec, payload, target) -> str:
    return Rule.APPROVE if held_by_default(spec, payload, target) else Rule.ALLOW


def decide(spec, principal: Principal, payload, target) -> Decision:
    """At most one query."""

    default = default_rule(spec, payload, target)
    if is_interactive(principal):
        return Decision(Rule.ALLOW, "operator", default)
    if principal.interface not in SURFACES:
        return Decision(default, "default", default)
    if spec.effect == DESTRUCTIVE_EFFECT:
        default = Rule.APPROVE

    rules = dict(
        CapabilityRule.objects.filter(capability=spec.name)
        .filter(
            Q(scope=Scope.SURFACE, subject=principal.interface)
            | Q(scope=Scope.AGENT, subject=principal.actor)
        )
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
        decision.source == "default"
        or _RESTRICTIVENESS[agent_rule] > _RESTRICTIVENESS[decision.rule]
    ):
        decision = Decision(agent_rule, f"{principal.actor} policy", default)
    return decision


def rules() -> dict[tuple[str, str, str], str]:
    return {
        (row.scope, row.subject, row.capability): row.rule
        for row in CapabilityRule.objects.all()
    }


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
    if rule == Rule.APPROVE and spec.effect == READ_EFFECT:
        raise ValueError(f"{spec.name} is a read; it can be allowed or denied, not held.")
    if scope == Scope.AGENT and rule is not None:
        granted = (
            AgentIdentity.objects.filter(client_id=subject)
            .values_list("granted", flat=True)
            .first()
        )
        if granted is None:
            raise ValueError(f"No agent named {subject!r} has presented a token here.")
        if not _held_by_grant(spec, granted):
            raise ValueError(
                f"{subject}'s grant in Pocket ID does not include {spec.name}. "
                "HQ can only narrow what the identity provider allows."
            )

    with transaction.atomic():
        existing = CapabilityRule.objects.select_for_update().filter(
            scope=scope, subject=subject, capability=spec.name
        ).first()
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
            message=f"{subject} · {spec.name}: {_name(before)} → {_name(rule)}",
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


def _held_by_grant(spec, granted) -> bool:
    """Asked through Principal.permits, the one definition of holding a capability."""

    return Principal("grant", "internal", frozenset(granted)).permits(
        *spec.required_capabilities
    )


def _name(rule: str | None) -> str:
    return "Default" if rule is None else Rule(rule).label


# Form field names are produced and parsed only here.
_FIELD_SEPARATOR = "|"

EFFECTS = {
    "read": "Read",
    "remote_write": "Write",
    "destructive": "Delete",
    "infrastructure_change": "Infra",
}
_EFFECT_ORDER = tuple(EFFECTS)


def field_name(scope: str, subject: str, capability: str) -> str:
    return _FIELD_SEPARATOR.join(("rule", scope, subject, capability))


def _parse_field(name: str) -> tuple[str, str, str] | None:
    parts = name.split(_FIELD_SEPARATOR)
    if len(parts) != 4 or parts[0] != "rule":
        return None
    return parts[1], parts[2], parts[3]


@dataclass(frozen=True)
class Column:
    scope: str
    subject: str
    label: str
    detail: str
    identity: AgentIdentity | None = None


@dataclass(frozen=True)
class Cell:
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


@dataclass(frozen=True)
class Row:
    name: str
    label: str
    effect: str
    effect_label: str
    default: str
    cells: tuple[Cell, ...]

    @property
    def everyone(self) -> tuple[Cell, ...]:
        return tuple(cell for cell in self.cells if cell.scope == Scope.SURFACE)

    @property
    def agents(self) -> tuple[Cell, ...]:
        return tuple(cell for cell in self.cells if cell.scope == Scope.AGENT)

    @property
    def overrides(self) -> tuple[Cell, ...]:
        return tuple(cell for cell in self.cells if cell.rule)


@dataclass(frozen=True)
class Group:
    label: str
    rows: tuple[Row, ...]


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

    from .capabilities import capability_registry

    cols = columns()
    grants = dict(AgentIdentity.objects.values_list("client_id", "granted"))
    ceiling = mcp_principal()
    current = rules()
    grouped: dict[str, list] = {}
    for spec in capability_registry().values():
        if spec.effect in EFFECTS:
            grouped.setdefault(_subject_label(_subject(spec)), []).append(spec)
    groups = []
    for label in sorted(grouped):
        specs = sorted(
            grouped[label], key=lambda spec: (_EFFECT_ORDER.index(spec.effect), spec.name)
        )
        actions = _actions(specs, label)
        rows = tuple(
            Row(
                spec.name,
                action,
                spec.effect,
                EFFECTS[spec.effect],
                _default_label(spec),
                tuple(_cell(spec, column, current, grants, ceiling) for column in cols),
            )
            for spec, action in zip(specs, actions, strict=True)
        )
        groups.append(Group(label, rows))
    return cols, tuple(groups)


def _subject(spec) -> str:
    return spec.subject_resource or spec.name.split(".", 1)[0]


def _subject_label(subject: str) -> str:
    words = subject.removesuffix(".resources").replace(".", " ").replace("_", " ")
    return words[:1].upper() + words[1:]


def _actions(specs, group: str) -> list[str]:
    """Row labels for one group. The prefix is dropped only where that keeps them apart."""

    labels = [spec.title for spec in specs]
    short = [_action(label, group, spec.name.split(".", 1)[0]) for label, spec in zip(labels, specs, strict=True)]
    return [
        _action(label, group, "") if short.count(action) > 1 else action
        for label, action in zip(labels, short, strict=True)
    ]


def _action(label: str, group: str, prefix: str) -> str:
    """The capability's label without the words its group or prefix already says."""

    def stem(word: str) -> str:
        return word.lower().rstrip("s")

    group_stems = {stem(word) for word in group.split()} | ({stem(prefix)} if prefix else set())
    # Wherever they fall: a declared label puts the verb first ("Create
    # project"), a generated one puts it last ("Project Create").
    kept = [word for word in label.split() if stem(word) not in group_stems]
    words = kept or label.split()[-1:]
    words = [word if word.isupper() and len(word) > 1 else word.lower() for word in words]
    phrase = " ".join(words)
    return phrase[:1].upper() + phrase[1:]


def _default_label(spec) -> str:
    if spec.effect == DESTRUCTIVE_EFFECT:
        return "Approval"
    return "Approval if gated" if may_be_held_by_default(spec) else "Allow"


def _cell(spec, column: Column, current, grants, ceiling) -> Cell:
    field = field_name(column.scope, column.subject, spec.name)
    if column.scope == Scope.AGENT and not _held_by_grant(spec, grants[column.subject]):
        return Cell(field, column.scope, column.label, None, (), "Not granted in Pocket ID")
    dormant = column.subject == "mcp" and not ceiling.permits(*spec.required_capabilities)
    options = [("", "Default"), (Rule.ALLOW, Rule.ALLOW.label)]
    if spec.effect != READ_EFFECT:
        options.append((Rule.APPROVE, Rule.APPROVE.label))
    options.append((Rule.DENY, Rule.DENY.label))
    return Cell(
        field,
        column.scope,
        column.label,
        current.get((column.scope, column.subject, spec.name)),
        tuple(options),
        dormant=dormant,
    )


def apply_changes(submitted, *, principal: Principal, user) -> tuple[int, list[str]]:
    """Apply the cells that differ from what holds. Returns (changed, problems)."""

    from .capabilities import capability_registry

    registry = capability_registry()
    current = rules()
    changed, problems = 0, []
    for name, value in submitted.items():
        parsed = _parse_field(name)
        if parsed is None:
            continue
        scope, subject, capability = parsed
        spec = registry.get(capability)
        if spec is None:
            problems.append(f"{capability} is no longer a capability HQ offers.")
            continue
        wanted = value or None
        if current.get((scope, subject, capability)) == wanted:
            continue
        try:
            if set_rule(
                scope=scope, subject=subject, spec=spec, rule=wanted, principal=principal, user=user
            ):
                changed += 1
        except ValueError as exc:
            problems.append(str(exc))
    return changed, problems
