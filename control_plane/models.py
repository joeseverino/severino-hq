"""Desired state and operation queue; credentials live only in the controller."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from control_plane.provider_adapters.contracts import REFUSALS
from core.models import TimestampedModel


class ManagedResource(TimestampedModel):
    """Desired state HQ authors, and the last thing a controller observed of it.

    There is no field recording who declared this. There was one, distinguishing
    a resource materialised from the topology document from one entered by hand,
    and it stopped meaning anything the moment HQ became the only author. A
    column with one reachable value is a question the model appears to answer
    and does not.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
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

    class Meta:
        ordering = ("kind", "key")
        indexes = [
            models.Index(fields=("kind", "enabled")),
            models.Index(fields=("last_observed_at",)),
        ]

    def __str__(self) -> str:
        return self.key

    def get_absolute_url(self) -> str:
        from .providers import resource_home

        return resource_home(self)

    @property
    def kind_label(self) -> str:
        from .providers import registry_label

        return registry_label(self.kind)

    @property
    def search_summary(self) -> str:
        """The provider's readout as one line, else the kind's label."""

        from .providers import readout_rows

        rows = " · ".join(
            f"{label}: {desired or observed}"
            for label, desired, observed in readout_rows(self)
            if desired or observed
        )
        return rows or self.kind_label


class ProviderInventory(TimestampedModel):
    """What a provider actually holds, as a controller last saw it.

    A cache, and named to stay one. HQ must not become a second copy of AdGuard
    or Nginx Proxy Manager: those own their own state, and a stored mirror is
    wrong the moment anything changes outside HQ. Nothing reconciles from this
    and nothing is derived from it that outlives the next sweep; it exists so an
    operator can see what is out there and adopt it.

    ``observed_at`` is the point. A row here is only a claim about a moment, and
    a surface showing it has to be able to say how old that moment is.
    """

    kind = models.CharField(primary_key=True, max_length=64)
    records = models.JSONField(default=list, blank=True)
    reachable = models.BooleanField(default=True)
    # False when the controller holds no connection that could read the kind.
    connected = models.BooleanField(default=True)
    error = models.CharField(max_length=500, blank=True)
    # What the provider refused when unreachable: the credential, or one
    # permission. Blank when the read failed for another reason.
    refusal = models.CharField(
        max_length=16, blank=True, choices=[(value, value) for value in REFUSALS]
    )
    observed_at = models.DateTimeField()
    controller_id = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ("kind",)
        verbose_name_plural = "provider inventories"

    def __str__(self) -> str:
        return f"{self.kind} ({len(self.records)} records)"


class ProviderConnection(TimestampedModel):
    """One endpoint a controller can reach, as that controller last found it.

    HQ holds no credential and never will. What it holds is the fact that one
    exists, what kind of thing it opens, and what that thing said when asked,
    which is enough to offer it as an answer and to say when it stopped working.

    The credential itself is a 1Password item, and that item is the only place a
    connection is created. Everything here is downstream of it: the controller
    renders the vault into its own environment, reads back what it was given,
    and reports that. So a page listing connections cannot drift from the vault,
    because it was never a second copy of it.

    ``reaches`` is what the credential can act on: the machines behind a
    Portainer, the zones a DNS token may edit. It is why this is worth sweeping
    rather than merely declaring: nothing in HQ can know it, and every menu that
    asks "which machine" or "which domain" should be offering exactly this.

    Keyed by controller as well as by ref, because two controllers render two
    vaults and a ref means what its own vault says it means.
    """

    connection_ref = models.CharField(max_length=160)
    controller_id = models.CharField(max_length=160, blank=True)
    provider = models.CharField(max_length=64, blank=True)
    endpoint = models.CharField(max_length=500, blank=True)
    reaches = models.JSONField(default=list, blank=True)
    reachable = models.BooleanField(default=True)
    # Whether anything was asked at all. An unprobed connection is not a broken
    # one, and showing the two the same way would make a working SSH transport
    # look like an outage every time the page loaded.
    probed = models.BooleanField(default=True)
    detail = models.CharField(max_length=500, blank=True)
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
    observed_at = models.DateTimeField()

    class Meta:
        ordering = ("provider", "connection_ref")
        constraints = [
            models.UniqueConstraint(
                fields=("controller_id", "connection_ref"),
                name="one_row_per_connection_per_controller",
            )
        ]

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
        constraints = [
            models.UniqueConstraint(
                fields=("kind", "token"), name="one_not_managed_row_per_record"
            )
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.label or self.token}"


class DashboardRefreshRequest(TimestampedModel):
    """A credential-free request the controller may claim by panel id."""

    panel_id = models.SlugField(max_length=80, unique=True)
    requested_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)


