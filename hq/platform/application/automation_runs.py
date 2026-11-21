"""Running an automation procedure: the command each procedure of the adopted
catalog is asked through, and the operation it queues for the automation worker.

A run passes HQ's one capability checkpoint like any command, and nothing runs
here. What it binds to, and what its approval is measured against:
docs/Architecture.md, "Running a playbook".
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, make_dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

from django.core.exceptions import ImproperlyConfigured, ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import get_text_list
from pydantic import Field, StrictStr, StringConstraints

from hq.domains.control_plane.names import HOSTNAME_LENGTH, HOSTNAME_PATTERN, normalized_hostname
from hq.platform.application.url_query import with_query
from hq.platform.core.digests import digest
lazy from hq.domains.control_plane.models import (
    ApprovalRequest,
    ManagedResource,
    OperationHost,
    OperationRequest,
    SourceProposal,
)
lazy from hq.domains.control_plane.provider_adapters.automation import OPERATION_KIND
lazy from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND
lazy from hq.domains.control_plane.providers import validate_spec
lazy from hq.platform.core.templatetags.value_tags import seconds

from .automation_manifest import OperationManifest, OperationRegistry, sorted_unique
from .command_fields import Among, AmongNow, Checked, Describe, ForAgents, Names, NonEmpty
from .contracts import Effect
from .integration_specs import CapabilitySpec, CommandAbout, CommandFact, command_adapter
from .process_caches import kept
from .record_errors import PolicyError, RecordNotFound
from .security import Capability, Principal
from .tone import Tone
lazy from . import approvals, automation_adoption, pages
lazy from .action_links import command_url
lazy from .approval_review import ChangePreview
lazy from .approvals import ApprovalSubject, every_call, measured_by, waits_for_a_person
lazy from .automation_sources import PLAYBOOKS, SHORT_REVISION
lazy from .cadence import ask, automation_standing
lazy from .capabilities import Consented, consent
lazy from .controller import release_lapsed
lazy from .doors import NOBODY
lazy from .entity_links import entity_link, kind_label
lazy from .findings import rule_for
lazy from .history import asked_by
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource
lazy from .json_schemas import fields as whole_fields, json_schema
lazy from .labels import lower_first
lazy from .machines import machine_catalog
lazy from .moments import ago, moment, when
lazy from .payload_words import ChangeRow, field_words, value_words
lazy from .resource_operations import (
    Prepared,
    RunAsked,
    asked_fields,
    enqueue,
    operation_asked,
    serialize_operation,
)
lazy from .routes import reverse
lazy from .run_reads import RUNS
lazy from .service_context import Cell, ServiceSection, machines_cell, recent_work
lazy from .ui import agreed, ended
lazy from .work_runs import RUN, WorkRun, operation_of, recent_runs, run_url

if TYPE_CHECKING:
    from .pages import PageAction


# One catalog at a time: a process builds its commands from the catalog
# adopted now, and builds them anew once another is.
@kept
def _registry(generation: str) -> OperationRegistry:
    return OperationRegistry(automation_adoption.manifests(generation))


def operation_registry(generation: str | None = None) -> OperationRegistry:
    """Every procedure of the adopted catalog, by id, built once per adoption.

    ``generation`` is the catalog's digest (``automation_adoption.generation``),
    read now when not given.
    """

    return _registry(automation_adoption.generation() if generation is None else generation)


# The variant of a playbook's run command that runs it again as it ran.
AGAIN = "again"
HERE = "here"
RUN_HERE = "Run playbook"


def capability_name(operation_id: str) -> str:
    """The command a procedure is run through, which a grant or a rule names it by."""

    return f"automation.{operation_id}.run"


def runs_procedure(name: str) -> bool:
    """Whether the command named ``name`` is in the namespace ``capability_name``
    gives the adopted catalog's procedures, the only commands an adoption moves."""

    return name.startswith("automation.") and name.endswith(".run")


