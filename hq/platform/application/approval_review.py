"""What a person is shown of a held request: the comparison it is decided on, and the sentence it is read by.

Apart from ``approvals``, which holds, fingerprints and decides a request, so
the words and the diff move without touching the gate.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from difflib import unified_diff
from typing import Any

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource, OperationRequest
lazy from hq.domains.control_plane.providers import PROVIDERS

from .approvals import COMPARED, DECLARATIONS, DESTRUCTIVE_EFFECT, WARNINGS, shown_by
from .payload_words import STEP, ChangeRow, changed_words, document, field_words, value_words
lazy from . import capabilities
lazy from .command_forms import switch_field
lazy from .command_targets import item_name
lazy from .entity_links import entity_link, kind_label, record_name
lazy from .integration_specs import TARGET, capability_fields
lazy from .labels import human_label, lower_first
lazy from .resource_capabilities import LIFECYCLE_VERBS
lazy from .resource_operations import ACTION_CAPABILITY


@dataclass(frozen=True, slots=True)
class ChangeGroup:
    """One record of the many a comparison holds, such as a playbook of a
    catalog: the fields of it that move, each named within it, and none when
    it stays as it is."""

    label: str
    rows: tuple[ChangeRow, ...]
    # What the record is at a glance, from its ``SUMMARY``: its name and what it does.
    summary: ChangeRow | None = None
    # Whether the change lets the record do more, as a request's warning
    # that names it says; its row is marked for attention, never said again.
    widens: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.rows)

    @property
    def named(self) -> tuple[ChangeRow, ...]:
        """Its fields that move, each named by the record too ("Empty a cache › Access")."""

        return tuple(replace(row, label=f"{self.label}{STEP}{row.label}") for row in self.rows)

    @property
    def whole(self) -> str:
        """ "added" or "removed" when the record comes or goes as a whole, so
        its one row says it and its fields fold; "" otherwise."""

        changes = {row.change for row in self.rows}
        return changes.pop() if len(changes) == 1 and changes <= {"added", "removed"} else ""


@dataclass(frozen=True, slots=True)
class ChangePreview:
    """The comparison a decision is taken on.

    ``rows`` is the semantic reading: a document is parsed and compared value
    by value, so an access policy shows the three grants that moved rather than
    two walls of text that differ somewhere. ``lines`` is the honest fallback for content
    that will not parse: a policy document with comments in it, for instance,
    where a unified diff is the most that can truthfully be said. A comparison
    of many records of one kind (a catalog's playbooks) holds each in
    ``groups``, beside the ``rows`` of what belongs to none.
    """

    label: str
    rows: tuple[ChangeRow, ...]
    lines: tuple[str, ...]
    # Whether the request sets these values on its record, so one that moves
    # is said as the change itself.
    amends: bool = False
    groups: tuple[ChangeGroup, ...] = ()

    @property
    def changes(self) -> tuple[ChangeRow, ...]:
        """Every value that moves, a grouped one named by its record too."""

        return (*self.rows, *(row for group in self.groups for row in group.named))

    @property
    def moves(self) -> int:
        """How many things a reader sees move: each value apart from any
        record, and each record that changes as one."""

        return len(self.rows) + sum(1 for group in self.groups if group.changed)

    @property
    def unchanged(self) -> tuple[str, ...]:
        """The records that stay as they are, named on one line rather than shown."""

        return tuple(group.label for group in self.groups if not group.changed)

    @property
    def empty(self) -> bool:
        return not self.changes and not self.lines

    @property
    def compares(self) -> bool:
        """Whether there is a before to show, or only what would be written."""

        return any(row.change != "added" for row in self.changes)


def _text_fields(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, ...]:
    """Fields that stayed unparsed text on both sides, differ, and run over
    lines. A one-line value is a value, said as a row like any other.
    """

    def text(value: Any) -> bool:
        return isinstance(document(value), str)

    return tuple(
        sorted(
            field
            for field in set(before) | set(after)
            if text(was := before.get(field, ""))
            and text(now := after.get(field, ""))
            and str(was) != str(now)
            and "\n" in f"{was}{now}"
        )
    )


def compare(before: dict[str, Any], after: dict[str, Any], *, label: str, amends: bool = False) -> ChangePreview:
    """The values that differ between two specs, each named and said in words,
    and a line diff where a document will not parse.
    """

    text = _text_fields(before, after)
    structural_before = {key: value for key, value in before.items() if key not in text}
    structural_after = {key: value for key, value in after.items() if key not in text}
    rows = changed_words(structural_before, structural_after)
    lines: list[str] = []
    for field in text:
        lines.extend(
            unified_diff(
                str(before.get(field, "")).splitlines(),
                str(after.get(field, "")).splitlines(),
                fromfile=f"{field} (now)",
                tofile=f"{field} (requested)",
                lineterm="",
                n=2,
            )
        )
    return ChangePreview(label=label, rows=rows, lines=tuple(lines), amends=amends)


def _records(value: Any) -> bool:
    """Whether a field holds many records of one kind, each by its name."""

    return isinstance(value, Mapping) and bool(value) and all(isinstance(each, Mapping) for each in value.values())


# The key a record of a grouped comparison names itself by at a glance: a
# mapping of ``label`` (its name, which its group is headed by) and ``value``
# (what it does, in a sentence), the one row a record that comes or goes shows.
SUMMARY = "_summary"


def _summary(record: Mapping[str, Any], change: str) -> ChangeRow | None:
    said = record.get(SUMMARY)
    if not isinstance(said, Mapping):
        return None
    words = str(said.get("value", ""))
    return ChangeRow(label=str(said.get("label", "")), before=words, after=words, change=change)


def _fields(record: Any) -> dict[str, Any]:
    return {field: value for field, value in dict(record or {}).items() if field != SUMMARY}


def _group(name: Any, was: Any, now: Any) -> ChangeGroup:
    """One record of a grouped comparison: its fields that move, and its
    summary from the side it is on."""

    shown = _summary(now or was or {}, "added" if now is not None else "removed")
    label = shown.label if shown is not None and shown.label else str(name)
    return ChangeGroup(label=label, rows=changed_words(_fields(was), _fields(now)), summary=shown)


def grouped(before: Mapping[str, Any], after: Mapping[str, Any], *, label: str) -> ChangePreview:
    """``compare``, with each record of a field that holds many (a catalog's
    playbooks) a group of its own: its fields that move, none when it stays."""

    fields = [*before, *(key for key in after if key not in before)]
    held = [key for key in fields if _records(before.get(key)) or _records(after.get(key))]
    rest = compare(
        {key: value for key, value in before.items() if key not in held},
        {key: value for key, value in after.items() if key not in held},
        label=label,
    )
    groups = []
    for key in held:
        was, now = dict(before.get(key) or {}), dict(after.get(key) or {})
        groups.extend(_group(name, was.get(name), now.get(name)) for name in sorted(set(was) | set(now)))
    return replace(rest, groups=tuple(groups))


def preview(held: ApprovalRequest) -> ChangePreview:
    """What approving this request would change, as a person needs to read it.

    Two different comparisons, because two different things are being asked.
    Amending a declaration is a change to what HQ intends, so it is read against
    what HQ intends now. Applying one is a change to the world, so it is read
    against what the world was last seen holding, which is the only reading
    that answers "what happens to the network if I click this".
    """

    compared = held.baseline.get(COMPARED)
    if isinstance(compared, Mapping):
        # A handler that measures its own subject says what it compares.
        return grouped(compared["before"], compared["after"], label="Would change")
    spec = capabilities.capability_registry().get(held.capability)
    if spec is not None and (shown := shown_by(spec)) is not None:
        # A handler that measures its own subject may say how it reads.
        return shown(held)
    if spec is not None and spec.subject_resource != DECLARATIONS:
        return _record_preview(spec, held)
    declared = dict(held.baseline.get("spec") or {})
    requested = held.payload.get("spec")
    if isinstance(requested, dict):
        return compare(declared, requested, label="Would change", amends=True)
    if held.capability.endswith(".remove"):
        return compare(declared, {}, label="Would be removed")
    if held.capability in _lifecycle_capabilities():
        # A start, stop or restart leaves the declaration as it is: what is
        # approved is the act its sentence names.
        return ChangePreview(label="", rows=(), lines=())
    live = held.baseline.get("observed")
    if isinstance(live, Mapping) and live:
        return compare(dict(live), declared, label="Live record changes")
    return compare({}, declared, label="Would be applied")


def _lifecycle_capabilities() -> frozenset[str]:
    return frozenset(ACTION_CAPABILITY[OperationRequest.Action(verb)] for verb in LIFECYCLE_VERBS)


# Fields that steer a request rather than being written by it.
_CONTROL_FIELDS = frozenset({"confirm", "idempotency_key", "reason"})


def _record_preview(spec, held: ApprovalRequest) -> ChangePreview:
    """A record's fields, as they are and as they would be. No document diffing:
    a project's description is a value, not a policy to parse.
    """

    fields = {
        key: value
        for key, value in held.payload.items()
        if key not in _CONTROL_FIELDS and value not in ("", None, [], {})
    }
    schemas = capability_fields(spec)
    if spec.effect == DESTRUCTIVE_EFFECT:
        return _field_rows(held.baseline, {}, schemas, label="Would be deleted")
    if not held.target:
        # The sentence says what it wants; these are the values it sends.
        return _field_rows({}, fields, schemas, label="Asked with")
    if fields:
        return _field_rows(
            {key: held.baseline.get(key) for key in fields}, fields, schemas, label="Would change", amends=True
        )
    return ChangePreview(label="", rows=(), lines=())


def _field_rows(
    before: dict[str, Any], after: dict[str, Any], schemas: dict[str, Any], *, label: str, amends: bool = False
) -> ChangePreview:
    """Each field that moves, named and valued through the command's schema."""

    rows = []
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key), after.get(key)
        if (was or "") == (now or ""):
            continue
        schema = schemas.get(key)
        rows.append(
            ChangeRow(
                label=field_words(key, schema),
                before=value_words(was, schema),
                after=value_words(now, schema),
                change="added" if was is None else "removed" if now is None else "changed",
            )
        )
    return ChangePreview(label=label, rows=tuple(rows), lines=(), amends=amends)


