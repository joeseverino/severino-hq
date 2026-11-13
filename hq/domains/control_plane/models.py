"""Desired state and operation queue; credentials live only in the controller."""

import uuid
from datetime import datetime
from typing import override

from django.conf import settings
from django.db import models
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce, Lower, NullIf, Trim
from django.utils import timezone

from hq.domains.jobs.models import JobState
from hq.platform.application.labels import dotted
from hq.platform.core.models import TimestampedModel
from hq.platform.core.rules import Rule, one_of, singleton
lazy from hq.domains.control_plane.provider_adapters.contracts import FAILURES, REFUSALS
lazy from hq.platform.application.approval_review import title as approval_title
lazy from hq.platform.application.capabilities import capability_title
lazy from hq.platform.application.security import subject_parts

lazy from .providers import readout_rows, registry_label, resource_home


# Choices as callables, which Django reads on first use: the contracts live in
# the provider package, whose import loads every provider. Migrations name
# them, so they keep their names.
def refusal_choices() -> list[tuple[str, str]]:
    return [(value, value) for value in REFUSALS]


def failure_choices() -> list[tuple[str, str]]:
    return [(value, value) for value in FAILURES]


def _named(key: str) -> models.Expression:
    """The text a declaration's ``spec`` holds under ``key``, or "" when none.

    ``KT`` spells a JSON null as the text ``null``; that is no name either.
    """

    return Coalesce(
        NullIf(KT(f"spec__{key}"), models.Value("null")),
        models.Value(""),
        output_field=models.TextField(),
    )


# The keys of ``spec`` that point one declaration at another thing, each a
# column of the same name the database derives. They mean the same thing in
# every kind that has them: the domain a record is in, and the connection a
# record is read and changed through.
REFERENCE_COLUMNS = ("zone", "connection_ref")


class ManagedResource(TimestampedModel):
    """Desired state HQ authors, and the last thing a controller observed of it.

    There is no field recording who declared this: HQ is the only author, and a
    column with one reachable value is a question the model appears to answer
    and does not.
    """

    id = models.UUIDField("ID", primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=180, unique=True)
    kind = models.CharField(max_length=64)
    spec = models.JSONField(default=dict)
    enabled = models.BooleanField(default=True)
    desired_fingerprint = models.CharField(max_length=64, blank=True, default="")
    generation = models.PositiveIntegerField(default=1)
    observed_generation = models.PositiveIntegerField(default=0)
    status = models.JSONField(default=dict, blank=True)
    conditions = models.JSONField(default=list, blank=True)
    last_observed_at = models.DateTimeField(null=True, blank=True)
    # What this declaration points at, derived by the database from ``spec``
    # (``REFERENCE_COLUMNS``): a link to filter and join on that cannot differ
    # from the spec it is read from. Nothing refuses a row whose link names
    # nothing, because a declaration may name what HQ only reads; the
    # ``names-nothing`` finding reports one that names nothing at all.
    #
    # The domain is one spelling of a DNS name, as ``names.normalized_hostname``
    # gives it: lowercase, trimmed, no trailing dot.
    zone = models.GeneratedField(
        expression=models.Func(
            Lower(Trim(_named("zone"))),
            models.Value("."),
            function="RTRIM",
            output_field=models.TextField(),
        ),
        output_field=models.CharField(max_length=253),
        db_persist=True,
    )
    connection_ref = models.GeneratedField(
        expression=Trim(_named("connection_ref")),
        output_field=models.CharField(max_length=160),
        db_persist=True,
    )

    class Meta:
        ordering = ("kind", "key")
        indexes = [
            models.Index(fields=("kind", "enabled")),
            models.Index(fields=("last_observed_at",)),
            models.Index(fields=("zone",), name="resource_zone"),
            models.Index(fields=("connection_ref",), name="resource_connection_ref"),
        ]
        constraints = [
            Rule(
                condition=models.Q(generation__gte=1),
                name="resource_generation_starts_at_one",
                violation_error_message="A generation starts at 1.",
                field="generation",
            ),
            Rule(
                condition=models.Q(observed_generation__lte=models.F("generation")),
                name="resource_observed_no_later_than_declared",
                violation_error_message="A resource is observed at a generation it has reached.",
                field="observed_generation",
            ),
        ]

    @override
    def __str__(self) -> str:
        return self.key

    def get_absolute_url(self) -> str:
        return resource_home(self)

    @property
    def kind_label(self) -> str:
        return registry_label(self.kind)

    @property
    def search_summary(self) -> str:
        """The provider's readout as one line, else the kind's label."""

        rows = dotted(
            *(
                f"{label}: {desired or observed}"
                for label, desired, observed in readout_rows(self)
                if desired or observed
            )
        )
        return rows or self.kind_label


