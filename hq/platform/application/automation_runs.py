"""Running an automation procedure: the command each procedure of the adopted
catalog is asked through, and the operation it queues for the automation worker.

A run passes HQ's one capability checkpoint like any command, and nothing runs
here. What it binds to, and what its approval is measured against:
docs/Architecture.md, "Running a playbook".
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, make_dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

from django.core.exceptions import ImproperlyConfigured, ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import get_text_list
from pydantic import Field, StrictBool, StrictStr, StringConstraints

from hq.domains.control_plane.names import HOSTNAME_LENGTH, HOSTNAME_PATTERN, normalized_hostname
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
lazy from hq.platform.core.attribution import own_operation
lazy from hq.platform.core.templatetags.value_tags import seconds

from .automation_manifest import OperationManifest, OperationRegistry, sorted_unique
from .command_fields import Among, AmongNow, Checked, Describe, ForAgents, Names, NonEmpty
from .contracts import Effect
from .integration_specs import CapabilitySpec, command_adapter
from .process_caches import kept
from .quoted import Quoted
from .record_errors import PolicyError, RecordNotFound
from .security import Capability, Principal
lazy from . import approvals, automation_adoption, pages
lazy from .action_links import command_url
lazy from .approvals import ApprovalSubject, every_call, measured_by, waits_for_a_person
lazy from .automation_sources import PLAYBOOKS
lazy from .cadence import ask, automation_standing
lazy from .capabilities import Consented, consent
lazy from .controller import release_lapsed
lazy from .entity_links import kind_label
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource
lazy from .json_schemas import json_schema
lazy from .labels import lower_first
lazy from .machines import machine_catalog
lazy from .moments import moment, since_when
lazy from .playbook_about import about, reading_words, run_preview
lazy from .resource_operations import (
    Prepared,
    RunAsked,
    acting_for,
    asked_fields,
    enqueue,
    operation_asked,
    serialize_operation,
)
lazy from .run_reads import RUNS
lazy from .ui import agreed, ended
lazy from .work_runs import RUN, WorkRun, operation_of, run_url

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
# The word of a run's input that makes it a preview, run in Ansible's check
# mode (the bridge's ``RunInput.check``), written by ``queue_run`` alone.
CHECK = "check"
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
PREVIEW = Field(title="Preview")
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

type Preview = Annotated[
    StrictBool,
    # The label says it on the form, so the sentence is the agent's alone.
    Describe(
        "",
        api="True to run it in Ansible's check mode, which reports what it would change. Only a task its "
        "playbook marks `check_mode: false` acts, with the access a run has.",
    ),
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
    # A preview is the run itself in check mode, as AWX's job type and
    # ansible-playbook's ``--check`` are, so it is granted, approved and logged
    # in as the run is (docs/Architecture.md, "Previewing a run").
    check: Annotated[Preview, PREVIEW] = False


def command_type(manifest: OperationManifest) -> type[RunCommand]:
    """The command one procedure is run with: the run's own fields, its
    parameters as its manifest's input type, and the retry key of a change."""

    parameters = manifest.input_type
    takes_none = asks_nothing(manifest)
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
    is measured against, and but its finding rules, readings and settling
    read, which say where it is offered and what to read back, and nothing of
    what a run does.
    """

    return validate_spec(
        OPERATION_KIND,
        {
            **manifest.model_dump(
                mode="json",
                exclude={"id", "input_type", "privilege", "fixes", "investigates", "readings", "settled_by"},
            ),
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
            "details.missing lists each. A connection added since the last scan shows once a scan reads it.",
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
    revision never applies another and a new adoption stales it. A preview's
    approval never runs one that acts, since ``check`` is measured too."""

    bound = _bound(manifest, command)
    return ApprovalSubject(
        OPERATION_KIND,
        resource_key(manifest.id),
        {"spec": pinned(manifest), "parameters": bound["parameters"], "hosts": bound["hosts"], CHECK: command.check},
    )


def record_of(manifest: OperationManifest, *, consented: Consented) -> ManagedResource:
    """The procedure's record, written as its manifest now says.

    Written when a run is asked for, where a transaction, an actor and an audit
    binding exist, never as the registry is composed in every process before
    migrations. The read that settles it (``settled_by``) is written beside
    it, under the same consent, since HQ asks that read itself.
    """

    if manifest.settled_by is not None and (settling := operation_registry().get(manifest.settled_by)) is not None:
        record_of(settling, consented=consented)
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


