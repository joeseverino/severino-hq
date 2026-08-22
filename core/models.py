"""Core models: AuditLog and shared mixins."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class TimestampedModel(models.Model):
    """Shared timestamps for create/update tracking."""

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """A single record of something a user (or the system) did."""

    class Action(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        DELETED = "deleted", "Deleted"
        LOGIN = "login", "Login"
        LOGOUT = "logout", "Logout"
        LOGIN_FAILED = "login_failed", "Login failed"
        UPLOADED = "uploaded", "Uploaded"
        EXPORTED = "exported", "Exported"
        IMPORTED = "imported", "Imported"
        FAILED = "failed", "Failed"
        SETTINGS_CHANGED = "settings_changed", "Settings changed"
        VIEWED = "viewed", "Viewed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=200, blank=True)
    # Stable application-operation identity. Domain status tables may retain
    # this value without importing or foreign-keying the host audit model, and
    # operators can follow one action across web/API/MCP adapters directly.
    operation_id = models.CharField(max_length=128, blank=True, db_index=True)
    message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("-created_at",)),
            models.Index(fields=("object_type", "object_id")),
            models.Index(fields=("action",)),
        ]

    def __str__(self) -> str:
        who = self.user.username if self.user_id else "system"
        target = f" {self.object_type}#{self.object_id}" if self.object_type else ""
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {who} {self.action}{target}"


class Pin(models.Model):
    """Something an operator wants to see first.

    Deliberately not a field on the thing pinned. A domain's declaration is
    what HQ asks the controller to make true, and an operator's preference
    about ordering is not part of that -- stored there it would bump the
    generation, queue a reconcile, and make "I look at this one most" into a
    change to the world.

    Generic on purpose: a pin is a (kind, key) pair, so services, records and
    anything else that later wants the same affordance uses this table rather
    than growing a second one shaped identically.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pins"
    )
    target_kind = models.CharField(max_length=64)
    target_key = models.CharField(max_length=255)
    # Where the operator wants it, among the others they pinned. Alphabetical
    # is an ordering nobody chose: the whole point of pinning is that these few
    # matter more than the rest, and which of them matters most is the same
    # kind of preference as pinning them at all.
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "target_kind", "target_key"), name="unique_pin"
            )
        ]
        indexes = [models.Index(fields=("user", "target_kind"))]
        # Position first, then the key, so pins that predate an ordering (all
        # of them share position 0) still come out stable rather than shuffling
        # between requests.
        ordering = ("position", "target_key")

    def __str__(self) -> str:
        return f"{self.user_id}:{self.target_kind}:{self.target_key}"
