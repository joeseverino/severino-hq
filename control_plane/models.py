"""Desired state and operation queue; credentials live only in the controller."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

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


class TopologySnapshot(TimestampedModel):
    """Trusted local cache of the authored topology SSOT."""

    id = models.CharField(primary_key=True, max_length=64, default="topology")
    schema_version = models.PositiveIntegerField()
    checksum = models.CharField(max_length=64)
    payload = models.JSONField()

    def __str__(self) -> str:
        return f"{self.id} v{self.schema_version}"


class ProviderInventory(TimestampedModel):
    """What a provider actually holds, as a controller last saw it.

    A cache, and named to stay one. HQ must not become a second copy of AdGuard
    or Nginx Proxy Manager -- those own their own state, and a stored mirror is
    wrong the moment anything changes outside HQ. Nothing reconciles from this
    and nothing is derived from it that outlives the next sweep; it exists so an
    operator can see what is out there and adopt it.

    ``observed_at`` is the point. A row here is only a claim about a moment, and
    a surface showing it has to be able to say how old that moment is.
    """

    kind = models.CharField(primary_key=True, max_length=64)
    records = models.JSONField(default=list, blank=True)
    reachable = models.BooleanField(default=True)
    error = models.CharField(max_length=500, blank=True)
    observed_at = models.DateTimeField()
    controller_id = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ("kind",)
        verbose_name_plural = "provider inventories"

    def __str__(self) -> str:
        return f"{self.kind} ({len(self.records)} records)"


class OperationRequest(TimestampedModel):
    class Action(models.TextChoices):
        RECONCILE = "reconcile", "Reconcile"
        RENEW = "renew", "Renew certificate"
        DELETE = "delete", "Delete"

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
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.QUEUED
    )
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