def _holding(resource: ManagedResource | None, hosts: list[str]) -> OperationRequest | None:
    """A run waiting or working on this procedure (none before its first run), else one on any of these hosts.

    One run per procedure is also the database's rule
    (``one_active_operation_per_resource_action``); one writer per host is
    this, read under the write lock every request takes.
    """

    active = OperationRequest.objects.select_related("resource").filter(
        action=OperationRequest.Action.RUN, state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED)
    )
    same = active.filter(resource=resource).first() if resource is not None else None
    if same is not None:
        return same
    return active.filter(hosts__host__in=hosts).order_by("created_at").first()


def _held_by(holder: OperationRequest, resource: ManagedResource | None, hosts: list[str]) -> dict[str, Any]:
    """What a refusal says of the run in the way: the run, the machines it is
    on, and the asked machines it holds, none when it is this playbook's own
    run, so a page can say which of the two is held (``held_words``)."""

    machines = sorted(holder.hosts.values_list("host", flat=True))
    own = resource is not None and holder.resource_id == resource.pk
    shared = [] if own else sorted(set(machines) & set(hosts))
    return {
        "holding": serialize_operation(holder),
        "playbook": str((holder.input or {}).get("operation", "")),
        "machines": machines,
        "shared": shared,
    }


def run_in_way(manifest: OperationManifest, hosts: Iterable[str]) -> str:
    """Why a run of ``manifest`` on ``hosts`` asked now would be refused for
    another run waiting or working (``_holding``), as a page says it; "" when
    none is in its way, so a page offers no press it refuses."""

    asked = list(hosts)
    resource = ManagedResource.objects.filter(key=resource_key(manifest.id)).first()
    holder = _holding(resource, asked)
    words = held_words(_held_by(holder, resource, asked)) if holder is not None else None
    return words.sentence if words else ""


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
    since = f"{lower_first(state.as_job.label)} {since_when(asked_at)}" if asked_at else lower_first(state.as_job.label)
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


def request_run(manifest: OperationManifest, command: RunCommand, *, consented: Consented) -> RunAsked:
    """Queue one run of ``manifest`` for the automation worker.

    A read takes no retry key, so asking twice queues twice, and the
    procedure's one active run refuses the second.
    """

    return queue_run(
        manifest, _bound(manifest, command), reason=command.reason, consented=consented, check=command.check
    )


@transaction.atomic
def queue_run(
    manifest: OperationManifest, bound: dict[str, Any], *, reason: str, consented: Consented, check: bool = False
) -> RunAsked:
    """Queue a bound request through the same machine locks and audit trail,
    as a preview where ``check`` says so: the one place a run's input is made
    one, so a run asked any way previews only through this."""

    asked = {**bound, CHECK: True} if check else bound
    operation, queued = _enqueue_run(
        partial(record_of, manifest, consented=consented), asked, consent(consented).principal, reason=reason
    )
    return operation_asked(operation, queued=queued)


@transaction.atomic
def queue_follow_up(
    manifest: OperationManifest, bound: dict[str, Any], *, asked: OperationRequest, once: str
) -> OperationRequest:
    """Queue the run another operation carries as its own next step, as that
    operation's requester and its approver, once for ``once``.

    It writes no record: the run's record is the one written when the step
    was approved (``record_of``), and one that no longer pins ``manifest``
    refuses it, as an adoption since then does.
    """

    def record() -> ManagedResource:
        resource = ManagedResource.objects.select_for_update().filter(key=resource_key(manifest.id)).first()
        if resource is None or resource.kind != OPERATION_KIND or resource.spec != pinned(manifest):
            raise PolicyError("The playbook adopted now is not the one approved, so nothing ran.")
        return resource

    acting = acting_for(asked)
    # Its rows are its requester's, under the operation that carried it.
    with own_operation(asked, operation=capability_name(manifest.id), principal=acting):
        operation, _ = _enqueue_run(record, bound, acting, reason=asked.reason, once=once)
    return operation


