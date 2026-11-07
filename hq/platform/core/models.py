"""Core models: AuditLog and shared mixins."""

import functools
import itertools
import re
from typing import override

from django.conf import settings
from django.db import models
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce
from django.utils import timezone
lazy from django.apps import apps
lazy from django.urls import reverse
lazy from django.utils.text import slugify

from hq.platform.application.roles import Role
lazy from hq.platform.application.labels import dotted, plural

from .rules import Rule, one_of, singleton
lazy from .audit import audited_labels


@functools.cache
def _model_labels() -> dict[str, str]:
    return {model.__name__: _sentence(str(model._meta.verbose_name)) for model in reversed(apps.get_models())}


@functools.cache
def _model_plurals() -> dict[str, str]:
    """The plural a model declares, by its class name and by its audit label.

    Only a declared one: Django's default is the singular with an "s", which
    is the guess ``labels.plural`` exists to replace.
    """

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


def free_slug(record: models.Model, text: str, fallback: str) -> str:
    """The first slug no other row of ``record``'s model holds: ``text``
    slugified (``fallback`` when nothing of it survives), then ``-2``, ``-3``.

    One query for every slug that could collide, so a crowded name costs the
    same as a fresh one; the column's uniqueness still decides a race.
    """

    length = type(record)._meta.get_field("slug").max_length or 50
    base = (slugify(text) or fallback)[: length - 4].rstrip("-") or fallback
    taken = set(
        type(record)._default_manager.filter(slug__startswith=base).exclude(pk=record.pk).values_list("slug", flat=True)
    )
    if base not in taken:
        return base
    return next(slug for n in itertools.count(2) if (slug := f"{base}-{n}") not in taken)


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
        # A run of work that finished and changed something: its outcome.
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        SETTINGS_CHANGED = "settings_changed", "Settings changed"
        VIEWED = "viewed", "Viewed"
        DENIED = "denied", "Denied"
        # HQ looking through a connection: a probe or a routine read.
        OBSERVED = "observed", "Checked"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField("object ID", max_length=64, blank=True)
    object_repr = models.CharField(max_length=200, blank=True)
    # Stable application-operation identity. Domain status tables may retain
    # this value without importing or foreign-keying the host audit model, and
    # operators can follow one action across web/API/MCP adapters directly.
    operation_id = models.CharField("operation ID", max_length=128, blank=True, db_index=True)
    # The connection the work went through, by connection_ref, or blank.
    connection = models.CharField(max_length=160, blank=True)
    message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    # Who acted and through which door, read out of the metadata `record_event`
    # wrote, so a deleted user's rows still say who it was and a read by actor
    # is served from an index. Blank when the metadata names none: a column
    # that may be empty would be added in place, which SQLite refuses for a
    # stored one, where a blank one is added by rebuilding the table.
    actor = models.GeneratedField(
        expression=Coalesce(KeyTextTransform("actor", "metadata"), models.Value("", output_field=models.TextField())),
        output_field=models.CharField(max_length=200),
        db_persist=True,
    )
    interface = models.GeneratedField(
        expression=Coalesce(
            KeyTextTransform("interface", "metadata"), models.Value("", output_field=models.TextField())
        ),
        output_field=models.CharField(max_length=32),
        db_persist=True,
    )
    # The tailnet device an agent's call came from, by Tailscale's stable id
    # (``AgentDevice.node_id``); blank where HQ could not place the call.
    device = models.GeneratedField(
        expression=Coalesce(KeyTextTransform("device", "metadata"), models.Value("", output_field=models.TextField())),
        output_field=models.CharField(max_length=64),
        db_persist=True,
    )
    # The key of the record the row is about (``register_audit``'s
    # ``subject``), so one indexed read is that record's story; blank for a row
    # about no record, and for every row written before rows named one.
    subject = models.GeneratedField(
        expression=Coalesce(KeyTextTransform("subject", "metadata"), models.Value("", output_field=models.TextField())),
        output_field=models.CharField(max_length=200),
        db_persist=True,
    )
    # The operation that started this row's operation (``OperationContext.cause``),
    # so a chain of operations is followed from either end; blank for one that
    # started itself.
    cause = models.GeneratedField(
        expression=Coalesce(KeyTextTransform("cause", "metadata"), models.Value("", output_field=models.TextField())),
        output_field=models.CharField(max_length=128),
        db_persist=True,
    )
    # Defaults to now, and a seed or test may pass the moment it records.
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("-created_at",)),
            models.Index(fields=("object_type", "object_id")),
            models.Index(fields=("action",)),
            models.Index(fields=("connection", "-created_at")),
            models.Index(fields=("actor", "-created_at")),
            models.Index(fields=("device", "-created_at")),
            models.Index(fields=("subject", "created_at")),
            models.Index(fields=("cause",)),
        ]

    @override
    def __str__(self) -> str:
        who = self.actor_label
        subject = self.object_repr or self.type_label
        target = f" {subject}" if subject else ""
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {who} {self.action}{target}"

    def get_absolute_url(self) -> str:
        return reverse("core:audit_detail", kwargs={"pk": self.pk})

    @property
    def type_label(self) -> str:
        return object_type_label(self.object_type)

    @property
    def actor_label(self) -> str:
        """The operator, else the recorded machine actor, else an unauthenticated source."""

        if self.user_id:
            return self.user.get_username()
        metadata = self.metadata or {}
        if actor := metadata.get("actor"):
            return str(actor)
        if source := metadata.get("source"):
            return dotted("unauthenticated", source)
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
        return dotted(self.type_label, message)


