"""Whether a credential's call runs, waits for approval, or is refused.

How the identity provider's grant, the default and the rules combine is
docs/Security.md#what-hq-enforces. For an agent, anything destructive (a
deletion, a stop) waits for a person by default: a caller that may have read
untrusted text is the one whose deletes and stops most deserve a second look.
Operators are never subject to policy.
"""

from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from django.db import transaction
from django.utils import timezone

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.core.audit import record_event
from hq.platform.core.models import AgentIdentity, AuditLog
lazy from hq.domains.control_plane.providers import PROVIDERS

from .approvals import DESTRUCTIVE_EFFECT, READ_EFFECT, is_destructive
from .command_fields import Describe
from .default_hold import held_by_default, may_be_held_by_default, needs_approval
from .doors import interface_label
from .labels import dotted, effect_label, field_label, lower_first
from .security import (
    AGENT_SURFACES,
    NEEDS_A_PERSON,
    Principal,
    agent_subject,
    could_hold,
    is_agent,
    is_interactive,
    person,
    subject_of,
)
lazy from . import capabilities
lazy from .agent_registry import identity_of
lazy from .entity_links import kind_label
lazy from .integrations import integration_graph

AUDIT_LABEL = "Capability policy"

Rule = CapabilityRule.Rule


@dataclass(frozen=True, slots=True)
class Decision:
    rule: str
    source: str
    default: str

    @property
    def overrides_a_hold(self) -> bool:
        return self.rule == Rule.ALLOW and self.default == Rule.APPROVE


def default_rule(spec, command, target, *, principal: Principal) -> str:
    return Rule.APPROVE if held_by_default(spec, command, target, principal=principal) else Rule.ALLOW


def decide(spec, principal: Principal, command, target) -> Decision:
    """At most one query. ``command`` is the validated command, which is what runs."""

    default = default_rule(spec, command, target, principal=principal)
    if is_interactive(principal):
        return Decision(Rule.ALLOW, "operator", default)
    if not is_agent(principal.interface):
        return Decision(default, "default", default)
    if is_destructive(spec, command):
        default = Rule.APPROVE

    # The agent's own rule beats the default outright: it is how one agent is
    # allowed what the rest still wait for, and how one is kept off what its
    # grant carries.
    rule = (
        CapabilityRule.objects.filter(capability=spec.name, subject=agent_subject(principal))
        .values_list("rule", flat=True)
        .first()
    )
    if rule is None:
        return Decision(default, "default", default)
    return Decision(rule, f"{principal.actor} policy", default)


def rules() -> dict[tuple[str, str], str]:
    return {(row.subject, row.capability): row.rule for row in CapabilityRule.objects.all()}