def _enqueue_run(
    record: Callable[[], ManagedResource], bound: dict[str, Any], principal: Principal, *, reason: str, once: str = ""
) -> tuple[OperationRequest, bool]:
    def prepare() -> Prepared:
        # A run no worker will take would wait for nothing: the finding that
        # says so (``automation_findings``) carries how to set one up.
        untaken = automation_standing().cannot("run", now=timezone.now())
        if untaken:
            raise PolicyError(untaken)
        resource = record()
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
        return resource, asked_fields(principal, reason=reason, generation=resource.generation, **bound)

    operation, queued = enqueue(OperationRequest.Action.RUN, prepare, once=once)
    if queued:
        OperationHost.objects.bulk_create(OperationHost(operation=operation, host=host) for host in bound["hosts"])
        # The worker pulls the run over the bridge; the ring only says one
        # waits, and only once it is stored for the claim to find.
        ask(doorbell="automation")
    return operation, queued


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


def asks_nothing(manifest: OperationManifest) -> bool:
    """Whether a run of ``manifest`` can be asked with no input of its own."""

    return not any(each.is_required() for each in manifest.input_type.model_fields.values())


def playbooks_for(rule: str, machine: str) -> Iterable[OperationManifest]:
    """Adopted playbooks written for the machine and the finding's rule."""

    return (
        manifest
        for manifest in operation_registry().values()
        if (rule in manifest.fixes or rule in manifest.investigates) and manifest.admits(MACHINE_KIND, machine)
    )


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


def stated_words(statements: tuple[str, ...]) -> Quoted:
    """An author's statements as one text, each a sentence: still theirs."""

    return Quoted(" ".join(ended(statement) for statement in statements))


def anywhere(target_kinds: Iterable[str]) -> str:
    """Where a playbook that names no machine may run, in every page's words: "Any machine"."""

    return f"Any {' or '.join(lower_first(kind_label(kind)) for kind in target_kinds)}"


def timeout_words(timeout_seconds: object) -> str:
    """How long a run of a playbook has, in every page's words: "10 min"."""

    return str(seconds(timeout_seconds))


def access_words(privilege: str | None) -> str:
    """What a run may do on a machine beyond its login, in every page's words:
    sudo as the profile ``privilege`` names, or none."""

    return f"sudo ({privilege})" if privilege else "No sudo"


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
        *(f"{lead} {stated_words(statements)}" for lead, statements in said if statements),
        ended(f"Retry: {RETRY_WORDS[manifest.retry]}"),
    )


WAITS_FOR_APPROVAL = "When an agent asks, the run waits for an operator's approval."
PREVIEW_NOTE = (
    "With `check` true it is a preview, Ansible's check mode: granted, approved and logged in as a run that acts, "
    "where only a task its playbook marks `check_mode: false` acts."
)


def _reading_notes(manifest: OperationManifest) -> tuple[str, ...]:
    """Which reading a run of ``manifest`` states, for an agent to read back once it ends."""

    return tuple(
        f"It states the reading `{kind}` ({reading_words(kind)}), in its run's `stated` on `{RUNS}`."
        for kind in manifest.readings
    )


def _capability(manifest: OperationManifest, titles: Mapping[str, str]) -> CapabilitySpec:
    name = capability_name(manifest.id)
    longest = ApprovalRequest._meta.get_field("capability").max_length
    if longest is not None and len(name) > longest:
        raise ImproperlyConfigured(
            f"Operation {manifest.id!r} has an id too long to be run: {name!r} is over {longest} characters."
        )

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
        "Run this adopted playbook on the machines you name.",
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
            *_reading_notes(manifest),
            PREVIEW_NOTE,
            *_reviewed_notes(manifest),
        ),
        label=run_title(manifest.id, titles),
        verb=OperationRequest.Action.RUN.label,
        # On a run's page once it has ended: the same playbook on the same machines.
        # A machine's page, which offers each playbook that runs there by this one name.
        variants=((AGAIN, f"{OperationRequest.Action.RUN.label} again"), (HERE, RUN_HERE)),
        # Read on a held run's card, whose Playbook row and link name which.
        wants="run a playbook",
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
    return (
        _cancel_spec(),
        *(_capability(manifest, titles) for manifest in operation_registry(digest).values()),
    )