def attempted_at(observed_at: datetime | None, updated_at: datetime) -> datetime:
    """When a stored reading was last tried, from its two columns.

    A failed read stores nothing new, so its try is ``updated_at``; a row no
    read has answered has no ``observed_at``, and its last try stands alone.
    Taken from the columns rather than a row, for the readers that ask
    ``values_list`` for them.
    """

    return updated_at if observed_at is None else max(observed_at, updated_at)


class ProviderInventory(TimestampedModel):
    """What a provider actually holds, as a controller last saw it.

    A cache, and named to stay one: AdGuard or Nginx Proxy Manager own their
    state, so nothing reconciles from this or derives anything from it that
    outlives the next sweep. It exists so an operator can see what is out there
    and adopt it.
    """

    kind = models.CharField(primary_key=True, max_length=64)
    records = models.JSONField(default=list, blank=True)
    reachable = models.BooleanField(default=True)
    # False when the controller holds no connection that could read the kind.
    connected = models.BooleanField(default=True)
    error = models.CharField(max_length=500, blank=True)
    # What the provider refused when unreachable: the credential, or one
    # permission. Blank when the read failed for another reason.
    refusal = models.CharField(max_length=16, blank=True, choices=refusal_choices)
    # Parts of the kind the last read could not read while the rest read, as
    # ``control_plane.reading_parts.clean_refused_parts`` stores them.
    refused_parts = models.JSONField(default=list, blank=True)
    # A row is only a claim about a moment, and a surface showing it has to be
    # able to say how old that moment is: the last read that answered, or none
    # when no read has.
    observed_at = models.DateTimeField(null=True, blank=True)
    controller_id = models.CharField("controller ID", max_length=160, blank=True)

    class Meta:
        ordering = ("kind",)
        verbose_name_plural = "provider inventories"

    @override
    def __str__(self) -> str:
        return f"{registry_label(self.kind)}, as its provider holds them"


class ProviderConnection(TimestampedModel):
    """One endpoint a controller can reach, as that controller last found it.

    HQ holds no credential: the 1Password item is where a connection is made,
    and this row is what the controller reports of it (that it exists, what it
    opens, what it said when asked), so a page of connections cannot drift from
    the vault.
    """

    connection_ref = models.CharField(max_length=160)
    controller_id = models.CharField("controller ID", max_length=160, blank=True)
    provider = models.CharField(max_length=64, blank=True)
    endpoint = models.CharField(max_length=500, blank=True)
    # What the credential can act on (the machines behind a Portainer, the zones a
    # DNS token may edit), which nothing else in HQ can know: every menu that asks
    # "which machine" or "which domain" offers this.
    reaches = models.JSONField(default=list, blank=True)
    reachable = models.BooleanField(default=True)
    # Whether anything was asked at all. An unprobed connection is not a broken
    # one, and showing the two the same way would make a working SSH transport
    # look like an outage every time the page loaded.
    probed = models.BooleanField(default=True)
    detail = models.CharField(max_length=500, blank=True)
    # Why the last probe failed: a refusal, the address answering as something
    # other than the API, or no answer. Blank when it answered or the cause is
    # unknown.
    failure = models.CharField(max_length=16, blank=True, choices=failure_choices)
    # Whether the connection's item declares that HQ manages through it. False
    # means it only observes, and nothing it reads is adopted.
    manages = models.BooleanField(default=False)
    # Work that went through this connection and could not finish, as the last
    # pass found it. A probe asks whether the credential still opens the door;
    # this is what happened to the things that walked through it, and the two
    # disagree exactly when something is wrong in a way a probe cannot see. A
    # connection can answer every probe and refuse every operation.
    #
    # Written by its own report at the end of a pass rather than with the probe
    # at the start, because most work happens after the sweep and a failure
    # recorded before it ran would describe the pass before.
    failing_steps = models.JSONField(default=list, blank=True)
    # When the credential stops working, where the provider says so.
    expires_at = models.DateTimeField(null=True, blank=True)
    # Where the credential is kept, as references: ``vault`` and ``item`` it
    # was rendered from, and the ``bootstrap`` item that may mint a replacement.
    # Never a value; ``application.credential_mint`` builds the fix from these.
    store = models.JSONField(default=dict, blank=True)
    # When it was last probed. An SSH connection is probed on its own slower
    # clock and carried between probes, so this can trail ``reported_at``.
    observed_at = models.DateTimeField()
    # When a controller last reported it, carried or probed.
    reported_at = models.DateTimeField(null=True, blank=True)
    # When its controller sent a report that carried no connection at all.
    # Such a report keeps the rows rather than replacing them with nothing,
    # because an emptied vault on the controller's machine looks exactly like a
    # host that never had any, and these rows are the only record that there
    # were. The next report that carries a connection replaces them as usual.
    unreported_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("provider", "connection_ref")
        constraints = [
            # Keyed by controller too: two controllers render two vaults, and a ref means
            # what its own vault says.
            models.UniqueConstraint(
                fields=("controller_id", "connection_ref"),
                name="one_row_per_connection_per_controller",
            )
        ]

    @override
    def __str__(self) -> str:
        return self.connection_ref