def set_rule(
    *,
    subject: str,
    spec,
    rule: str | None,
    user,
) -> bool:
    """Set an agent's rule, or clear it with None. Returns whether anything changed.

    Refuses approval on a read, and an agent rule for a capability its grant
    does not carry or its sign-in could never use. Who may set one at all is
    ``GOVERN_AGENTS``'s, which ``Principal.require`` holds to a person. The rule
    and its audit row commit together.
    """

    if rule is not None and rule not in Rule.values:
        raise ValueError(f"{rule!r} is not a rule.")
    if not agents_may_hold(spec):
        raise ValueError(f"Only a person in the browser can {spec.title.lower()}, so no agent has a rule for it.")
    if rule == Rule.APPROVE and spec.effect == READ_EFFECT:
        raise ValueError(f"{spec.title} only reads. It can be allowed or blocked, not held for approval.")
    if rule is not None:
        identity = identity_of(subject).first()
        if identity is None:
            raise ValueError(f"No agent named {subject} has connected yet.")
        if not _held_by_grant(spec, identity.granted):
            raise ValueError(f"The sign-in service does not allow {subject} to use {spec.title}.")
        if not _usable_by(spec, identity):
            raise ValueError(f"{subject} signs in as itself, and {spec.title} keeps something of a person's.")

    with transaction.atomic():
        existing = CapabilityRule.objects.select_for_update().filter(subject=subject, capability=spec.name).first()
        before = existing.rule if existing else None
        if before == rule:
            return False
        if rule is None:
            existing.delete()
        elif existing is None:
            CapabilityRule.objects.create(subject=subject, capability=spec.name, rule=rule, changed_by=user)
        else:
            existing.rule = rule
            existing.changed_by = user
            existing.changed_at = timezone.now()
            existing.save(update_fields=["rule", "changed_by", "changed_at"])
        record_event(
            action=AuditLog.Action.SETTINGS_CHANGED,
            type_label=AUDIT_LABEL,
            message=dotted(subject, f"{spec.name}: from {_name(before)} to {_name(rule)}"),
            metadata={
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

    return any(could_hold(surface, spec.required_capabilities) for surface in AGENT_SURFACES)


def _held_by_grant(spec, granted) -> bool:
    """Whether ``granted`` carries every capability ``spec`` requires.

    Asked of a grant alone, since whether its agent has a person behind it is
    ``_usable_by``'s question.
    """

    return all(capability in frozenset(granted) for capability in spec.required_capabilities)


def _usable_by(spec, identity: AgentIdentity) -> bool:
    """Whether ``identity``'s sign-in could ever run ``spec``: one that keeps
    something of a person's (``NEEDS_A_PERSON``) needs a person behind it."""

    return identity.person_id is not None or NEEDS_A_PERSON.isdisjoint(spec.required_capabilities)


# What a rule is called on the page, beside "Default". The stored values keep their names.
RULE_LABELS = {Rule.ALLOW: "On", Rule.APPROVE: "Ask me first", Rule.DENY: "Off"}
# The status tone a rule is drawn in; the default is neutral.
RULE_TONES = {Rule.ALLOW: "good", Rule.APPROVE: "attention", Rule.DENY: "serious"}


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


def field_name(subject: str, capability: str) -> str:
    return _FIELD_SEPARATOR.join(("rule", subject, capability))


def _parse_field(name: str) -> tuple[str, str] | None:
    parts = name.split(_FIELD_SEPARATOR)
    if len(parts) != 3 or parts[0] != "rule":
        return None
    return parts[1], parts[2]


def submitted_rules(post) -> list[dict[str, str]]:
    """The policy form's cells as ``SET_POLICY``'s ``rules``: every field the form names, by what it names."""

    found = []
    for name, value in post.items():
        parsed = _parse_field(name)
        if parsed is not None:
            subject, capability = parsed
            found.append({"subject": subject, "capability": capability, "rule": value})
    return found


@dataclass(frozen=True, slots=True)
class Column:
    """One agent's column."""

    subject: str
    label: str
    detail: str
    identity: AgentIdentity


@dataclass(frozen=True, slots=True)
class PolicyCell:
    field: str
    column: str
    rule: str | None
    options: tuple[tuple[str, str], ...]
    unavailable: str = ""
    # What a reader hears the setting named: its action, group and agent.
    label: str = ""

    @property
    def tone(self) -> str:
        return RULE_TONES.get(self.rule, "neutral")


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    label: str
    effect: str
    effect_label: str
    default: str
    cells: tuple[PolicyCell, ...]

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
    return tuple(
        Column(
            subject_of(identity.client_id, identity.person_key),
            identity.client_id,
            dotted(
                *(interface_label(interface) for interface in identity.interfaces),
                *(() if identity.person is None else (f"for {identity.person.get_username()}",)),
            ),
            identity,
        )
        for identity in AgentIdentity.objects.select_related("person")
    )


def matrix(current: dict[tuple[str, str], str] | None = None) -> tuple[tuple[Column, ...], tuple[Group, ...]]:
    """Every capability against every column, grouped by what it acts on; ``current`` is
    ``rules()`` when the caller has read them already."""

    cols = columns()
    grants = {column.subject: column.identity.granted for column in cols}
    current = rules() if current is None else current
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
                tuple(_cell(spec, column, current, grants, label) for column in cols),
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
    return field_label(subject)


def _default_label(spec) -> str:
    """What happens with no rule set: a destructive call always asks, and a
    change to a type that needs approval asks for that type."""

    if spec.effect == DESTRUCTIVE_EFFECT:
        return RULE_LABELS[Rule.APPROVE]
    if may_be_held_by_default(spec):
        return f"{RULE_LABELS[Rule.APPROVE]} for {_approved_types(spec)}"
    return RULE_LABELS[Rule.ALLOW]


def _approved_types(spec) -> str:
    """The types whose changes through ``spec`` wait for a person, by the names their pages use."""

    names = sorted(
        lower_first(kind_label(kind)) for kind, provider in PROVIDERS.items() if needs_approval(provider, spec)
    )
    if not names:
        return "types that need approval"
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _cell(spec, column: Column, current, grants, group: str) -> PolicyCell:
    field = field_name(column.subject, spec.name)
    if not _held_by_grant(spec, grants[column.subject]):
        return PolicyCell(field, column.label, None, (), "Not granted")
    if not _usable_by(spec, column.identity):
        return PolicyCell(field, column.label, None, (), "Needs a person")
    options = [("", "Default"), (Rule.ALLOW, RULE_LABELS[Rule.ALLOW])]
    if spec.effect != READ_EFFECT:
        options.append((Rule.APPROVE, RULE_LABELS[Rule.APPROVE]))
    options.append((Rule.DENY, RULE_LABELS[Rule.DENY]))
    return PolicyCell(
        field,
        column.label,
        current.get((column.subject, spec.name)),
        tuple(options),
        label=f"{spec.title}, {group}, for {column.label}",
    )


SET_POLICY = "agent.policy.set"


@dataclass(frozen=True, slots=True)
class RuleCell:
    """One cell of the policy: the rule for one capability and one agent; empty for the default."""

    subject: str
    capability: str
    rule: str = ""


@dataclass(frozen=True, slots=True)
class PolicyCommand:
    """The cells a person submitted; those already as they hold change nothing."""

    rules: Annotated[tuple[RuleCell, ...], Describe("The policy cells to set, each one rule.")]


class PolicyApplied(TypedDict):
    """What applying the rules changed, and why any rule was left as it was."""

    ok: Literal[True]
    changed: Annotated[int, Describe("How many rules changed.")]
    problems: Annotated[
        list[str], Describe("Why each rule left as it was could not change, a sentence each; empty when none was.")
    ]


def apply_changes(command: PolicyCommand, *, principal: Principal) -> PolicyApplied:
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
        if current.get((cell.subject, cell.capability)) == wanted:
            continue
        try:
            if set_rule(subject=cell.subject, spec=spec, rule=wanted, user=user):
                changed += 1
        except ValueError as exc:
            problems.append(str(exc))
    return {"ok": True, "changed": changed, "problems": problems}
