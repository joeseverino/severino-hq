"""Core models: AuditLog and shared mixins."""

import functools
import re

from django.conf import settings
from django.db import models
from django.utils import timezone

from .rules import singleton


@functools.cache
def _model_labels() -> dict[str, str]:
    from django.apps import apps

    return {
        model.__name__: _sentence(str(model._meta.verbose_name))
        for model in reversed(apps.get_models())
    }


@functools.cache
def _model_plurals() -> dict[str, str]:
    """The plural a model declares, by its class name and by its audit label.

    Only a declared one: Django's default is the singular with an "s", which
    is the guess ``labels.plural`` exists to replace.
    """
    from django.apps import apps

    from .audit import audited_labels

    declared = {
        model: _sentence(str(model._meta.verbose_name_plural))
        for model in reversed(apps.get_models())
        if "verbose_name_plural" in model._meta.original_attrs
    }
    labels = audited_labels()
    return {
        **{model.__name__: many for model, many in declared.items()},
        **{labels[model]: many for model, many in declared.items() if model in labels},
    }


# A UUID, or a run of hex long enough to be a key or digest.
_IDENTIFIER = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{16,}\b",
    re.IGNORECASE,
)


def object_type_label(stored: str) -> str:
    """An audit row's object type as a person reads it.

    Rows store whatever their writer passed: a model's class name by default
    ("DocumentationRecord"), or a label ("Managed resource", "example.record").
    The stored value stays as written, because queries and migrations match on
    it; this is the one place it is turned into words.
    """
    if not stored:
        return ""
    label = _model_labels().get(stored)
    if label:
        return label
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stored).replace(".", " ").replace("_", " ")
    if words != stored:
        # Split from an identifier, so its later words are not proper nouns.
        words = words.lower()
    return _sentence(words)


def object_type_plural(stored: str) -> str:
    """Many of an audit row's object type: "Calendar entries", "Managed resources".

    The model's own ``verbose_name_plural`` where it declares one, and
    otherwise the label as ``labels.plural`` spells many of it. The one place
    a type's plural comes from, so no page appends an "s" to a label.
    """
    from hq.platform.application.labels import plural

    if not stored:
        return ""
    return _model_plurals().get(stored) or plural(object_type_label(stored))


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


class TimestampedModel(models.Model):
    """Shared timestamps for create/update tracking."""

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """A single record of something a user (or the system) did."""

    @property
    def type_label(self) -> str:
        return object_type_label(self.object_type)

    @property
    def type_plural(self) -> str:
        return object_type_plural(self.object_type)

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
        DENIED = "denied", "Denied"
        # HQ looking through a connection: a probe or a routine read.
        OBSERVED = "observed", "Observed"

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
    # The connection the work went through, by connection_ref, or blank.
    connection = models.CharField(max_length=160, blank=True)
    message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("-created_at",)),
            models.Index(fields=("object_type", "object_id")),
            models.Index(fields=("action",)),
            models.Index(fields=("connection", "-created_at")),
        ]

    @property
    def actor_label(self) -> str:
        """The operator, else the recorded machine actor, else an unauthenticated source."""

        if self.user_id:
            return self.user.get_username()
        metadata = self.metadata or {}
        if actor := metadata.get("actor"):
            return str(actor)
        if source := metadata.get("source"):
            return f"unauthenticated · {source}"
        return "system"

    @property
    def summary(self) -> str:
        """What happened, in a few words: the message, else action and object."""

        first = (self.message or "").strip().splitlines()
        if first:
            return first[0][:120]
        return f"{self.get_action_display()} {self.object_repr or self.type_label}".strip()

    @property
    def search_title(self) -> str:
        """The change and the object's label, never its raw identifier."""

        subject = self.object_repr or self.type_label
        return f"{self.get_action_display()} {subject}".strip() if subject else self.summary

    @property
    def search_snippet(self) -> str:
        """The object's type and the message's first line, with identifiers left out."""

        first = next(iter((self.message or "").strip().splitlines()), "")
        message = " ".join(_IDENTIFIER.sub("", first).split())[:160]
        return " · ".join(part for part in (self.type_label, message) if part)

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("core:audit_detail", kwargs={"pk": self.pk})

    def __str__(self) -> str:
        who = self.actor_label
        subject = self.object_repr or self.type_label
        target = f" {subject}" if subject else ""
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {who} {self.action}{target}"


class Pin(models.Model):
    """Something an operator wants to see first.

    Deliberately not a field on the thing pinned. A domain's declaration is
    what HQ asks the controller to make true, and an operator's preference
    about ordering is not part of that: stored there it would bump the
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


class AgentAccess(models.Model):
    """The operator's switch pausing every agent. One row at pk=1; no row means not paused."""

    paused = models.BooleanField(default=False)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [singleton("agent_access_is_one_row")]

    def __str__(self) -> str:
        return "agents paused" if self.paused else "agents allowed"


class AgentIdentity(models.Model):
    """An identity that has presented a verified token, and its grant as of the latest one."""

    client_id = models.CharField(max_length=160, unique=True)
    interfaces = models.JSONField(default=list)
    granted = models.JSONField(default=list)
    first_seen = models.DateTimeField(default=timezone.now, editable=False)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("client_id",)

    def __str__(self) -> str:
        return self.client_id


class ActionItemRead(models.Model):
    """An action item a person has seen: which item, and which revision of it."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=200)
    revision = models.CharField(max_length=16, default="")
    read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("user", "key"), name="unique_action_item_read")]


class LinkedAccount(models.Model):
    """An account elsewhere that a person's sign-in says is theirs.

    Written from a verified ID token at each sign-in (``application.linked_accounts``),
    so it is what the identity provider asserts: a statement, not proof of
    ownership. Something else HQ reads can confirm it.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="linked_accounts")
    provider = models.CharField(max_length=40)
    login = models.CharField(max_length=100)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("user", "provider"), name="unique_linked_account_per_provider")
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.login}"


class UpstreamReading(models.Model):
    """The last value read from a service outside HQ, and when it was read."""

    key = models.CharField(max_length=100, primary_key=True)
    value = models.JSONField()
    observed_at = models.DateTimeField()


class Revision(models.Model):
    """How many times one table has been written.

    The database keeps the count: a trigger on each table moves its row inside
    the writing statement's own transaction (``hq.platform.core.revisions``), so
    a count can never disagree with the rows it counts.
    """

    name = models.CharField(max_length=160, primary_key=True)
    value = models.PositiveBigIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.name}@{self.value}"


class Appearance(models.Model):
    """How one person wants HQ drawn. No row means follow the operating system.

    Kept on the person rather than in the session, because a session ends every
    twelve hours and a choice of theme should not quietly revert with it.
    """

    class Theme(models.TextChoices):
        SYSTEM = "system", "System"
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="appearance"
    )
    theme = models.CharField(max_length=8, choices=Theme.choices, default=Theme.SYSTEM)
    changed_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.user_id}:{self.theme}"


class Avatar(models.Model):
    """A person's picture, as their identity provider last gave it.

    Kept here rather than loaded from the provider by the browser. A page that
    drew it from there would need the content policy opened to another origin,
    would show nothing whenever sign-on was unreachable, and would tell the
    provider every time somebody looked at a page.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="avatar"
    )
    content_type = models.CharField(max_length=32)
    image = models.BinaryField()
    # What a page puts in the image's address, so a new picture is a new address
    # and the old one can be cached for as long as a browser likes.
    digest = models.CharField(max_length=64)
    source = models.CharField(max_length=500)
    fetched_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.user_id}:{self.digest[:12]}"