def run_title(operation_id: str, titles: Mapping[str, str]) -> str:
    """A procedure's run in words, as its command's heading and button and a
    list of work say it: its playbook's title among ``titles``
    (``automation_adoption.playbook_titles``), else Run and its id."""

    return titles.get(operation_id) or f"{OperationRequest.Action.RUN.label} {operation_id}"


def resource_key(operation_id: str) -> str:
    """The key of a procedure's record: its id in a key's spelling. A dotted
    name's segments hold no hyphen, so no two ids share a key."""

    return f"automation-{operation_id.replace('.', '-')}"


type Hosts = Annotated[
    tuple[
        Annotated[
            StrictStr,
            StringConstraints(strip_whitespace=True, max_length=HOSTNAME_LENGTH, pattern=HOSTNAME_PATTERN),
            Checked(normalized_hostname),
        ],
        ...,
    ],
    NonEmpty(),
    Field(max_length=64),
    Checked(sorted_unique),
    Names("machines"),
    # The label says it on the form, so the sentence is the agent's alone.
    Describe("", api="The machines to run it on, each by its name in HQ."),
]
# What a run's hosts are titled wherever a person reads them, as every page
# names them: on the field's own use, since an alias's title names the alias.
MACHINES = Field(title="Machines")
# The longest correlation id a run keeps, and a character of one: printable,
# no spaces, and no comma, which separates the ids Jobs is filtered by.
CORRELATION_LONGEST = 200
CORRELATION_CHARACTER = r"[!-+\--~]"
CORRELATION_PATTERN = rf"^{CORRELATION_CHARACTER}*$"
type Correlation = Annotated[
    StrictStr,
    Field(max_length=CORRELATION_LONGEST, pattern=CORRELATION_PATTERN),
    Describe("", api="Your own id to find this run by later, kept with the run."),
    ForAgents(),
]
type Reason = Annotated[
    StrictStr, Field(max_length=300), Describe("", api="Why it is run, kept with the operation it queues.")
]


@dataclass(frozen=True, kw_only=True)
class RunCommand:
    """What every run takes beside its playbook's own parameters.

    Closed like every command, so a playbook path, an inventory, extra-vars or
    another path sent beside the parameters is refused, and the playbook's
    input type refuses one sent inside them (``automation_manifest._closed``).
    """

    hosts: Annotated[Hosts, MACHINES]
    # Each procedure's own input type, which ``command_type`` declares.
    parameters: Any
    correlation: Correlation = ""
    reason: Reason = ""


def command_type(manifest: OperationManifest) -> type[RunCommand]:
    """The command one procedure is run with: the run's own fields, its
    parameters as its manifest's input type, and the retry key of a change."""

    parameters = manifest.input_type
    takes_none = not any(each.is_required() for each in parameters.model_fields.values())
    # Offered as a person's choices, the machines a run can reach now
    # (``reachable``); ``reached`` refuses any other however it is asked.
    now = AmongNow(partial(reachable, manifest))
    hosts: Any = Annotated[Hosts, now, MACHINES]
    if manifest.hosts or MACHINE_KIND not in manifest.target_kinds:
        # What ``resolved_hosts`` admits, published as the only choices.
        admitted = manifest.hosts if MACHINE_KIND in manifest.target_kinds else ()
        hosts = Annotated[Hosts, Among(admitted), now, MACHINES]
    fields: list[tuple[str, Any, Any]] = [
        ("hosts", hosts, field()),
        (
            "parameters",
            Annotated[parameters, Describe("The playbook's own inputs, as its manifest lists them.")],
            field(default_factory=parameters) if takes_none else field(),
        ),
    ]
    return make_dataclass(
        f"Run{''.join(part.title() for part in manifest.id.replace('.', '_').split('_'))}",
        fields,
        bases=(RunCommand,),
        frozen=True,
        kw_only=True,
        module=__name__,
    )