class NotManaged(TimestampedModel):
    """A record an operator said HQ does not manage.

    Written when a declaration HQ adopted is forgotten, and read by adoption,
    which skips the record until an operator manages it again. Keyed by kind and
    the record's token (``inventory.record_token``).
    """

    kind = models.CharField(max_length=64)
    token = models.CharField(max_length=16)
    label = models.CharField(max_length=300, blank=True)
    actor = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ("kind", "label")
        constraints = [models.UniqueConstraint(fields=("kind", "token"), name="one_not_managed_row_per_record")]

    @override
    def __str__(self) -> str:
        return f"{registry_label(self.kind)} {self.label or self.token}"


class RefusedReport(models.Model):
    """A controller call the bridge refused, kept until that call is next accepted.

    A refused report writes nothing else, so this row is the only record that a
    controller's work stopped landing (``application.refused_reports``). It
    holds what the bridge already answered the controller: the action, the
    controller the call named once its query was held to the contract, and the
    refusal's detail, struck of secrets and cut to the problem's length. Never
    the payload. One row per action and controller.
    """

    action = models.CharField(max_length=64)
    controller_id = models.CharField("controller ID", max_length=160, blank=True)
    status = models.PositiveSmallIntegerField()
    detail = models.CharField(max_length=500)
    refused_at = models.DateTimeField()

    class Meta:
        ordering = ("action", "controller_id")
        constraints = [
            models.UniqueConstraint(fields=("action", "controller_id"), name="one_refused_report_per_action_controller")
        ]

    @override
    def __str__(self) -> str:
        return f"{self.action} refused for {self.controller_id or 'a controller'}"


class DashboardRefreshRequest(TimestampedModel):
    """A credential-free request the controller may claim by panel id."""

    panel_id = models.SlugField("panel ID", max_length=80, unique=True)
    requested_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    @override
    def __str__(self) -> str:
        return f"A fresh reading of the {self.panel_id} panel"


class ReadRequest(TimestampedModel):
    """An operator asking for one connection, one kind, or everything read now.

    Blank ``connection_ref`` and ``kind`` is every connection. Answered once
    every reading it forces was stored after ``requested_at``; see
    ``application.cadence``.
    """

    connection_ref = models.CharField(max_length=160, blank=True)
    kind = models.CharField(max_length=64, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("connection_ref", "kind"), name="one_read_request_per_subject")]

    @override
    def __str__(self) -> str:
        return f"Read {self.connection_ref or self.kind or 'every connection'} now"


