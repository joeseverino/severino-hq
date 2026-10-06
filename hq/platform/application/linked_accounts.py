"""Accounts elsewhere that a person's sign-in says are theirs.

The identity provider carries them as claims (``github: <login>``). HQ keeps the
latest one per provider against the user, so work that runs with no session
behind it (a sweep of what they star) knows whose account to read.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog, LinkedAccount

GITHUB = "github"
# The identity provider's own subject for the person: the one claim that names
# them and never changes. Kept as a digest of issuer and subject, which fits the
# column whatever length the provider's subjects are.
SIGN_IN = "oidc"
AUDIT_LABEL = "Linked account"

# GitHub's own rule for a login: letters, digits and single hyphens, neither
# first nor last, at most 39 characters.
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def claimed_github_login(payload: Mapping[str, Any]) -> str:
    """The ``github`` claim, when it is a login GitHub could have issued; else ""."""

    value = payload.get(GITHUB)
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if _GITHUB_LOGIN.match(value) else ""


def record_claimed_accounts(user, payload: Mapping[str, Any]) -> None:
    """Keep what this sign-in claims, and forget what it no longer does."""

    if getattr(user, "pk", None) is None:
        return
    _record(user, GITHUB, claimed_github_login(payload))


def _record(user, provider: str, login: str) -> None:
    with transaction.atomic():
        found = LinkedAccount.objects.select_for_update().filter(user=user, provider=provider).first()
        if found is not None and found.login == login:
            return
        if not login:
            if found is not None:
                record_event(
                    action=AuditLog.Action.DELETED,
                    obj=found,
                    type_label=AUDIT_LABEL,
                    message=f"Your sign-in no longer names the {provider} account {found.login}",
                    user=user,
                    required=True,
                )
                found.delete()
            return
        account, created = LinkedAccount.objects.update_or_create(
            user=user, provider=provider, defaults={"login": login, "updated_at": timezone.now()}
        )
        record_event(
            action=AuditLog.Action.CREATED if created else AuditLog.Action.UPDATED,
            obj=account,
            type_label=AUDIT_LABEL,
            message=f"Your sign-in names the {provider} account {login}",
            user=user,
            required=True,
        )


def linked_login(user, provider: str) -> str:
    """The login a user's sign-in last claimed for ``provider``, or ""."""

    found = LinkedAccount.objects.filter(user=user, provider=provider).values_list("login", flat=True).first()
    return found or ""


def sign_in_subject(issuer: str, subject: str) -> str:
    """The key a sign-in binds a user to: this issuer's name for this person."""

    return hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()


def bind_sign_in(user, key: str) -> None:
    """Hold ``user`` to the subject that signed in as them, from now on."""

    if getattr(user, "pk", None) is not None and key:
        _record(user, SIGN_IN, key)