def pinned(manifest: OperationManifest) -> dict[str, Any]:
    """The manifest as its record's spec holds it: what a run binds to.

    Every field but the input type, which its schema's digest stands for, so a
    new revision or a changed input moves the spec and with it what a held run
    is measured against, and but its finding rules, which say where it is
    offered and nothing of what a run does.
    """

    return validate_spec(
        OPERATION_KIND,
        {
            **manifest.model_dump(mode="json", exclude={"id", "input_type", "privilege", "fixes", "investigates"}),
            "operation": manifest.id,
            "privilege": manifest.privilege or "",
            "input_schema_sha256": digest(json_schema(manifest.input_type)),
        },
    )


def resolved_hosts(manifest: OperationManifest, hosts: tuple[str, ...]) -> tuple[str, ...]:
    """Each host named, as the machine HQ knows it by, held to what the manifest admits.

    A machine is found by its name or any name it is also known by, so a run
    asked by an alias binds to the one machine it is, and two runs naming one
    machine two ways are seen to share it.
    """

    known: dict[str, str] = {}
    for machine in machine_catalog():
        canonical = normalized_hostname(machine.name)
        for name in (machine.name, *machine.aliases):
            known.setdefault(normalized_hostname(name), canonical)
    unknown = [host for host in hosts if host not in known]
    if unknown:
        raise RecordNotFound(
            "A host this names is no machine HQ knows; details.unknown lists them.", details={"unknown": unknown}
        )
    resolved = tuple(sorted({known[host] for host in hosts}))
    refused = [host for host in resolved if not manifest.admits(MACHINE_KIND, host)]
    if refused:
        raise PolicyError(
            "This playbook is not written for a machine this names; details.refused lists them, and "
            "details.admits what it is written for.",
            details={
                "refused": refused,
                "admits": {"target_kinds": list(manifest.target_kinds), "hosts": list(manifest.hosts)},
            },
        )
    return resolved


# The privilege profile of the worker's login that escalates to nothing, as
# the controller's connections.ReadProfile and hq_runner's READ_PROFILE name it.
READ_PROFILE = "read"


def login_profile(manifest: OperationManifest) -> str:
    """The profile of the login a run of ``manifest`` goes through: its
    privilege, or the read login's for one that names none."""

    return manifest.privilege or READ_PROFILE


def runner_reaches() -> frozenset[tuple[str, str]]:
    """Each machine and profile the worker has a login for, as its newest scan said."""

    newest = SourceProposal.objects.values_list("reaches", flat=True).first()
    return frozenset((normalized_hostname(found["machine"]), found["profile"]) for found in newest or ())


def reachable(manifest: OperationManifest) -> frozenset[str]:
    """The machines a run of ``manifest`` can be asked on now, each by its
    name in HQ: those it is written for that the worker has a login to as its
    profile, by any name the machine is known by, as ``reached`` holds a run to."""

    profile = login_profile(manifest)
    reaches = runner_reaches()
    return frozenset(
        machine.name
        for machine in machine_catalog()
        if manifest.admits(MACHINE_KIND, normalized_hostname(machine.name))
        and any((normalized_hostname(name), profile) in reaches for name in (machine.name, *machine.aliases))
    )


def reached(manifest: OperationManifest, hosts: tuple[str, ...]) -> None:
    """Refuse a run on a machine the worker has no login to as the run's profile.

    The worker's own word, so it can make this check wrong and nothing more:
    the worker refuses at the claim a machine it cannot reach as the profile.
    """

    profile = login_profile(manifest)
    reaches = runner_reaches()
    missing = [{"machine": host, "profile": profile} for host in hosts if (host, profile) not in reaches]
    if missing:
        raise PolicyError(
            "HQ has no runner connection to a machine this names, as this playbook's profile; "
            "details.missing lists each. A connection added since the last Refresh shows once you Refresh.",
            details={"missing": missing},
        )