class DashboardConfiguration(TimestampedModel):
    """Operator-managed sources for the shared dashboard glance."""

    id = models.PositiveSmallIntegerField("ID", primary_key=True, default=1, editable=False)
    infrastructure_label = models.CharField(max_length=40, default="Homelab")
    weather_point = models.CharField(max_length=64, blank=True)
    weather_label = models.CharField(max_length=40, default="Weather")

    class Meta:
        constraints = [singleton("dashboard_configuration_is_one_row")]

    @override
    def __str__(self) -> str:
        return "Dashboard settings"


class DashboardMachine(TimestampedModel):
    """A machine selected for the dashboard, ordered independently of its state."""

    machine = models.OneToOneField(
        ManagedResource,
        on_delete=models.CASCADE,
        related_name="dashboard_placement",
    )
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("position", "pk")

    @override
    def __str__(self) -> str:
        return f"{self.machine.key} on the dashboard"


class WeatherObservation(TimestampedModel):
    """The last explicitly refreshed NWS reading for one configured point."""

    point = models.CharField(max_length=64, unique=True)
    payload = models.JSONField(default=dict)
    observed_at = models.DateTimeField()

    @override
    def __str__(self) -> str:
        return f"The weather at {self.point}"


class CertificateMaterial(TimestampedModel):
    """A certificate an operator generated elsewhere, held so it can be reused.

    The one secret HQ keeps on purpose: an internally signed certificate comes from
    an air-gapped machine, and without this reaches a proxy by copy and paste into
    a provider's web form, with another trip to the offline CA for each new place.
    """

    resource = models.OneToOneField(ManagedResource, on_delete=models.CASCADE, related_name="material")
    # The certificate and its chain, which every client of the service is sent.
    # Its private key is a SealedSecret on the same resource.
    fullchain = models.TextField()
    # Held in the clear because they are printed, not protected: an operator has
    # to see which certificate this is and when it stops working, and both are
    # readable by anyone who can already connect to the service it secures.
    fingerprint_sha256 = models.CharField(max_length=95, blank=True)
    # The names the certificate actually carries, read out of it at upload. The
    # deploy path needs them to know which proxy hosts to rebind, and the
    # service view needs them to know what this covers: neither is declared,
    # both are facts about the artifact.
    domains = models.JSONField(default=list, blank=True)
    not_after = models.DateTimeField(null=True, blank=True)
    subject = models.CharField(max_length=500, blank=True)

    @override
    def __str__(self) -> str:
        return f"The certificate for {self.resource.key}"


class SealedSecret(models.Model):
    """A secret an operator handed HQ for one resource, sealed to the controller.

    HQ holds only the controller's public key, so the web writes this and never
    reads it back: the controller is given ``sealed`` over the bridge and opens
    it itself. Sealing binds the value to its resource and slot, so a value
    copied to another row does not open. ``sealed_secrets.set_secret`` is its
    one writer.
    """

    resource = models.ForeignKey(ManagedResource, on_delete=models.CASCADE, related_name="sealed_secrets")
    # Where the value goes on the resource, such as "certificate:private_key".
    slot = models.CharField(max_length=200)
    sealed = models.TextField()
    # How many times the slot has been set, so a replacement is told apart.
    version = models.PositiveIntegerField(default=1)
    set_at = models.DateTimeField()
    set_by = models.CharField(max_length=160)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("resource", "slot"), name="one_sealed_secret_per_slot")]

    @override
    def __str__(self) -> str:
        return f"A secret for {self.resource.key}"


class OperationState(models.TextChoices):
    """An operation's state, in the words a job's is said in."""

    QUEUED = "queued", JobState.QUEUED.label
    CLAIMED = "claimed", JobState.RUNNING.label
    SUCCEEDED = "succeeded", JobState.SUCCEEDED.label
    FAILED = "failed", JobState.FAILED.label
    CANCELLED = "cancelled", JobState.LOST.label

    @property
    def as_job(self) -> JobState:
        """The job state this one reads as on Jobs, found by the word both say,
        so the label above is the one declaration of the mapping."""

        return next(state for state in JobState if state.label == self.label)


