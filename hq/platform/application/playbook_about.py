"""What a playbook's command page and a held run's card say of it, from its
adopted manifest: the facts its page states, its runs waiting for approval and
its latest runs, and what approving a held run applies."""

from django.utils.text import get_text_list

from hq.platform.application.url_query import with_query
lazy from hq.domains.control_plane.models import ApprovalRequest

from .automation_manifest import OperationManifest
from .automation_runs import (
    CHECK,
    MACHINES,
    PREVIEW,
    RETRY_WORDS,
    access_words,
    capability_name,
    run_title,
    stated_words,
    timeout_words,
)
from .contracts import Effect
from .integration_specs import CommandAbout, CommandFact
from .security import Principal
from .tone import Tone
lazy from . import approvals, automation_adoption
lazy from .approval_review import ChangePreview
lazy from .automation_sources import SHORT_REVISION
lazy from .doors import NOBODY
lazy from .entity_links import entity_link
lazy from .findings import rule_for
lazy from .history import asked_by
lazy from .json_schemas import fields as whole_fields, json_schema
lazy from .moments import when
lazy from .payload_words import ChangeRow, field_words, value_words
lazy from .playbook_reach import reachable
lazy from .routes import reverse
lazy from .service_context import Cell, ServiceSection, machines_cell, recent_work
lazy from .work_runs import recent_runs, wording


def _rule_fact(label: str, rules: tuple[str, ...]) -> tuple[CommandFact, ...]:
    """The findings a playbook names, in those rules' words."""

    titles = "; ".join(found.title for rule in rules if (found := rule_for(rule)))
    return (CommandFact(label, titles),) if titles else ()


def _runs_on(manifest: OperationManifest) -> tuple[CommandFact, ...]:
    """The machines a run of the procedure can be asked on now, each by its link."""

    links = tuple(entity_link("machine", host) for host in reachable(manifest))
    return (CommandFact("Runs on", links=links),) if links else ()


def _held_runs(manifest: OperationManifest, principal: Principal) -> ServiceSection | None:
    """The runs of ``manifest`` waiting for a person's approval that
    ``principal`` may read, each linked to the request it is decided on; None
    when none waits."""

    waiting = approvals.pending(principal=principal, capability=capability_name(manifest.id))
    who = asked_by(ApprovalRequest, waiting)
    records = tuple(
        (
            machines_cell([str(host) for host in held.baseline.get("hosts") or ()]),
            Cell(
                "Waiting for approval",
                reverse("core:approval_entry", kwargs={"approval_id": held.id}),
                tone=Tone.ATTENTION,
            ),
            Cell(when(held.created_at), muted=True),
            Cell(who.get(str(held.pk), NOBODY).words),
        )
        for held in waiting
    )
    if not records:
        return None
    return ServiceSection(
        id="held-runs", label="Waiting for approval", columns=("Machines", "State", "Asked", "By"), records=records
    )


def about(manifest: OperationManifest, principal: Principal) -> CommandAbout:
    """What a procedure's command page says of it to ``principal``: what its
    adopted catalog declares, its runs waiting for approval, and how its
    latest runs went, with their evidence where they may read Jobs. Its
    inputs are the form's own fields."""

    facts = (
        *_runs_on(manifest),
        CommandFact("Effect", manifest.effect.label),
        CommandFact("Access", access_words(manifest.privilege)),
        CommandFact("Timeout", timeout_words(manifest.timeout_seconds)),
        CommandFact("Preconditions", stated_words(manifest.preconditions)),
        CommandFact("Verification", stated_words(manifest.verification)),
        CommandFact("Recovery", stated_words(manifest.recovery)),
        CommandFact("Retry", RETRY_WORDS[manifest.retry]),
        *_rule_fact("Fixes", manifest.fixes),
        *_rule_fact("Investigates", manifest.investigates),
        *(
            (CommandFact("Reading", "; ".join(reading_words(kind) for kind in manifest.readings)),)
            if manifest.readings
            else ()
        ),
        CommandFact("Adopted commit", manifest.revision[:SHORT_REVISION]),
        CommandFact("Playbook file", manifest.playbook),
    )
    every = with_query(reverse("jobs:list"), {"kind": capability_name(manifest.id)})
    sections = (
        _held_runs(manifest, principal),
        recent_work(recent_runs(manifest.id), by_machine=False, more_url=every, principal=principal),
    )
    return CommandAbout(label="Playbook", facts=facts, sections=tuple(section for section in sections if section))


def run_preview(manifest: OperationManifest, held: ApprovalRequest) -> ChangePreview:
    """What a held run would do, as its card shows it: its machines and its
    inputs, each by its form's label, then what it does, its access and the
    commit adopted. Read from what the run was measured against
    (``run_subject``), so it shows what approving it applies; only the
    inputs' labels come from ``manifest``, the catalog adopted now."""

    pinned = dict(held.baseline.get("spec") or {})
    inputs = whole_fields(json_schema(manifest.input_type))
    hosts = [str(host) for host in held.baseline.get("hosts") or ()]
    said = (
        ("Playbook", run_title(manifest.id, automation_adoption.playbook_titles())),
        (str(MACHINES.title), get_text_list(hosts, "and")),
        (str(PREVIEW.title), value_words(True) if held.baseline.get(CHECK) else ""),
        *(
            (field_words(name, inputs.get(name)), value_words(value, inputs.get(name)))
            for name, value in dict(held.baseline.get("parameters") or {}).items()
        ),
        ("Effect", Effect(pinned["effect"]).label if pinned.get("effect") in Effect else ""),
        ("Access", access_words(str(pinned.get("privilege", "")))),
        ("Adopted commit", str(pinned.get("revision", ""))[:SHORT_REVISION]),
        (field_words("correlation"), str(held.payload.get("correlation") or "")),
    )
    rows = tuple(ChangeRow(label=label, before="", after=value, change="added") for label, value in said if value)
    return ChangePreview(label="Asked with", rows=rows, lines=())


def reading_words(kind: str) -> str:
    """A kind of reading as every page names it: the label HQ declares for it (``work_runs.wording``), else its kind."""

    declared = wording(kind)
    return declared.label if declared is not None else kind