def _bound(manifest: OperationManifest, command: RunCommand) -> dict[str, Any]:
    """What a run of ``command`` binds to: the procedure, its parameters as
    validated, and its hosts as resolved, each one the worker can reach."""

    hosts = resolved_hosts(manifest, command.hosts)
    reached(manifest, hosts)
    return {
        "operation": manifest.id,
        "revision": manifest.revision,
        "parameters": command_adapter(type(command)).dump_python(command, mode="json")["parameters"],
        "hosts": list(hosts),
        "correlation": command.correlation,
    }


def run_subject(manifest: OperationManifest, command: RunCommand) -> ApprovalSubject:
    """What a held run is about and measured against: the procedure as the
    catalog adopted now pins it, the parameters as validated and the hosts as
    they resolve now. Read from the catalog rather than the procedure's record,
    which a run writes only when it is asked for, so an approval of one
    revision never applies another and a new adoption stales it."""

    bound = _bound(manifest, command)
    return ApprovalSubject(
        OPERATION_KIND,
        resource_key(manifest.id),
        {"spec": pinned(manifest), "parameters": bound["parameters"], "hosts": bound["hosts"]},
    )


def _record(manifest: OperationManifest, *, consented: Consented) -> ManagedResource:
    """The procedure's record, written as its manifest now says.

    Written when a run is asked for, where a transaction, an actor and an audit
    binding exist, never as the registry is composed in every process before
    migrations.
    """

    key = resource_key(manifest.id)
    current = ManagedResource.objects.select_for_update().filter(key=key).first()
    spec = pinned(manifest)
    if current is None or current.kind != OPERATION_KIND or current.spec != spec:
        save_managed_resource(
            ManagedResourceCommand(
                key=key, kind=OPERATION_KIND, spec=spec, enabled=current.enabled if current else True
            ),
            consented=consented,
            current_key=key if current else None,
        )
    return ManagedResource.objects.select_for_update().get(key=key)


def _holding(resource: ManagedResource, hosts: list[str]) -> OperationRequest | None:
    """A run waiting or working on this procedure, else one on any of these hosts.

    One run per procedure is also the database's rule
    (``one_active_operation_per_resource_action``); one writer per host is
    this, read under the write lock every request takes.
    """

    active = OperationRequest.objects.select_related("resource").filter(
        action=OperationRequest.Action.RUN, state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED)
    )
    same = active.filter(resource=resource).first()
    if same is not None:
        return same
    return active.filter(hosts__host__in=hosts).order_by("created_at").first()


def _held_by(holder: OperationRequest, resource: ManagedResource, hosts: list[str]) -> dict[str, Any]:
    """What a refusal says of the run in the way: the run, the machines it is
    on, and the asked machines it holds, none when it is this playbook's own
    run, so a page can say which of the two is held (``held_words``)."""

    machines = sorted(holder.hosts.values_list("host", flat=True))
    shared = [] if holder.resource_id == resource.pk else sorted(set(machines) & set(hosts))
    return {
        "holding": serialize_operation(holder),
        "playbook": str((holder.input or {}).get("operation", "")),
        "machines": machines,
        "shared": shared,
    }


@dataclass(frozen=True, slots=True)
class HeldWords:
    """A refused run's reason in a page's words, and where the run in its way is shown."""

    sentence: str
    href: str
    link: str = "See that run"
    # The button that cancels that run, for a reader who may cancel it now.
    cancel: PageAction | None = None


