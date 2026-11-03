"""The clients a person registered in HQ as their own devices, the half of a
device's standing HQ keeps (docs/Security.md#agents)."""

import logging
import re
from dataclasses import dataclass
from typing import Annotated, Any

from django.db import DatabaseError, transaction

from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog, OwnDevice

from .command_fields import Describe
from .linked_accounts import SIGN_IN, linked_login
from .record_errors import InputError, PolicyError
from .security import Principal, person

logger = logging.getLogger("severino.agents")

AUDIT_LABEL = "Own device"

SET_OWN_DEVICE = "agent.device.set"

# A client the identity provider registered. A client known only by a Client
# ID Metadata Document is named by a URL, which this refuses, so one opt-in
# covering every such client can never reach a person's standing.
REGISTERED_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]+")


def registered(client_id: str, person_key: str) -> bool:
    """Whether the person whose sign-in key is ``person_key`` registered ``client_id`` as theirs.

    The person is found by the subject bound at their sign-in, so a token
    another person signed in through the same client matches nothing. Fails
    closed: a registry that cannot be read leaves the token an agent's.
    """

    if not person_key or not REGISTERED_CLIENT_ID.fullmatch(client_id):
        return False
    try:
        return OwnDevice.objects.filter(
            client_id=client_id,
            user__linked_accounts__provider=SIGN_IN,
            user__linked_accounts__login=person_key,
        ).exists()
    except DatabaseError:
        logger.warning(
            "Own devices unreadable; %s stands as an agent.", client_id, extra={"event": "agents.devices.unreadable"}
        )
        return False


def devices_of(user) -> list[OwnDevice]:
    return list(OwnDevice.objects.filter(user=user))


@dataclass(frozen=True, slots=True)
class OwnDeviceCommand:
    """Whether this client is one of the signed-in person's own devices."""

    registered: Annotated[bool, Describe("Whether the client is registered as your own device.")]


def set_own_device(command: OwnDeviceCommand, *, principal: Principal, current_key: str) -> dict[str, Any]:
    """Register a client as the signed-in person's device, or remove it. Set, not toggled.

    Only for the person doing it: nobody registers a device for someone else.
    """

    user = person(principal)
    client = current_key.strip()
    if not REGISTERED_CLIENT_ID.fullmatch(client):
        raise InputError("A device's client id is the one Pocket ID registered: letters, digits, . _ or -.")
    with transaction.atomic():
        held = OwnDevice.objects.select_for_update().filter(user=user, client_id=client).first()
        if (held is not None) == command.registered:
            return {"ok": True, "client_id": client, "registered": command.registered}
        if command.registered:
            if not linked_login(user, SIGN_IN):
                raise PolicyError("You have not signed in through Pocket ID, so no device's token can name you yet.")
            row = OwnDevice.objects.create(user=user, client_id=client)
        else:
            row = held
        record_event(
            action=AuditLog.Action.CREATED if command.registered else AuditLog.Action.DELETED,
            obj=row,
            type_label=AUDIT_LABEL,
            message=f"{client} {'added to' if command.registered else 'removed from'} your own devices",
            metadata={"client_id": client, "registered": command.registered},
            user=user,
            required=True,
        )
        if not command.registered:
            row.delete()
    return {"ok": True, "client_id": client, "registered": command.registered}