class Pin(models.Model):
    """Something an operator wants to see first.

    Not a field on the thing pinned: a declaration is what HQ asks the
    controller to make true, and stored there a preference about ordering would
    bump the generation and queue a reconcile. A pin is a generic (kind, key)
    pair, so anything that wants the same affordance uses this table.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pins")
    target_kind = models.CharField(max_length=64)
    target_key = models.CharField(max_length=255)
    # Where the operator wants it, among the others they pinned. Alphabetical
    # is an ordering nobody chose: the whole point of pinning is that these few
    # matter more than the rest, and which of them matters most is the same
    # kind of preference as pinning them at all.
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("user", "target_kind", "target_key"), name="unique_pin")]
        indexes = [models.Index(fields=("user", "target_kind"))]
        # Position first, then the key, so pins that predate an ordering (all
        # of them share position 0) still come out stable rather than shuffling
        # between requests.
        ordering = ("position", "target_key")

    @override
    def __str__(self) -> str:
        return f"{self.target_key}, pinned by {self.user.get_username()}"


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

    @override
    def __str__(self) -> str:
        return "agents paused" if self.paused else "agents allowed"


class AgentSignIn(models.TextChoices):
    """How an agent's token was signed in."""

    # A client-credentials token, its subject the client itself.
    MACHINE = "machine", "As itself"
    # A token a person signed in for (an authorization code or device grant).
    PERSON = "person", "Through a person's sign-in"