def held_words(details: object, principal: Principal | None = None) -> HeldWords | None:
    """What a page says of the run a refusal names in ``details`` (``_held_by``):
    which playbook it runs, on which machines, in what state and since when,
    and whether this playbook or a machine is what it holds, with the button
    that cancels it where ``principal`` may. None for a refusal that names no
    run in the way."""

    if not isinstance(details, dict) or not isinstance(details.get("holding"), dict):
        return None
    holding = details["holding"]
    playbook = str(details.get("playbook") or "")
    title = run_title(playbook, automation_adoption.playbook_titles())
    state = OperationRequest.State(holding["state"])
    asked_at = moment(holding.get("created_at"))
    since = f"{lower_first(state.as_job.label)} since {when(asked_at)}" if asked_at else lower_first(state.as_job.label)
    machines = [str(host) for host in details.get("machines") or ()]
    shared = [str(host) for host in details.get("shared") or ()]
    if shared:
        busy = f"{get_text_list(shared, 'and')} {agreed(len(shared), 'is', 'are')} busy with a run of “{title}”"
        sentence = f"{ended(f'{busy}, {since}')} A machine takes one run at a time, so ask again once that run ends."
    else:
        held = f"“{title}” already has a run on {get_text_list(machines, 'and')}, {since}"
        sentence = f"{ended(held)} A playbook runs once at a time, so ask again once that run ends."
    return HeldWords(sentence, run_url(str(holding["id"])), cancel=_holder_cancel(holding, principal))


def _holder_cancel(holding: dict[str, Any], principal: Principal | None) -> PageAction | None:
    """The button that cancels the run in the way, read as it stands now, for a ``principal`` who may."""

    if principal is None:
        return None
    holder = OperationRequest.objects.select_related("resource").filter(pk=holding.get("id")).first()
    if holder is None or not _offers_cancel(holder.state, str(holder.resource.spec.get("effect", "")), principal):
        return None
    return _cancel_button(str(holder.pk))


@transaction.atomic
def request_run(manifest: OperationManifest, command: RunCommand, *, consented: Consented) -> RunAsked:
    """Queue one run of ``manifest`` for the automation worker.

    A read takes no retry key, so asking twice queues twice, and the
    procedure's one active run refuses the second.
    """

    bound = _bound(manifest, command)

    def prepare() -> Prepared:
        # A run no worker will take would wait for nothing: the finding that
        # says so (``automation_findings``) carries how to set one up.
        untaken = automation_standing().cannot("run", now=timezone.now())
        if untaken:
            raise PolicyError(untaken)
        resource = _record(manifest, consented=consented)
        if not resource.enabled:
            raise PolicyError("This playbook is switched off in HQ.")
        # A claim whose lease ran out is ended or put back first, by the rule
        # a claim applies too, so a worker that went quiet holds nothing.
        release_lapsed(timezone.now())
        holder = _holding(resource, bound["hosts"])
        if holder is not None:
            raise PolicyError(
                "Another run holds this playbook or one of these machines; details.holding is that run. "
                "Ask again once it ends.",
                details=_held_by(holder, resource, bound["hosts"]),
            )
        return resource, asked_fields(
            consent(consented).principal, reason=command.reason, generation=resource.generation, **bound
        )

    operation, queued = enqueue(OperationRequest.Action.RUN, prepare)
    if not queued:
        return operation_asked(operation, queued=False)
    OperationHost.objects.bulk_create(OperationHost(operation=operation, host=host) for host in bound["hosts"])
    # The worker pulls the run over the bridge; the ring only says one waits,
    # and only once it is stored for the claim to find.
    ask(doorbell="automation")
    return operation_asked(operation, queued=True)


CANCEL = "automation.run.cancel"
# The reason a run cancelled before it started carries in its result.
CANCELLED_REASON = "Cancelled"


def run_authority(effect: str) -> Capability:
    """What asking for a run of a procedure of ``effect`` takes, and so what cancelling one does."""

    return Capability.READ if effect == Effect.READ else Capability.MANAGE_INFRASTRUCTURE


@dataclass(frozen=True, slots=True)
class CancelRunCommand:
    """The run to cancel, by its operation id."""

    operation_id: Annotated[
        str,
        Describe(
            "The run to cancel.",
            api="Its `operation.id`, from the answer that queued it, or its held request's `operation` once approved.",
        ),
    ]


