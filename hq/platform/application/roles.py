"""The roles a person holds in HQ, each a name and the capabilities it grants.

Declared once, for people and the agents acting for them: a person's browser
holds their role's capabilities, and a token a person signed in is bounded by
them (docs/Security.md#roles). The names are the ones the identity provider's
groups are mapped to (``SEVERINO_OIDC_ROLE_GROUPS``).
"""

from collections.abc import Iterable

from django.core.exceptions import ObjectDoesNotExist
from django.db import models, transaction
from django.utils import timezone

lazy from hq.platform.core.audit import record_event
lazy from hq.platform.core.models import AuditLog, PersonRole

lazy from .linked_accounts import SIGN_IN
lazy from .plugins import plugin_capabilities
lazy from .security import Capability, operator_capabilities

AUDIT_LABEL = "Role"


class Role(models.TextChoices):
    # Declared lowest first: a person several groups name holds the last.
    VIEWER = "viewer", "Viewer"
    OPERATOR = "operator", "Operator"


def grants(role: str) -> frozenset[Capability | str]:
    """Everything ``role`` holds; an unknown one holds nothing."""

    if role == Role.OPERATOR:
        return operator_capabilities()
    if role == Role.VIEWER:
        return viewer_capabilities()
    return frozenset()


def viewer_capabilities() -> frozenset[Capability | str]:
    """The read base: HQ's own reads and every extension's, which every agent's ceiling starts from too."""

    return frozenset({Capability.READ, *plugin_capabilities("mcp_read")})


def highest(roles: Iterable[str]) -> str:
    """The highest of ``roles``, or "" for none."""

    ranked = list(Role.values)
    held = [ranked.index(role) for role in roles if role in ranked]
    return ranked[max(held)] if held else ""


def parse(entries: Iterable[str], *, setting: str, default: str = "") -> dict[str, str]:
    """``name=role`` entries as a map, refusing a role HQ does not have.

    An entry without ``=role`` takes ``default``, and is refused when there is
    none. Read at start, so a misspelled role stops HQ rather than refusing
    whoever it names at their sign-in.
    """

    mapped: dict[str, str] = {}
    for entry in entries:
        name, _, role = (part.strip() for part in entry.partition("="))
        role = role or default
        if not name or role not in Role.values:
            raise RuntimeError(
                f"{setting} entries are name=role, each role one of {', '.join(Role.values)}: {entry!r}."
            )
        if name in mapped:
            raise RuntimeError(f"{setting} names {name!r} twice.")
        mapped[name] = role
    return mapped


def role_of(user) -> str:
    """The role ``user`` holds, or "" for none: no query when the request's user was loaded with it."""

    try:
        return user.person_role.role
    except ObjectDoesNotExist:
        return ""


def held_by(person_key: str) -> frozenset[Capability | str] | None:
    """What the person whose sign-in key is ``person_key`` holds, or None for nobody with a role.

    One query, by the subject their sign-in bound (``linked_accounts.bind_sign_in``).
    """

    role = (
        PersonRole.objects.filter(user__linked_accounts__provider=SIGN_IN, user__linked_accounts__login=person_key)
        .values_list("role", flat=True)
        .first()
    )
    return None if role is None else grants(role)


def assign(user, role: str) -> None:
    """Hold ``user`` to ``role`` from now, as resolved now; a change is audited."""

    if user.pk is None:
        return
    now = timezone.now()
    with transaction.atomic():
        held = PersonRole.objects.select_for_update().filter(user=user).first()
        if held is not None and held.role == role:
            held.resolved_at = now
            held.save(update_fields=["resolved_at"])
            user.person_role = held
            return
        before = "" if held is None else held.role
        row, _ = PersonRole.objects.update_or_create(user=user, defaults={"role": role, "resolved_at": now})
        record_event(
            action=AuditLog.Action.CREATED if held is None else AuditLog.Action.UPDATED,
            obj=row,
            type_label=AUDIT_LABEL,
            message=f"{user} is now {Role(role).label.lower()}" + (f", was {before}" if before else ""),
            metadata={"role": role, "was": before},
            user=user,
            required=True,
        )
    user.person_role = row


def give_operator(user) -> None:
    """An operator row for an account the host's own shell made, without an audit row of its own.

    Creating a superuser is not audited either; its next sign-in resolves its role afresh.
    """

    row, _ = PersonRole.objects.get_or_create(user=user, defaults={"role": Role.OPERATOR})
    user.person_role = row