class OperationRequest(TimestampedModel):
    class Action(models.TextChoices):
        """Each action in the words of the button that asks for it, which a
        capability that queues one says too."""

        RECONCILE = "reconcile", "Apply HQ's config"
        RENEW = "renew", "Renew certificate"
        DELETE = "delete", "Remove"
        # Lifecycle, not convergence. Restarting a container does not move the
        # world toward a declaration: it is a thing an operator asks for once,
        # about something already exactly as declared, which is why none of
        # these is ever scheduled automatically.
        RESTART = "restart", "Restart"
        START = "start", "Start"
        STOP = "stop", "Stop"
        # Consent, not convergence. A route a machine advertises is inert until
        # the tailnet approves it, and approving is a decision about trusting
        # that machine to carry that traffic, so it is asked for once, by an
        # operator, about a route the machine is already offering.
        APPROVE_ROUTES = "approve-routes", "Approve routes"
        # A reviewed automation procedure, run once on the hosts it names. The
        # automation worker claims it, never the controller, which has no
        # handler for it (``provider_adapters.automation``).
        RUN = "run", "Run"
        # A read of the automation source's repository for the procedures it
        # declares, claimed by the automation worker. Asked for by a person's
        # Refresh, never on a timer.
        SCAN = "scan", "Refresh"

    # Declared above the model so a rule in ``Meta`` can name its values.
    State = OperationState

    id = models.UUIDField("ID", primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(ManagedResource, on_delete=models.PROTECT, related_name="operations")
    action = models.CharField(max_length=20, choices=Action.choices)
    state = models.CharField(max_length=20, choices=State.choices, default=State.QUEUED)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="infrastructure_operations",
    )
    requested_actor = models.CharField(max_length=160)
    requested_interface = models.CharField(max_length=32)
    # The tailnet device the asker called from (``Principal.device``), so a
    # block on that device holds what it asked for (``agent_access.held_askers``).
    requested_device = models.CharField(max_length=64, blank=True)
    # A machine has no person (""); NULL means a legacy operation whose
    # person was never recorded, so a block on its client still holds it.
    requested_person_key = models.CharField(max_length=100, blank=True, null=True)
    reason = models.CharField(max_length=300, blank=True)
    idempotency_key = models.CharField(max_length=200, unique=True)
    input = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    claimed_by = models.CharField(max_length=160, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    # The claimant's name for the claim that leased it, so a claim retried
    # with the same name is answered this operation again
    # (``controller.claim_next_operation``).
    claim_attempt = models.CharField(max_length=64, blank=True, default="")
    attempt_count = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("state", "created_at")),
            models.Index(fields=("resource", "action", "state")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("resource", "action"),
                condition=models.Q(state__in=("queued", "claimed")),
                name="one_active_operation_per_resource_action",
            ),
            one_of("state", OperationState, "operation_state_is_declared"),
            Rule(
                condition=~models.Q(state="claimed")
                | (~models.Q(claimed_by="") & models.Q(lease_expires_at__isnull=False)),
                name="operation_claimed_names_its_claim",
                violation_error_message="A claimed operation names its controller and when its claim ends.",
                field="state",
            ),
            Rule(
                condition=~models.Q(state="queued")
                | models.Q(claimed_by="", claimed_at__isnull=True, lease_expires_at__isnull=True),
                name="operation_queued_has_no_claim",
                violation_error_message="A queued operation has no claim.",
                field="state",
            ),
            Rule(
                # Finished exactly when it says when.
                condition=models.Q(state__in=("succeeded", "failed", "cancelled"), completed_at__isnull=False)
                | models.Q(state__in=("queued", "claimed"), completed_at__isnull=True),
                name="operation_finished_says_when",
                violation_error_message="An operation has a completion time exactly when it has finished.",
                field="state",
            ),
        ]

    @override
    def __str__(self) -> str:
        return f"{self.get_action_display()}: {self.resource.key}"


class OperationHost(models.Model):
    """One machine a run acts on, as a row, so "the runs on this machine" is an
    indexed query rather than a read of every run's ``input``.

    ``host`` is the machine's canonical name, as ``automation_runs.resolved_hosts``
    gives it: machines are a projection of many readings rather than rows, so
    there is no row to point at. Written with the run, in its transaction, by
    ``automation_runs.request_run``, its one writer.
    """

    operation = models.ForeignKey(OperationRequest, on_delete=models.CASCADE, related_name="hosts")
    host = models.CharField(max_length=253)

    class Meta:
        constraints = [
            # Leads with the host, so it is also the index a machine's runs are found by.
            models.UniqueConstraint(fields=("host", "operation"), name="one_row_per_run_and_host"),
        ]

    @override
    def __str__(self) -> str:
        return self.host