# Held requests are decided on their audit entry. State stays on ApprovalRequest.


@dataclass(frozen=True, slots=True)
class Asked:
    """What a held request wants done, as the one sentence a person reads it
    by, cut around its target so a page can link it."""

    actor: str
    before: str
    target: str
    after: str
    # Whether it says the whole change, which is then not said again.
    whole: bool = False

    def __str__(self) -> str:
        return f"{self.actor} {self.before}{self.target}{self.after}"


# The longest value a sentence says inline; a longer one is left to the table.
_SAID_INLINE = 60


def asked(held: ApprovalRequest, shown: ChangePreview | None = None) -> Asked:
    """The sentence for ``held``: what its capability says it wants
    (``CapabilitySpec.wanted``), a switch as the side it sets, and a change of
    one value as that change. ``shown`` is its preview, when already read."""

    shown = preview(held) if shown is None else shown
    spec = capabilities.capability_registry().get(held.capability)
    # A switch is said as the side it sets, never as a value moving.
    moved = _one_move(shown) if spec is not None and spec.target_kind and not switch_field(spec) else None
    if moved is not None:
        expected = "expected " if spec.subject_resource == DECLARATIONS else ""
        phrase = f"change the {expected}{lower_first(moved.label)} of {TARGET} from {moved.before} to {moved.after}"
    else:
        phrase = _wanted(spec, held)
    if not held.target:
        phrase = phrase.replace(f" {TARGET}", "").replace(TARGET, "")
    before, _, after = phrase.partition(TARGET)
    return Asked(
        held.requested_actor,
        f"wants to {before}",
        _named(held) if TARGET in phrase else "",
        after,
        whole=moved is not None,
    )