def _cancellable(operation_id: str, principal: Principal) -> OperationRequest:
    """The run ``principal`` may cancel, locked, else a refusal.

    A person cancels any run they have the authority to ask for. An agent
    cancels only a run it asked for, and another's reads as no run at all.
    """

    try:
        found = (
            OperationRequest.objects.select_for_update()
            .select_related("resource")
            .filter(pk=operation_id, action=OperationRequest.Action.RUN)
            .first()
        )
    except ValueError, DjangoValidationError:
        found = None
    if found is not None and not approvals.reads_every_hold(principal) and not _asked_it(found, principal):
        found = None
    if found is None:
        raise RecordNotFound("No run you may cancel has that id.")
    principal.require(run_authority(str(found.resource.spec.get("effect", ""))))
    return found


def _asked_it(operation: OperationRequest, principal: Principal) -> bool:
    return (operation.requested_actor, operation.requested_interface, operation.requested_person_key) == (
        principal.actor,
        principal.interface,
        principal.person_key,
    )


@transaction.atomic
def cancel_run(command: CancelRunCommand, *, principal: Principal) -> RunAsked:
    """Cancel a run nothing has claimed, so it holds its playbook and machines no more.

    A claimed run is left to its runner, which ends it or lets its lease run
    out, and a lapsed lease holds nothing (``release_lapsed``).
    """

    # A claim whose lease ran out is put back or ended first, by the rule a claim applies.
    release_lapsed(timezone.now())
    operation = _cancellable(command.operation_id, principal)
    if operation.state == OperationRequest.State.CLAIMED:
        raise PolicyError(
            "HQ's runner started this run, so it runs until it ends or the runner stops reporting.",
            details={"operation": serialize_operation(operation)},
        )
    if operation.state != OperationRequest.State.QUEUED:
        raise PolicyError(
            f"This run already ended: {operation.get_state_display().lower()}.",
            details={"operation": serialize_operation(operation)},
        )
    operation.state = OperationRequest.State.CANCELLED
    operation.completed_at = timezone.now()
    operation.result = {
        "ok": False,
        "message": f"{principal.actor} cancelled it before it started.",
        "reason": CANCELLED_REASON,
    }
    operation.save(update_fields=("state", "completed_at", "result", "updated_at"))
    return operation_asked(operation, queued=False)


def _offers_cancel(state: str, effect: str, principal: Principal) -> bool:
    """Whether a page offers ``principal`` the button that cancels a run in
    ``state`` of a playbook of ``effect``: one nothing has claimed, for a
    person who may ask for it themselves."""

    return (
        state == OperationRequest.State.QUEUED
        and approvals.reads_every_hold(principal)
        and principal.permits(Capability.READ, run_authority(effect))
    )


def _cancel_button(operation_id: str) -> PageAction | None:
    """The press that posts the run's id to its cancel command's own form."""

    return pages.PageAction.command(CANCEL, field="operation_id", value=operation_id)


def cancel_action(operation_id: str, playbook: str, state: str, effect: str, principal: Principal) -> PageAction | None:
    """The button that cancels the run of ``playbook`` ``operation_id`` names,
    posting its id, or None; a scan runs no playbook, and is never cancelled."""

    if not playbook or not _offers_cancel(state, effect, principal):
        return None
    return _cancel_button(operation_id)


def run_here_url(operation_id: str, hosts: tuple[str, ...], *, finding: str = "") -> str:
    """The Run page of the playbook ``operation_id`` with ``hosts`` chosen."""

    return command_url(capability_name(operation_id), hosts=hosts, finding=finding)


class PlaybookHere(NamedTuple):
    """A playbook that may run on one machine: its title, its command, and its
    Run page with the machine chosen."""

    title: str
    capability: str
    url: str


def playbooks_on(machine: str, principal: Principal) -> tuple[PlaybookHere, ...]:
    """Each adopted playbook that may run on the machine ``machine`` and that
    ``principal`` may run, A to Z."""

    titles = automation_adoption.playbook_titles()
    found = (
        PlaybookHere(
            run_title(manifest.id, titles), capability_name(manifest.id), run_here_url(manifest.id, (machine,))
        )
        for manifest in operation_registry().values()
        if manifest.admits(MACHINE_KIND, machine) and principal.permits(run_authority(manifest.effect))
    )
    return tuple(sorted(found, key=lambda each: each.title.casefold()))


