"""Ending every HQ session a person holds, wherever they are signed in."""

from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.db import transaction
from django.utils import timezone
lazy from django.contrib.auth import get_user_model
lazy from django.contrib.sessions.models import Session

from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog

from .command_fields import Describe
from .linked_accounts import SIGN_IN, sign_in_subject
from .security import Principal
from .ui import counted

# The capability the identity provider's back-channel logout runs.
SIGN_OUT = "session.sign_out"


@dataclass(frozen=True, slots=True)
class SignOutCommand:
    subject: Annotated[str, Describe("Pocket ID's subject for the person it signed out.")]


class SessionsEnded(TypedDict):
    """The sessions ended for the person the identity provider signed out."""

    ok: Literal[True]
    sessions: Annotated[int, Describe("How many sessions the call ended.")]


def sign_out(command: SignOutCommand, *, principal: Principal) -> SessionsEnded:
    """End every session of the person the identity provider signed out, by its subject.

    A subject no sign-in names has no session here to end, so nothing is done.
    """

    del principal
    key = sign_in_subject(settings.OIDC_ISSUER, command.subject.strip())
    users = get_user_model()._default_manager
    found = users.filter(linked_accounts__provider=SIGN_IN, linked_accounts__login=key).first()
    if found is None:
        return {"ok": True, "sessions": 0}
    return {"ok": True, "sessions": end_sessions(found, reason="the identity provider signed them out")}


def end_sessions(user, *, reason: str) -> int:
    """Sign ``user`` out of every live session, audited with ``reason``; how many ended.

    The sessions are the database's (Django's default engine), each read for
    whose it is, since a session row is keyed by nothing else. The account
    itself is untouched, so a local password kept for break-glass still works.
    """

    with transaction.atomic():
        theirs = [
            session.session_key
            for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator()
            if str(session.get_decoded().get(SESSION_KEY, "")) == str(user.pk)
        ]
        Session.objects.filter(session_key__in=theirs).delete()
        record_event(
            action=AuditLog.Action.LOGOUT,
            obj=user,
            type_label="User",
            message=f"{user} signed out of {counted(len(theirs), 'session')}: {reason}",
            metadata={"sessions": len(theirs)},
            user=user,
            required=True,
        )
    return len(theirs)