def _named(held: ApprovalRequest) -> str:
    """What a person knows the target by: a record's name, or the name a
    declaration answers for, from what was read when it was asked; its key
    where neither says one."""

    declared = held.baseline.get("spec")
    if held.resource_kind and isinstance(declared, dict):
        return record_name(held.resource_kind, declared, held.target, leads=False)
    read = held.baseline.get("resource", held.baseline)
    return (item_name(read) if isinstance(read, dict) else "") or held.target


def title(held: ApprovalRequest) -> str:
    """What the audit log calls ``held``: its command, and what it acts on as a
    reader knows it, the record it names or the machines a run is bound to
    (``automation_runs.run_subject``). The key HQ files it under links the
    record and is never its title."""

    hosts = held.baseline.get("hosts")
    on = _named(held) if held.target else ", ".join(map(str, hosts)) if isinstance(hosts, list) else ""
    return f"{capabilities.capability_title(held.capability)}{f' on {on}' if on else ''}"


def _wanted(spec, held: ApprovalRequest) -> str:
    if spec is None:
        phrase = lower_first(human_label(held.capability))
        return f"{phrase} {TARGET}" if held.target else phrase
    switch = switch_field(spec)
    if switch and isinstance(held.payload.get(switch), bool):
        return spec.wanted("on" if held.payload[switch] else "off")
    return spec.wanted()