def playbooks_for(rule: str, machine: str) -> Iterable[OperationManifest]:
    """Adopted playbooks written for the machine and the finding's rule."""

    return (
        manifest
        for manifest in operation_registry().values()
        if (rule in manifest.fixes or rule in manifest.investigates) and manifest.admits(MACHINE_KIND, machine)
    )


def _rule_fact(label: str, rules: tuple[str, ...]) -> tuple[CommandFact, ...]:
    """The findings a playbook names, in those rules' words."""

    titles = "; ".join(found.title for rule in rules if (found := rule_for(rule)))
    return (CommandFact(label, titles),) if titles else ()


def listed_with_actions(runs: Iterable[WorkRun], principal: Principal) -> list[WorkRun]:
    """``runs`` each with what ``principal`` may do to it where it is listed."""

    return [
        replace(run, actions=(action,))
        if run.source == RUN
        and (action := cancel_action(run.id, operation_of(run.kind), run.state, run.effect, principal))
        else run
        for run in runs
    ]


# What a manifest's ``retry`` lets a run that stopped without a known
# outcome do, in every page's words.
RETRY_WORDS = {
    "never": "Never",
    "observe_first": "Only once the machines are looked at",
    "safe": "Safe to run again",
}


def _statements(statements: tuple[str, ...]) -> str:
    return " ".join(ended(statement) for statement in statements)


def anywhere(target_kinds: Iterable[str]) -> str:
    """Where a playbook that names no machine may run, in every page's words: "Any machine"."""

    return f"Any {' or '.join(lower_first(kind_label(kind)) for kind in target_kinds)}"


def _runs_on(manifest: OperationManifest) -> CommandFact:
    """The machines a procedure runs on: each it names by its link, else any
    of the kinds it is written for."""

    if manifest.hosts:
        return CommandFact("Runs on", links=tuple(entity_link("machine", host) for host in manifest.hosts))
    return CommandFact("Runs on", anywhere(manifest.target_kinds))


def timeout_words(timeout_seconds: object) -> str:
    """How long a run of a playbook has, in every page's words: "10 min"."""

    return str(seconds(timeout_seconds))


def access_words(privilege: str | None) -> str:
    """What a run may do on a machine beyond its login, in every page's words:
    sudo as the profile ``privilege`` names, or none."""

    return f"sudo ({privilege})" if privilege else "No sudo"


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
            Cell(ago(held.created_at), muted=True),
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
        _runs_on(manifest),
        CommandFact("Effect", manifest.effect.label),
        CommandFact("Access", access_words(manifest.privilege)),
        CommandFact("Timeout", timeout_words(manifest.timeout_seconds)),
        CommandFact("Preconditions", _statements(manifest.preconditions)),
        CommandFact("Verification", _statements(manifest.verification)),
        CommandFact("Recovery", _statements(manifest.recovery)),
        CommandFact("Retry", RETRY_WORDS[manifest.retry]),
        *_rule_fact("Fixes", manifest.fixes),
        *_rule_fact("Investigates", manifest.investigates),
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
        (str(MACHINES.title), get_text_list(hosts, "and")),
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


def _reviewed_notes(manifest: OperationManifest) -> tuple[str, ...]:
    """What the adopted catalog says of a run: what it may do on a machine,
    how long it has, what it checks before it works, how it shows it worked,
    how a failed one is recovered and whether it is run again, for an agent
    to read before it asks, as a person reads them on the command's page."""

    said = (
        ("Before it works, the playbook checks:", manifest.preconditions),
        ("It shows it worked when:", manifest.verification),
        ("If it fails:", manifest.recovery),
    )
    return (
        ended(f"Access: {access_words(manifest.privilege)}"),
        ended(f"Timeout: {timeout_words(manifest.timeout_seconds)}"),
        *(f"{lead} {_statements(statements)}" for lead, statements in said if statements),
        ended(f"Retry: {RETRY_WORDS[manifest.retry]}"),
    )