class AgentIdentity(models.Model):
    """One client as one person, or a machine's client alone, that has presented
    a verified token, and its grant as of the latest one.

    The row is the unit HQ answers for (``security.agent_subject``): a rule,
    a block and a device approval are its own, never shared with another
    person who signed the same client in.
    """

    client_id = models.CharField("client ID", max_length=160)
    # The sign-in key (``linked_accounts.sign_in_subject``) of the person whose
    # sign-in its tokens come through; blank for a machine's own token.
    person_key = models.CharField(max_length=64, blank=True, default="")
    interfaces = models.JSONField(default=list)
    granted = models.JSONField(default=list)
    first_seen = models.DateTimeField(default=timezone.now, editable=False)
    last_seen = models.DateTimeField(default=timezone.now)
    # How its latest token was signed in; blank for one not seen since HQ asked.
    signs_in = models.CharField(max_length=16, choices=AgentSignIn.choices, blank=True, default="")
    # A person's stop on this one client, asked on its every call
    # (``agent_access.refusal``): blocked while ``blocked_at`` is set.
    blocked_at = models.DateTimeField(null=True, blank=True)
    blocked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # The person whose sign-in its latest token came from; none for a machine's own token.
    person = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ("client_id", "person_key")
        constraints = [
            models.UniqueConstraint(
                fields=("client_id", "person_key"), name="agent_identity_one_per_client_and_person"
            ),
            # Whoever set a block, it says when; a person deleted since leaves the block standing.
            Rule(
                condition=models.Q(blocked_by__isnull=True) | models.Q(blocked_at__isnull=False),
                name="agent_identity_block_is_dated",
                violation_error_message="A block someone set says when they set it.",
                field="blocked_at",
            ),
            Rule(
                condition=models.Q(signs_in__in=["", *AgentSignIn.values]),
                name="agent_identity_signs_in_a_known_way",
                violation_error_message=f"Must be blank or one of: {', '.join(AgentSignIn.values)}.",
                field="signs_in",
            ),
        ]

    @override
    def __str__(self) -> str:
        return self.client_id if self.person is None else f"{self.client_id} for {self.person.get_username()}"


class AgentDevice(models.Model):
    """A tailnet device one agent's client has called HQ from, for one person.

    Tailscale's word, "device": HQ's "machine" is a host it manages. Keyed by
    Tailscale's stable id, never a name or node key, which a rename or a
    re-authentication changes. A call from it does nothing until it is
    approved (``agent_devices.refusal``): the person approves their own
    client's device, and that approval is what links their HQ person to the
    device's tailnet owner.
    """

    identity = models.ForeignKey(AgentIdentity, on_delete=models.CASCADE, related_name="devices")
    # The person whose sign-in the calls came through; none for a machine's
    # own token. Deleting them deletes their approvals with them rather than
    # leaving one standing as a machine's.
    person = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    node_id = models.CharField("node ID", max_length=64)
    # As the tailnet named it at the latest call, for a person to recognize.
    name = models.CharField(max_length=200, blank=True)
    os = models.CharField(max_length=64, blank=True)
    # The tailnet login that owns the device, or blank for a tagged one.
    owner = models.CharField(max_length=200, blank=True)
    first_seen = models.DateTimeField(default=timezone.now, editable=False)
    last_seen = models.DateTimeField(default=timezone.now)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    blocked_at = models.DateTimeField(null=True, blank=True)
    blocked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ("identity", "name", "node_id")
        constraints = [
            models.UniqueConstraint(
                fields=("identity", "person", "node_id"),
                condition=models.Q(person__isnull=False),
                name="unique_agent_device_per_person",
            ),
            # A unique constraint treats every null person as distinct, so a
            # machine's own devices are held unique on their own.
            models.UniqueConstraint(
                fields=("identity", "node_id"),
                condition=models.Q(person__isnull=True),
                name="unique_agent_device_per_machine",
            ),
            Rule(
                condition=models.Q(approved_by__isnull=True) | models.Q(approved_at__isnull=False),
                name="agent_device_approval_is_dated",
                violation_error_message="An approval someone gave says when they gave it.",
                field="approved_at",
            ),
            Rule(
                condition=models.Q(blocked_by__isnull=True) | models.Q(blocked_at__isnull=False),
                name="agent_device_block_is_dated",
                violation_error_message="A block someone set says when they set it.",
                field="blocked_at",
            ),
        ]

    @override
    def __str__(self) -> str:
        return f"{self.identity} from {self.name or self.node_id}"


