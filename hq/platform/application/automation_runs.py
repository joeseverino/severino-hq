"""Running an automation procedure: the command each procedure of the adopted
catalog is asked through, and the operation it queues for the automation worker.

A run passes HQ's one capability checkpoint like any command, and nothing runs
here. What it binds to, and what its approval is measured against:
docs/Architecture.md, "Running a playbook".
"""

import hashlib
import json
import secrets
from dataclasses import dataclass
from functools import lru_cache, partial
from typing import Annotated, Any
from urllib.parse import urlencode

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from django.utils.text import get_text_list
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr, create_model

from hq.domains.control_plane.names import is_hostname, normalized_hostname
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
lazy from hq.platform.core.audit import audit_operation
lazy from hq.platform.core.templatetags.value_tags import seconds

from .automation_manifest import OperationManifest, OperationRegistry, sorted_unique
from .command_fields import Among, Describe, Names
from .integration_specs import IDEMPOTENCY_FIELD, CapabilitySpec, CommandAbout, CommandFact
from .record_errors import PolicyError, RecordNotFound
from .security import Capability, Principal
lazy from . import automation_adoption
lazy from .approvals import ApprovalSubject, every_call, measured_by, waits_for_a_person
lazy from .automation_adoption import AdoptedInput, AdoptedProcedure
lazy from .cadence import ring_automation_doorbell
lazy from .controller import release_lapsed
lazy from .entity_links import entity_link, kind_label
lazy from .idempotency import IdempotencyConflict
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource
lazy from .json_schemas import json_schema
lazy from .labels import effect_label, lower_first
lazy from .machines import machine_catalog
lazy from .moments import when
lazy from .payload_words import value_words
lazy from .resource_operations import OperationQueued, enqueue, serialize_operation
lazy from .routes import reverse
lazy from .service_context import Cell, ServiceSection, recent_work
lazy from .timestamps import moment
lazy from .ui import counted, ended
lazy from .work_runs import recent_runs


# One catalog at a time: a process builds its commands from the catalog
# adopted now, and builds them anew once another is.
@lru_cache(maxsize=1)
def _registry(generation: str) -> OperationRegistry:
    return OperationRegistry(automation_adoption.manifests(generation))


def operation_registry(generation: str | None = None) -> OperationRegistry:
    """Every procedure of the adopted catalog, by id, built once per adoption.

    ``generation`` is the catalog's digest (``automation_adoption.generation``),
    read now when not given.
    """

    return _registry(automation_adoption.generation() if generation is None else generation)


def clear_operation_registry_cache() -> None:
    _registry.cache_clear()


def capability_name(operation_id: str) -> str:
    """The command a procedure is run through, which a grant or a rule names it by."""

    return f"automation.{operation_id}.run"


def run_title(operation_id: str) -> str:
    """A procedure's run in words, as its command's button and a list of work say it."""

    return f"{OperationRequest.Action.RUN.label} {operation_id}"


def resource_key(operation_id: str) -> str:
    """The key of a procedure's record: its id in a key's spelling. A dotted
    name's segments hold no hyphen, so no two ids share a key."""

    return f"automation-{operation_id.replace('.', '-')}"


def _host(name: str) -> str:
    host = normalized_hostname(name)
    if not is_hostname(host):
        raise ValueError("Name a machine by its host name, such as app.example.com.")
    return host


type Hosts = Annotated[
    tuple[Annotated[StrictStr, Field(min_length=1, max_length=253), AfterValidator(_host)], ...],
    Field(min_length=1, max_length=64),
    AfterValidator(sorted_unique),
    Names("machines"),
    Describe("The machines to run it on.", api="Each by its name in HQ."),
]
# The longest correlation id a run keeps, and the pattern of one: printable, no spaces.
CORRELATION_LONGEST = 200
CORRELATION_PATTERN = r"^[!-~]*$"
type Correlation = Annotated[
    StrictStr,
    Field(max_length=CORRELATION_LONGEST, pattern=CORRELATION_PATTERN),
    Describe(
        f"Your own id to find this run by later, such as an investigation's. Up to {CORRELATION_LONGEST} "
        "printable characters, no spaces. Kept with the run."
    ),
]
type Reason = Annotated[StrictStr, Field(max_length=300), Describe("Why it is run, kept with the operation it queues.")]