WAITS_FOR_APPROVAL = "When an agent asks, the run waits for an operator's approval."


def _capability(manifest: OperationManifest, titles: Mapping[str, str]) -> CapabilitySpec:
    name = capability_name(manifest.id)
    longest = ApprovalRequest._meta.get_field("capability").max_length
    if longest is not None and len(name) > longest:
        raise ImproperlyConfigured(
            f"Operation {manifest.id!r} has an id too long to be run: {name!r} is over {longest} characters."
        )

    # The playbook by its title in quotes, or by its id where it has none.
    named = f"“{titles[manifest.id]}”" if titles.get(manifest.id) else manifest.id
    run = measured_by(partial(run_subject, manifest), shown=partial(run_preview, manifest))(
        partial(request_run, manifest)
    )
    # A read is keyed afresh at each ask (``request_run``), so asking again finds the first run in the way.
    asked_again = (
        "A read takes no `idempotency_key`. Asked again after `call_timed_out`, it is refused while the first "
        "run waits or works, and `details.holding` is that run."
    )
    if manifest.effect.changes:
        # A procedure that changes a machine is a person's to approve each time
        # an agent asks; a person's own click is their consent.
        run = waits_for_a_person(every_call)(run)
    return CapabilitySpec(
        name,
        f"Run the adopted playbook {named} on the machines you name.",
        manifest.effect,
        run_authority(manifest.effect),
        command_type(manifest),
        run,
        execution_notes=(
            "Find each machine named, and refuse one the playbook is not written for.",
            "Refuse while another run waits or works on this playbook or on one of those machines.",
            "Queue it once, and HQ runs it with Ansible outside this request.",
            # What ``waits_for_a_person`` above holds, for an agent to read before it asks.
            *((WAITS_FOR_APPROVAL,) if manifest.effect.changes else (asked_again,)),
            f"Read how it went from `{RUNS}`, by the `operation.id` this answers.",
            *_reviewed_notes(manifest),
        ),
        label=run_title(manifest.id, titles),
        verb=OperationRequest.Action.RUN.label,
        # On a run's page once it has ended: the same playbook on the same machines.
        # A machine's page, which offers each playbook that runs there by this one name.
        variants=((AGAIN, f"{OperationRequest.Action.RUN.label} again"), (HERE, RUN_HERE)),
        # Read on a held run's card, where the procedure is what was asked for.
        wants=f"carry out the playbook {named}",
        # A run belongs to Playbooks, where its playbook is listed.
        home=PLAYBOOKS,
        about=partial(about, manifest),
    )


def _cancel_spec() -> CapabilitySpec:
    return CapabilitySpec(
        CANCEL,
        "Cancel a playbook's run that has not started, so it holds its playbook and machines no more. "
        "Send the run's `operation.id`. An agent may cancel only a run it asked for.",
        Effect.REMOTE_WRITE,
        # Held to the run's own authority by the handler, which reads the run.
        Capability.READ,
        CancelRunCommand,
        cancel_run,
        execution_notes=(
            "Find the run, and refuse one you may not ask for, or another agent's.",
            "Refuse a run HQ's runner has started: it runs until it ends or the runner stops reporting.",
            "Mark it cancelled, so nothing runs, and the next run of its playbook or machines can be asked.",
        ),
        label="Cancel run",
        # Read on a held cancel's card, after "example-agent wants to".
        wants="call off playbook work before HQ's runner starts it",
        reached_from="jobs:list",
        home=PLAYBOOKS,
    )


def capability_specs(generation: str | None = None) -> tuple[CapabilitySpec, ...]:
    """Cancelling a run, and the command each procedure of the adopted catalog is run through."""

    digest = automation_adoption.generation() if generation is None else generation
    titles = automation_adoption.playbook_titles(digest)
    return (_cancel_spec(), *(_capability(manifest, titles) for manifest in operation_registry(digest).values()))