class SourceProposal(TimestampedModel):
    """What one scan of the automation source found: the procedures its
    repository declares at one commit, as HQ judged them.

    A proposal is never adopted and never runnable; it says what adopting
    that commit would bring. Written by ``automation_sources.propose``, its
    one writer, which ``controller._settle_scan`` calls as the scan's report
    is settled.
    """

    operation = models.OneToOneField(OperationRequest, on_delete=models.CASCADE, related_name="proposal")
    branch = models.CharField(max_length=200)
    revision = models.CharField(max_length=40)
    verified = models.BooleanField()
    verification_reason = models.CharField(max_length=40)
    committer = models.CharField(max_length=39, blank=True)
    # Each procedure as the worker read it with HQ's problems added, every
    # file with its object id, and each path Ansible reads that no setting pins.
    procedures = models.JSONField(default=list)
    files = models.JSONField(default=list)
    unpinned = models.JSONField(default=list)
    # Each machine and privilege profile the worker said it has a login for,
    # which a run is held to when it is asked (``automation_runs.reached``).
    reaches = models.JSONField(default=list)

    class Meta:
        ordering = ("-created_at",)

    @override
    def __str__(self) -> str:
        return f"{self.branch} at {self.revision[:12]}"


class AdoptedCatalog(TimestampedModel):
    """The procedures HQ runs: one commit of the automation source, adopted
    by a person from what a scan proposed.

    The newest row is the catalog every procedure's command is built from;
    each earlier one is what was adopted before it. Each row carries all it
    pins, so a pruned proposal leaves it whole. Written by
    ``automation_adoption.adopt``, its one writer.
    """

    proposal = models.ForeignKey(SourceProposal, on_delete=models.SET_NULL, null=True, related_name="adoptions")
    branch = models.CharField(max_length=200)
    revision = models.CharField(max_length=40)
    committer = models.CharField(max_length=39, blank=True)
    # Each procedure as adopted: its manifest's fields, its inputs and the
    # files it runs; and every file of the commit, which the next adoption
    # is compared against.
    procedures = models.JSONField(default=list)
    files = models.JSONField(default=list)
    # Over the revision and the procedures: what every process builds its
    # commands from, and so how it knows its own are current.
    digest = models.CharField(max_length=64)

    class Meta:
        ordering = ("-id",)

    @override
    def __str__(self) -> str:
        return f"{self.branch} at {self.revision[:12]}"