class RunCommand(BaseModel):
    """What every run takes beside its playbook's own parameters.

    Closed like every command, so a playbook path, an inventory, extra-vars or
    another path sent beside the parameters is refused, and the playbook's
    input type refuses one sent inside them (``automation_manifest._closed``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hosts: Hosts
    # Each procedure's own input type, which ``command_type`` declares.
    parameters: BaseModel
    correlation: Correlation = ""
    reason: Reason = ""


def command_type(manifest: OperationManifest) -> type[RunCommand]:
    """The command one procedure is run with: the run's own fields, its
    parameters as its manifest's input type, and the retry key of a change."""

    parameters = manifest.input_type
    takes_none = not any(field.is_required() for field in parameters.model_fields.values())
    fields: dict[str, Any] = {}
    if manifest.hosts or MACHINE_KIND not in manifest.target_kinds:
        # What ``resolved_hosts`` admits, offered as the only choices.
        admitted = manifest.hosts if MACHINE_KIND in manifest.target_kinds else ()
        fields["hosts"] = (Annotated[Hosts, Among(admitted)], ...)
    fields |= {
        "parameters": (
            Annotated[parameters, Describe("The playbook's own inputs, as its manifest lists them.")],
            Field(default_factory=parameters) if takes_none else ...,
        ),
    }
    if manifest.effect != "read":
        # Filled by the capability runner with the caller's key, or a fresh one.
        fields[IDEMPOTENCY_FIELD] = (StrictStr, ...)
    model: type[RunCommand] = create_model(
        f"Run{''.join(part.title() for part in manifest.id.replace('.', '_').split('_'))}",
        __base__=RunCommand,
        __module__=__name__,
        **fields,
    )
    return model


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def pinned(manifest: OperationManifest) -> dict[str, Any]:
    """The manifest as its record's spec holds it: what a run binds to.

    Every field but the input type, which its schema's digest stands for, so a
    new revision or a changed input moves the spec and with it what a held run
    is measured against.
    """

    schema = _canonical(json_schema(manifest.input_type))
    return validate_spec(
        OPERATION_KIND,
        {
            **manifest.model_dump(mode="json", exclude={"id", "input_type", "privilege"}),
            "operation": manifest.id,
            "privilege": manifest.privilege or "",
            "input_schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
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
        "parameters": command.parameters.model_dump(mode="json"),
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


def _record(manifest: OperationManifest, *, principal: Principal) -> ManagedResource:
    """The procedure's record, written as its manifest now says.

    Written here, when a run is asked for, rather than when the registry is
    composed: composing happens in every process and before any database is
    migrated, with nobody to attribute a write to, while a request has a
    transaction, an actor and an audit binding. An id nobody runs needs no
    record, and each ask pins the record to the manifest it was asked of, so
    the record says what its latest run bound to.
    """

    key = resource_key(manifest.id)
    current = ManagedResource.objects.select_for_update().filter(key=key).first()
    spec = pinned(manifest)
    if current is None or current.kind != OPERATION_KIND or current.spec != spec:
        save_managed_resource(
            ManagedResourceCommand(
                key=key, kind=OPERATION_KIND, spec=spec, enabled=current.enabled if current else True
            ),
            principal=principal,
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
    link: str = "See that run on Jobs"


def held_words(details: object) -> HeldWords | None:
    """What a page says of the run a refusal names in ``details`` (``_held_by``):
    which playbook it runs, on which machines, in what state and since when,
    and whether this playbook or a machine is what it holds. None for a
    refusal that names no run in the way."""

    if not isinstance(details, dict) or not isinstance(details.get("holding"), dict):
        return None
    holding = details["holding"]
    playbook = str(details.get("playbook") or "")
    titles = {procedure.id: procedure.title for procedure in automation_adoption.procedures()}
    title = titles.get(playbook) or run_title(playbook)
    state = OperationRequest.State(holding["state"])
    asked_at = moment(holding.get("created_at"))
    since = f"{lower_first(state.as_job.label)} since {when(asked_at)}" if asked_at else lower_first(state.as_job.label)
    machines = [str(host) for host in details.get("machines") or ()]
    shared = [str(host) for host in details.get("shared") or ()]
    if shared:
        busy = f"{get_text_list(shared, 'and')} {'is' if len(shared) == 1 else 'are'} busy with a run of “{title}”"
        sentence = f"{ended(f'{busy}, {since}')} A machine takes one run at a time, so ask again once that run ends."
    else:
        held = f"“{title}” already has a run on {get_text_list(machines, 'and')}, {since}"
        sentence = f"{ended(held)} A playbook runs once at a time, so ask again once that run ends."
    asked = {"kind": capability_name(playbook), "state": state.as_job.value}
    href = f"{reverse('jobs:list')}?{urlencode({**asked, **({'host': (shared or machines)[0]} if machines else {})})}"
    return HeldWords(sentence, href)


@transaction.atomic
def request_run(
    manifest: OperationManifest, command: RunCommand, *, principal: Principal, idempotency_key: str
) -> OperationQueued:
    """Queue one run of ``manifest`` for the automation worker, once per retry key."""

    bound = _bound(manifest, command)
    asked = {
        **bound,
        **({"approved_by": principal.approved_by} if principal.approved_by else {}),
    }
    existing = OperationRequest.objects.filter(idempotency_key=idempotency_key).select_related("resource").first()
    if existing is not None:
        kept = {name: existing.input.get(name) for name in bound}
        if (
            existing.resource.key != resource_key(manifest.id)
            or existing.action != OperationRequest.Action.RUN
            or (kept != bound)
        ):
            raise IdempotencyConflict("That retry key was already used for a different request.")
        return {"ok": True, "queued": False, "operation": serialize_operation(existing)}
    with audit_operation(operation="automation.run.request", principal=principal):
        resource = _record(manifest, principal=principal)
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
        # No run of this procedure is active and the key is new, so this
        # queues one.
        operation, _ = enqueue(
            resource,
            OperationRequest.Action.RUN,
            idempotency_key=idempotency_key,
            requested_actor=principal.actor,
            requested_interface=principal.interface,
            requested_device=principal.device,
            requested_person_key=principal.person_key,
            reason=command.reason,
            input={"generation": resource.generation, **asked},
        )
        OperationHost.objects.bulk_create(OperationHost(operation=operation, host=host) for host in bound["hosts"])
    # The worker pulls the run over the bridge; the ring only says one waits,
    # and only once it is stored for the claim to find.
    transaction.on_commit(ring_automation_doorbell)
    return {"ok": True, "queued": True, "operation": serialize_operation(operation)}


def _run(manifest: OperationManifest, command: RunCommand, *, principal: Principal) -> OperationQueued:
    # A read takes no retry key, so its run is keyed afresh: asking twice
    # queues twice, and the procedure's one active run refuses the second.
    key = getattr(command, IDEMPOTENCY_FIELD, "") or f"run:{secrets.token_urlsafe(18)}"
    return request_run(manifest, command, principal=principal, idempotency_key=key)


# What a manifest's ``retry`` lets a run that stopped without a known
# outcome do, in the words its command's page says it.
RETRY_WORDS = {
    "never": "Never: a run whose outcome is not known is not run again",
    "observe_first": "Only once the machines are looked at",
    "safe": "Safe to run again",
}


def _statements(statements: tuple[str, ...]) -> str:
    return " ".join(ended(statement) for statement in statements)


# What each input type of an argument spec takes, in a page's words.
INPUT_TYPE_WORDS = {
    "str": "Text",
    "path": "Path",
    "int": "Whole number",
    "float": "Number",
    "bool": "Yes or no",
    "list": "List",
}


def _runs_on(manifest: OperationManifest) -> CommandFact:
    """The machines a procedure runs on: each it names by its link, else any
    of the kinds it is written for."""

    if manifest.hosts:
        return CommandFact("Runs on", links=tuple(entity_link("machine", host) for host in manifest.hosts))
    return CommandFact("Runs on", f"Any {' or '.join(lower_first(kind_label(kind)) for kind in manifest.target_kinds)}")


def _type_words(spec: AdoptedInput) -> str:
    if spec.type == "list" and spec.elements is not None:
        return f"List of {lower_first(INPUT_TYPE_WORDS[spec.elements])}"
    return INPUT_TYPE_WORDS[spec.type]


def _inputs(procedure: AdoptedProcedure | None) -> ServiceSection | None:
    """A procedure's inputs as its argument spec declares them, with the bound
    its sidecar sets on a text input; None when it takes none, or when its
    manifest was supplied without the adoption that declares them."""

    if procedure is None:
        return None
    records = tuple(
        (
            Cell(name),
            Cell(_type_words(spec)),
            Cell("Yes" if spec.required else ""),
            Cell(value_words(spec.default)),
            Cell(value_words(spec.choices)),
            Cell(spec.bound.pattern if spec.bound else ""),
            Cell(counted(spec.bound.max_length, "character") if spec.bound else ""),
            Cell(spec.description),
        )
        for name, spec in procedure.inputs.items()
    )
    if not records:
        return None
    return ServiceSection(
        id="inputs",
        label="Inputs",
        columns=("Input", "Type", "Required", "Default", "Choices", "Pattern", "Longest", "Description"),
        records=records,
    )


def about(manifest: OperationManifest, generation: str, principal: Principal) -> CommandAbout:
    """What a procedure's command page says of it to ``principal``: what its
    adopted catalog declares, reviewed, the inputs its argument spec takes,
    and how its latest runs went, with their evidence where they may read Jobs."""

    facts = (
        _runs_on(manifest),
        CommandFact("Effect", effect_label(manifest.effect)),
        CommandFact("Privilege", manifest.privilege or "None"),
        CommandFact("Timeout", str(seconds(manifest.timeout_seconds))),
        CommandFact("Preconditions", _statements(manifest.preconditions)),
        CommandFact("Verification", _statements(manifest.verification)),
        CommandFact("Recovery", _statements(manifest.recovery)),
        CommandFact("Retry", RETRY_WORDS[manifest.retry]),
        CommandFact("Reviewed commit", manifest.revision),
        CommandFact("Playbook file", manifest.playbook),
    )
    every = f"{reverse('jobs:list')}?{urlencode({'kind': capability_name(manifest.id)})}"
    sections = (
        recent_work(recent_runs(manifest.id), by_machine=False, more_url=every, principal=principal),
        _inputs(next((row for row in automation_adoption.procedures(generation) if row.id == manifest.id), None)),
    )
    return CommandAbout(label="Playbook", facts=facts, sections=tuple(section for section in sections if section))


def _reviewed_notes(manifest: OperationManifest) -> tuple[str, ...]:
    """What the adopted catalog says a run checks before it works, how it
    shows it worked, and how a failed one is recovered, for an agent to read
    before it asks, as a person reads them on the command's page."""

    said = (
        ("Before it works, the playbook checks:", manifest.preconditions),
        ("It shows it worked when:", manifest.verification),
        ("If it fails:", manifest.recovery),
    )
    return tuple(f"{lead} {_statements(statements)}" for lead, statements in said if statements)


def _capability(manifest: OperationManifest, generation: str) -> CapabilitySpec:
    name = capability_name(manifest.id)
    longest = ApprovalRequest._meta.get_field("capability").max_length
    if longest is not None and len(name) > longest:
        raise ImproperlyConfigured(
            f"Operation {manifest.id!r} has an id too long to be run: {name!r} is over {longest} characters."
        )

    run = measured_by(partial(run_subject, manifest))(partial(_run, manifest))
    if manifest.effect != "read":
        # A procedure that changes a machine is a person's to approve each time
        # an agent asks; a person's own click is their consent.
        run = waits_for_a_person(every_call)(run)
    return CapabilitySpec(
        name,
        f"Run the reviewed playbook {manifest.id} on the machines named.",
        manifest.effect,
        Capability.READ if manifest.effect == "read" else Capability.MANAGE_INFRASTRUCTURE,
        command_type(manifest),
        run,
        execution_notes=(
            "Find each machine named, and refuse one the playbook is not written for.",
            "Refuse while another run waits or works on this playbook or on one of those machines.",
            "Queue it once, and HQ runs it with Ansible outside this request.",
            *_reviewed_notes(manifest),
        ),
        label=run_title(manifest.id),
        verb=OperationRequest.Action.RUN.label,
        # Read on a held run's card, where the procedure is what was asked for.
        wants=f"carry out the playbook {manifest.id}",
        about=partial(about, manifest, generation),
    )


def capability_specs(generation: str | None = None) -> tuple[CapabilitySpec, ...]:
    """The command each procedure of the adopted catalog is run through."""

    digest = automation_adoption.generation() if generation is None else generation
    return tuple(_capability(manifest, digest) for manifest in operation_registry(digest).values())