class OwnDevice(models.Model):
    """A client a person registered in HQ as their own device.

    One half of a device's standing: a token earns it only when it carries the
    identity provider's device permission too, and names this person as its
    subject (``tokens.token_principal``). So a grant misconfigured in the
    provider promotes no client nobody registered, and a registration grants
    nothing to a token another person signed in.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="own_devices")
    client_id = models.CharField("client ID", max_length=160)
    registered_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ("client_id",)
        constraints = [models.UniqueConstraint(fields=("user", "client_id"), name="unique_own_device_per_person")]

    @override
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

    @override
    def __str__(self) -> str:
        return f"{self.key}, seen by {self.user.get_username()}"


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
            models.UniqueConstraint(fields=("user", "provider"), name="unique_linked_account_per_provider"),
            # One account elsewhere is one person's, so a sign-in subject finds one user.
            models.UniqueConstraint(fields=("provider", "login"), name="unique_linked_account_login"),
        ]

    @override
    def __str__(self) -> str:
        return f"{self.login} on {self.provider}"


class PersonRole(models.Model):
    """The role a person holds in HQ, as their identity provider's groups resolved it.

    Written at each sign-in and renewal (``core.oidc``); a person with no row
    holds nothing (``application.roles``).
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="person_role")
    role = models.CharField(max_length=16, choices=Role.choices)
    resolved_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [one_of("role", Role, "person_role_is_a_known_role")]

    @override
    def __str__(self) -> str:
        return f"{self.user.get_username()} as {self.get_role_display()}"


class UpstreamReading(models.Model):
    """The last value read from a service outside HQ, and when it was read."""

    key = models.CharField(max_length=100, primary_key=True)
    value = models.JSONField()
    observed_at = models.DateTimeField()

    @override
    def __str__(self) -> str:
        return f"{self.key} as last read"


class IdempotencyRecord(models.Model):
    """A command's retry key and the answer it was given, so a retry replays it across restarts.

    Every transport keeps one (``hq.platform.application.idempotency``), a
    browser's command and an agent's alike, so it is platform state rather
    than the machine API's.
    """

    actor = models.CharField(max_length=255)
    actor_sha256 = models.CharField(max_length=64)
    key = models.CharField(max_length=128)
    request_sha256 = models.CharField(max_length=64)
    response = models.JSONField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        # The name hq_api's 0001 migration creates it under, so no migration renames the table.
        db_table = "hq_api_idempotencyrecord"
        constraints = [
            models.UniqueConstraint(
                fields=("actor_sha256", "key"),
                name="unique_api_idempotency_key_per_actor",
            )
        ]
        ordering = ("-created_at",)

    @override
    def __str__(self) -> str:
        return f"A retry key of {self.actor}"


class Revision(models.Model):
    """How many times one table has been written.

    The database keeps the count: a trigger on each table moves its row inside
    the writing statement's own transaction (``hq.platform.core.revisions``), so
    a count can never disagree with the rows it counts.
    """

    name = models.CharField(max_length=160, primary_key=True)
    value = models.PositiveBigIntegerField(default=0)

    @override
    def __str__(self) -> str:
        return f"{self.name}, written {self.value} times"


class Appearance(models.Model):
    """How one person wants HQ drawn. No row means follow the operating system.

    Kept on the person rather than in the session, because a session ends every
    twelve hours and a choice of theme should not quietly revert with it.
    """

    class Theme(models.TextChoices):
        SYSTEM = "system", "System"
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="appearance")
    theme = models.CharField(max_length=8, choices=Theme.choices, default=Theme.SYSTEM)
    changed_at = models.DateTimeField(default=timezone.now)

    @override
    def __str__(self) -> str:
        return f"{self.user.get_username()}'s theme"


class Avatar(models.Model):
    """A person's picture, as their identity provider last gave it.

    Kept here rather than loaded from the provider by the browser. A page that
    drew it from there would need the content policy opened to another origin,
    would show nothing whenever sign-on was unreachable, and would tell the
    provider every time somebody looked at a page.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="avatar")
    content_type = models.CharField(max_length=32)
    image = models.BinaryField()
    # What a page puts in the image's address, so a new picture is a new address
    # and the old one can be cached for as long as a browser likes.
    digest = models.CharField(max_length=64)
    source = models.CharField(max_length=500)
    fetched_at = models.DateTimeField(default=timezone.now)

    @override
    def __str__(self) -> str:
        return f"{self.user.get_username()}'s picture"