class ApprovalState(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"
    # Decided by nobody: the thing it described changed underneath it, so
    # the approval that was asked for no longer covers what would happen.
    STALE = "stale", "Superseded"


class ApprovalRequest(TimestampedModel):
    """A change something other than a person asked for, held until one agrees.

    The caller already holds the capability; what is stored is the call it made,
    replayed unchanged on approval, and nothing about the estate moves until
    then: no declaration is written and no operation queued, so an unapproved
    request is inert by construction.
    """

    # Declared above the model so a rule in ``Meta`` can name its values.
    State = ApprovalState

    id = models.UUIDField("ID", primary_key=True, default=uuid.uuid4, editable=False)
    capability = models.CharField(max_length=64)
    target = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    # The kind is why this was held at all, and the key is what it is about.
    # Stored rather than re-derived: the declaration may be gone by the time a
    # person reads the queue, and "which resource was this" still has an answer.
    resource_kind = models.CharField(max_length=64, blank=True)
    resource_key = models.CharField(max_length=180, blank=True)
    # What the requested change was measured against, as it stood when asked.
    # The diff a person is shown is rendered from this, so the page cannot show
    # one comparison and the fingerprint cover another.
    baseline = models.JSONField(default=dict, blank=True)
    # Covers the requested call and the baseline, so a declaration that moves
    # before the decision invalidates the approval instead of widening it.
    content_fingerprint = models.CharField(max_length=64)
    requested_actor = models.CharField(max_length=160)
    requested_interface = models.CharField(max_length=32)
    # The tailnet device the asker called from (``Principal.device``), so a
    # block on that device holds what it asked for (``agent_access.held_askers``).
    requested_device = models.CharField(max_length=64, blank=True)
    # The sign-in key of the person a token asked for (``Principal.person_key``),
    # so an approved personal write replays as that person's; blank for a
    # machine's token.
    requested_person_key = models.CharField(max_length=100, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    # A request nobody answers lapses, so a click weeks later applies nothing.
    expires_at = models.DateTimeField()
    # The decider is a name and an interface, not a user row. A foreign key
    # would be the obvious thing and would answer a question nobody asks: what
    # matters is that a person on the web surface agreed, and both of those
    # facts are here. ``OperationRequest.requested_by`` is the counter-example
    # sitting next door: a user column no writer has ever filled in.
    decided_actor = models.CharField(max_length=160, blank=True)
    decided_interface = models.CharField(max_length=32, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)
    # What the replayed call answered, so the queue can say what approving it
    # actually did without the reader having to go and find the operation.
    result = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("state", "expires_at")),
            models.Index(fields=("resource_key", "state")),
        ]
        constraints = [
            # One row per identical outstanding request from one caller. A
            # caller that asks the same thing fifty times is told about the one
            # request fifty times; it does not fill a person's queue with fifty
            # decisions that are all the same decision. The asker is in the key
            # because a request is its asker's: another caller asking the same
            # change gets its own, approved as itself, and a client is one asker
            # per person who signed it in.
            models.UniqueConstraint(
                fields=(
                    "capability",
                    "target",
                    "content_fingerprint",
                    "requested_actor",
                    "requested_interface",
                    "requested_person_key",
                ),
                condition=models.Q(state="pending"),
                name="one_pending_approval_per_requested_change",
            ),
            one_of("state", ApprovalState, "approval_state_is_declared"),
            Rule(
                # Pending exactly while nothing has settled it.
                condition=models.Q(state="pending", decided_at__isnull=True)
                | (~models.Q(state="pending") & models.Q(decided_at__isnull=False)),
                name="approval_settled_says_when",
                violation_error_message="A request has a decision time exactly when it is no longer pending.",
                field="state",
            ),
            Rule(
                # A person decides an approval or a rejection, and nothing else.
                condition=(models.Q(state__in=("approved", "rejected")) & ~models.Q(decided_actor=""))
                | (~models.Q(state__in=("approved", "rejected")) & models.Q(decided_actor="")),
                name="approval_decision_names_who_decided",
                violation_error_message="An approval or a rejection names who decided it, and nothing else does.",
                field="state",
            ),
        ]

    @override
    def __str__(self) -> str:
        # Who asked and who decided are the row's Who, never its title.
        return approval_title(self)


class AddressReading(models.Model):
    """What the public registries last said about one address.

    Kept because the answer barely moves (a block changes hands, a network's PTR
    is reconfigured), and re-asking on every load spends a stranger's rate limit
    and tells them which addresses HQ cares about. A disposable cache an operator
    can refresh, keyed by address so the connection panel and the tools page share
    one answer.
    """

    address = models.GenericIPAddressField(primary_key=True)
    reading = models.JSONField(default=dict)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-observed_at",)

    @override
    def __str__(self) -> str:
        return f"{self.address} ({self.observed_at:%Y-%m-%d})"


class CapabilityRule(models.Model):
    """An operator's rule for one capability and one agent, named by its subject.

    No row means the default.
    """

    class Rule(models.TextChoices):
        ALLOW = "allow", "Allow"
        APPROVE = "approve", "Require approval"
        DENY = "deny", "Deny"

    # An agent subject (``security.agent_subject``): a client id of up to 160 and its person's key.
    subject = models.CharField(max_length=255)
    capability = models.CharField(max_length=64)
    rule = models.CharField(max_length=16, choices=Rule.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("subject", "capability"), name="unique_capability_rule")]
        ordering = ("subject", "capability")

    @override
    def __str__(self) -> str:
        return f"{self.get_rule_display()} “{capability_title(self.capability)}” for {subject_parts(self.subject)[0]}"