def _one_move(shown: ChangePreview) -> ChangeRow | None:
    """The one value an amendment moves, when it moves one short enough to say."""

    if not shown.amends or len(shown.changes) != 1 or shown.lines:
        return None
    (row,) = shown.changes
    said = (row.before, row.after)
    if row.change != "changed" or any(len(value) > _SAID_INLINE or "\n" in value for value in said):
        return None
    return row


def review(held: ApprovalRequest) -> dict[str, Any]:
    """What the decision card shows: ``tone`` marks one still waiting for a decision."""

    shown, warnings = marked(preview(held), _warnings(held))
    sentence = asked(held, shown)
    found = ManagedResource.objects.filter(key=held.resource_key).first() if held.resource_kind else None
    # The card opens the page the record lives on, which its own page would redirect to.
    record = replace(entity_link("resource", held.resource_key), url=found.get_absolute_url()) if found else None
    return {
        "held": held,
        "tone": "attention" if held.state == ApprovalRequest.State.PENDING else "",
        "asked": sentence,
        "preview": shown,
        "warnings": warnings,
        "resource_url": record.url if record else "",
        "resource_label": _resource_label(held, record, sentence),
    }


def _resource_label(held: ApprovalRequest, record: Any, sentence: Asked) -> str:
    """What the aside names the record it links by: the sentence's own name
    for it, else its name, else (a record filed by its key alone) its kind."""

    if record is None:
        return ""
    return sentence.target or (record.label if record.title else kind_label(held.resource_kind))


def _warnings(held: ApprovalRequest) -> tuple[str, ...]:
    """What the request reaches that a person should weigh, said plainly: what
    the handler that measured it says (``WARNINGS``), then what its kind's
    provider reads in the declaration it would write."""

    measured = tuple(str(warning) for warning in held.baseline.get(WARNINGS) or ())
    provider = PROVIDERS.get(held.resource_kind)
    requested = held.payload.get("spec")
    if provider is None or provider.review_warnings is None or not isinstance(requested, dict):
        return measured
    return (*measured, *provider.review_warnings(requested))


def marked(shown: ChangePreview, warnings: tuple[str, ...]) -> tuple[ChangePreview, tuple[str, ...]]:
    """``shown`` with each changed record a warning names, as a handler that
    measures a catalog writes them ("Empty a cache gains ..."), marked as
    widening, and the warnings no record carries. A record's own row already
    says what moved, so its warning is not said again."""

    changed = sorted((group.label for group in shown.groups if group.changed), key=len, reverse=True)
    carried: set[str] = set()
    rest = []
    for warning in warnings:
        named = next((label for label in changed if warning.startswith(f"{label} ")), None)
        if named is None:
            rest.append(warning)
        else:
            carried.add(named)
    groups = tuple(replace(group, widens=True) if group.label in carried else group for group in shown.groups)
    return replace(shown, groups=groups), tuple(rest)