class DashboardConfiguration(TimestampedModel):
    """Operator-managed sources for the shared dashboard glance."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    infrastructure_label = models.CharField(max_length=40, default="Homelab")
    weather_point = models.CharField(max_length=64, blank=True)
    weather_label = models.CharField(max_length=40, default="Weather")


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


class WeatherObservation(TimestampedModel):
    """The last explicitly refreshed NWS reading for one configured point."""

    point = models.CharField(max_length=64, unique=True)
    payload = models.JSONField(default=dict)
    observed_at = models.DateTimeField()


class CertificateMaterial(TimestampedModel):
    """A certificate an operator generated elsewhere, held so it can be reused.

    The one secret HQ keeps on purpose. An internally signed certificate is
    produced on an air-gapped machine and has to reach a proxy somehow; without
    this, that is a copy and paste into a provider's web form, and installing it
    somewhere else later means another trip to the offline CA.

    Sealed with a key that is not in this database (see ``core.secrets``) and
    never returned by any serializer. The controller reads it through its own
    bridge command, so it does not ride along in the contract that every other
    resource's export prints.
    """

    resource = models.OneToOneField(
        ManagedResource, on_delete=models.CASCADE, related_name="material"
    )
    sealed_fullchain = models.TextField()
    sealed_private_key = models.TextField()
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

    def __str__(self) -> str:
        return f"material for {self.resource_id}"


class OperationRequest(TimestampedModel):
    class Action(models.TextChoices):
        RECONCILE = "reconcile", "Reconcile"
        RENEW = "renew", "Renew certificate"
        DELETE = "delete", "Delete"
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
        APPROVE_ROUTES = "approve-routes", "Approve advertised routes"

    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        CLAIMED = "claimed", "Claimed"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(
        ManagedResource, on_delete=models.PROTECT, related_name="operations"
    )
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
    reason = models.CharField(max_length=300, blank=True)
    idempotency_key = models.CharField(max_length=200, unique=True)
    input = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    claimed_by = models.CharField(max_length=160, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
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
            )
        ]

    def __str__(self) -> str:
        return f"{self.resource.key}: {self.action} ({self.state})"


class ApprovalRequest(TimestampedModel):
    """A change something other than a person asked for, held until one agrees.

    Written after a single service token, held on a laptop, changed the whole
    estate's access policy twice inside a minute: once to amend the declaration
    and once to push it, with nothing in between that a human had to see. Both
    calls were authorized. Authority was never the missing thing: consent was.

    So this is not a second permission system. The caller already held the
    capability; what it did not hold was a person's agreement, and that is the
    only thing stored here. The requested call is kept verbatim, in
    ``capability``, ``target`` and ``payload``, and is replayed unchanged when
    somebody approves it. Nothing about the estate moves in the meantime: no
    declaration is written, no operation is queued, so there is nothing for a
    controller to find and apply. An unapproved request is inert by
    construction rather than by a filter somebody has to remember to write.

    ``content_fingerprint`` is what makes an approval an approval *of
    something*. It covers the requested call and the state it was measured
    against, so a declaration that moves between the request and the decision
    invalidates the approval instead of quietly widening it. A person approves
    the diff they were shown, and only that diff.

    ``expires_at`` exists because a request nobody ever answers is not a
    pending decision, it is litter, and litter that still applies a change if
    it is clicked six weeks later.
    """

    class State(models.TextChoices):
        PENDING = "pending", "Waiting for a person"
        APPROVED = "approved", "Approved and applied"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Lapsed unanswered"
        # Decided by nobody: the thing it described changed underneath it, so
        # the approval that was asked for no longer covers what would happen.
        STALE = "stale", "Superseded by a later change"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
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
    content_fingerprint = models.CharField(max_length=64)
    requested_actor = models.CharField(max_length=160)
    requested_interface = models.CharField(max_length=32)
    reason = models.CharField(max_length=300, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
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
            # One row per identical outstanding request. A caller that asks the
            # same thing fifty times is told about the one request fifty times;
            # it does not fill a person's queue with fifty decisions that are
            # all the same decision.
            models.UniqueConstraint(
                fields=("capability", "target", "content_fingerprint"),
                condition=models.Q(state="pending"),
                name="one_pending_approval_per_requested_change",
            )
        ]

    def __str__(self) -> str:
        subject = self.target or self.resource_key
        return f"{self.capability}{f' on {subject}' if subject else ''} by {self.requested_actor}"


class AddressReading(models.Model):
    """What the public registries last said about one address.

    Kept because the answer barely moves. An allocation changes when a block is
    transferred between organisations; a PTR record changes when someone
    reconfigures a network. Neither happens between two page loads, and
    re-asking on every load spends a stranger's rate limit to be told the same
    thing, and tells them, each time, which addresses HQ is interested in.

    A cache rather than declared state: nothing here is HQ's to be right about,
    the row is disposable, and an operator who thinks it has gone stale can ask
    again. Stored by address so the connection panel and the tools page share
    one answer rather than each keeping their own.
    """

    address = models.GenericIPAddressField(primary_key=True)
    reading = models.JSONField(default=dict)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-observed_at",)

    def __str__(self) -> str:
        return f"{self.address} ({self.observed_at:%Y-%m-%d})"


class CapabilityRule(models.Model):
    """An operator's rule for one capability, bound to a surface or an agent.

    No row means the default. Where both apply, the stricter rule wins.
    """

    class Scope(models.TextChoices):
        SURFACE = "surface", "Surface"
        AGENT = "agent", "Agent"

    class Rule(models.TextChoices):
        ALLOW = "allow", "Allow"
        APPROVE = "approve", "Require approval"
        DENY = "deny", "Deny"

    scope = models.CharField(max_length=16, choices=Scope.choices)
    subject = models.CharField(max_length=160)
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
        constraints = [
            models.UniqueConstraint(
                fields=("scope", "subject", "capability"), name="unique_capability_rule"
            )
        ]
        ordering = ("scope", "subject", "capability")

    def __str__(self) -> str:
        return f"{self.scope}:{self.subject}:{self.capability}={self.rule}"
