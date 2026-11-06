"""An agent HQ has seen: it called over the API and MCP from the server, a
device the operator approved for it, was refused a change to the tailnet the
operator blocked it from, and asked to answer the photos library's name with
the server's tailnet address, which waits because the operator asks to see the
records it changes first, and holds one update stream open. Beside it, the agents of the last week (``lately``):
one calling from the owner's laptop, a device that waits for the owner's
approval, and from an address the tailnet reading does not hold yet, holding
every update stream it may; and one a
viewer signed in, calling from the owner's phone, which the owner blocked.

Each step runs the way a verified token's call does in production, so the
Agents page and the approvals queue read what HQ itself records.
"""

from dataclasses import replace
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import F

from hq.domains.control_plane.models import CapabilityRule, ManagedResource
from hq.platform.application import (
    agent_admission,
    agent_devices,
    agent_limits,
    agent_registry,
    denials,
    linked_accounts,
    roles,
)
from hq.platform.application.agent_access import SET_AGENT_BLOCK
from hq.platform.application.agent_devices import APPROVE_AGENT_DEVICE, BLOCK_AGENT_DEVICE
from hq.platform.application.agent_limits import RATE_LIMITED, Slot
from hq.platform.application.approvals import is_held
from hq.platform.application.capabilities import run_capability
from hq.platform.application.linked_accounts import sign_in_subject
from hq.platform.application.own_devices import SET_OWN_DEVICE
from hq.platform.application.security import (
    API_INTERFACE,
    MCP_INTERFACE,
    Capability,
    PolicyDenied,
    agent_grantable,
    web_principal,
)
from hq.platform.application.tokens import token_principal
from hq.platform.core.audit import stamped
from hq.platform.core.models import AgentDevice, AgentIdentity, LinkedAccount, OwnDevice, PersonRole

from . import doors
from .infra import CERTIFICATE
from .names import LAPTOP_NAME, MACHINE, PHONE_NAME, TAILNET, now, tailnet_v4

AGENT = "example-agent"
# What its token carries: reading HQ, mirroring the documentation index, and
# keeping the infrastructure records.
CLAIMS = {"client_id": AGENT, "scope": "read sync_documentation manage_infrastructure"}
# What the operator decided for it: it stops a container without asking first,
# a record it changes asks them first, and the tailnet policy is off.
RULES = (
    ("infrastructure.resource.stop", CapabilityRule.Rule.ALLOW),
    ("infrastructure.resource.update", CapabilityRule.Rule.APPROVE),
    ("tailnet.reach.allow", CapabilityRule.Rule.DENY),
)
# The name it asks to move: the photos library, answered on the tailnet like HQ.
REWRITE = "immich-dns"

# The owner's subject at the identity provider, which a token they signed in names.
PERSON = "example-owner-subject"
# An assistant the owner signed in, reading HQ over MCP for them.
ASSISTANT = "example-assistant"
# The one permission HQ offers that its last sign-in did not carry, as one a
# release added since would be.
NOT_CARRIED = Capability.LOOK_UP_PUBLIC_RECORDS
# How many of its calls one minute refused for calling too fast.
RATE_LIMITED_TIMES = 14
# A tailnet address no device in the reading holds, which the assistant called from.
UNSEEN_AT = "100.101.102.140"
# A reader a viewer signed in, whose token carries more than a viewer holds:
# the calendar writes are granted, and beyond its person's role.
VIEWER_USERNAME = "riley"
VIEWER_PERSON = "example-viewer-subject"
READER = "example-reader"
READER_CLAIMS = {"client_id": READER, "sub": VIEWER_PERSON, "scope": "read write_calendar"}
# A build runner calling as itself, its subject the client.
RUNNER = "example-runner"
RUNNER_CLAIMS = {"client_id": RUNNER, "sub": f"client-{RUNNER}", "scope": "read sync_documentation"}
# The owner's phone, registered as their own device.
PHONE = "example-phone"
# How many days ago the assistant and the runner first called, and the phone was registered.
FIRST_SEEN = 40
REGISTERED = 30
# How many days ago the viewer first signed in, and their reader first called.
JOINED = 20


def seen(owner=None):
    for interface in (API_INTERFACE, MCP_INTERFACE):
        with _first_call(interface):
            _observe_from(token_principal(CLAIMS, interface), MACHINE)
    _decide(owner)
    if owner is not None:
        _approve(owner, AGENT)
    agent = _from(token_principal(CLAIMS, API_INTERFACE), MACHINE)
    with doors.called("tailnet.reach.allow"):
        reach = run_capability(
            "tailnet.reach.allow", {}, principal=agent, target=CERTIFICATE, idempotency_key="example-reach"
        ).answer
    _expect(reach, reach.get("error", {}).get("code") == PolicyDenied.code, "refused by the operator's rule")
    with doors.called("infrastructure.resource.update"):
        update = run_capability(
            "infrastructure.resource.update",
            _tailnet_only(),
            principal=agent,
            target=REWRITE,
            idempotency_key="example-update",
        ).answer
    _expect(update, is_held(update), "held for the operator")
    _listen(token_principal(CLAIMS, MCP_INTERFACE), 1)
    # Its last call, a few minutes ago, from the server.
    AgentIdentity.objects.filter(client_id=AGENT).update(last_seen=now() - timedelta(minutes=4))
    AgentDevice.objects.filter(identity__client_id=AGENT).update(last_seen=now() - timedelta(minutes=4))


def lately(owner=None):
    """The week behind the Agents page, after the months ``history.settle`` dates.

    An assistant the owner signed in through their own sign-in, carrying every
    permission HQ offers but one, calling over MCP faster than HQ lets it, and
    once from a device the tailnet reading did not hold; a reader a viewer
    signed in, granted more than their role, calling from the owner's phone,
    which the owner blocked there; a build runner calling as itself, which the
    owner blocked; and the owner's phone, registered as their own device.
    """

    # The server was approved for the agent the day it was first seen (``history.settle``).
    AgentDevice.objects.filter(identity__client_id=AGENT, approved_at__isnull=False).update(approved_at=F("first_seen"))
    # Each step's events are dated to the day it happened, as its record is.
    first_seen, blocked_at, registered_at = (now() - timedelta(days=days) for days in (FIRST_SEEN, 1, REGISTERED))
    # The assistant acts for the owner, so their sign-in names them first; with no owner it is refused.
    if owner is not None:
        with _on(first_seen), doors.page(doors.SIGN_IN):
            linked_accounts.bind_sign_in(owner, sign_in_subject(settings.OIDC_ISSUER, PERSON))
        LinkedAccount.objects.filter(user=owner, provider=linked_accounts.SIGN_IN).update(updated_at=first_seen)
    if owner is not None:
        # From the owner's laptop, which they have not approved for it yet.
        with _on(first_seen), _first_call(MCP_INTERFACE):
            _observe_from(token_principal(_assistant_claims(), MCP_INTERFACE), LAPTOP_NAME)
        AgentDevice.objects.filter(identity__client_id=ASSISTANT).update(
            first_seen=first_seen, last_seen=now() - timedelta(minutes=12)
        )
    with _on(first_seen), _first_call(API_INTERFACE):
        agent_registry.observe(token_principal(RUNNER_CLAIMS, API_INTERFACE))
    AgentIdentity.objects.filter(client_id__in=(ASSISTANT, RUNNER)).update(first_seen=first_seen)
    if owner is not None:
        joined = now() - timedelta(days=JOINED)
        _viewer(joined)
        with _on(joined), _first_call(MCP_INTERFACE):
            _observe_from(token_principal(READER_CLAIMS, MCP_INTERFACE), PHONE_NAME)
        AgentIdentity.objects.filter(client_id=READER).update(first_seen=joined)
        AgentDevice.objects.filter(identity__client_id=READER).update(
            first_seen=joined, last_seen=now() - timedelta(hours=3)
        )
    AgentIdentity.objects.filter(client_id=READER).update(last_seen=now() - timedelta(hours=3))
    AgentIdentity.objects.filter(client_id=ASSISTANT).update(last_seen=now() - timedelta(minutes=12))
    AgentIdentity.objects.filter(client_id=RUNNER).update(last_seen=now() - timedelta(days=2))
    # Refused for its rate: a minute of refusals, written as the one row that counts them.
    counted = {"created_at": now() - timedelta(minutes=14), "metadata": {denials.COUNT: RATE_LIMITED_TIMES}}
    with stamped(lambda _obj, _action, _type: counted), doors.mcp():
        denials.record_denial(interface=MCP_INTERFACE, reason=RATE_LIMITED, actor=ASSISTANT)
    if owner is not None:
        # Refused at its admission, which placed it on no device.
        with _on(now() - timedelta(minutes=30)), doors.mcp():
            unseen = agent_admission.refused(token_principal(_assistant_claims(), MCP_INTERFACE), source=UNSEEN_AT)
        _expect(unseen, unseen is not None, "refused")
        _listen(token_principal(_assistant_claims(), MCP_INTERFACE), settings.SEVERINO_AGENT_STREAMS_AT_ONCE)
    if owner is not None:
        # Pressed in the browser, whose form sends a retry key.
        with _on(blocked_at), doors.page("agent_block"):
            blocked = run_capability(
                SET_AGENT_BLOCK,
                {"blocked": True},
                principal=web_principal(owner),
                target=RUNNER,
                idempotency_key="example-block",
            ).answer
        _expect(blocked, blocked["ok"], "done")
    # Blocked the day after its last call.
    AgentIdentity.objects.filter(client_id=RUNNER).update(blocked_at=blocked_at)
    if owner is None:
        return
    _block(owner, READER, blocked_at)
    with _on(registered_at), doors.page("own_device"):
        registered = run_capability(
            SET_OWN_DEVICE,
            {"registered": True},
            principal=web_principal(owner),
            target=PHONE,
            idempotency_key="example-device",
        ).answer
    _expect(registered, registered["ok"], "done")
    OwnDevice.objects.filter(client_id=PHONE).update(registered_at=registered_at)


def _assistant_claims():
    """The assistant's token: every permission HQ offers an agent, as a sign-in
    asks for them all, but ``NOT_CARRIED``."""

    granted = sorted(agent_grantable() - {NOT_CARRIED})
    return {"client_id": ASSISTANT, "sub": PERSON, "scope": " ".join(granted)}


def _listen(principal, streams):
    """``principal`` holding ``streams`` update streams open, each opened as the MCP door opens one.

    HQ counts a stream in its one serving process's memory, so these are held
    for the life of the process that builds the estate: a page it draws shows
    them, as the demo, ``dev:look`` and the browser gates draw, and a server
    the ``seed`` command filled from another process holds none. A database
    rolled back gives none of them back, so an estate built again in the same
    process opens only those it lacks.
    """

    with doors.mcp():
        for _ in range(streams - agent_limits.LIMITS.listening(principal.actor)):
            held = agent_admission.stream_opened(principal)
            _expect(held, isinstance(held, Slot), "opened")


def _from(principal, device_name):
    """``principal``'s call placed on the tailnet device ``device_name``, as its admission places one."""

    return replace(principal, device=agent_devices.place(tailnet_v4(device_name)).node_id)


def _observe_from(principal, device_name):
    placed = agent_devices.place(tailnet_v4(device_name))
    agent_registry.observe(replace(principal, device=placed.node_id), placed.device)


def _decide_device(owner, client_id, capability, field):
    """The owner's press of ``capability`` on the one device ``client_id`` has called from, in the browser."""

    device = AgentDevice.objects.get(identity__client_id=client_id)
    with doors.page("agent_device"):
        decided = run_capability(
            capability,
            {field: True},
            principal=web_principal(owner),
            target=str(device.pk),
            idempotency_key=f"example-{field}-{client_id}",
        ).answer
    _expect(decided, decided["ok"], "done")
    return device


def _approve(owner, client_id):
    _decide_device(owner, client_id, APPROVE_AGENT_DEVICE, "approved")


def _block(owner, client_id, moment):
    """Blocked at ``moment``, as its record and its event say."""

    with _on(moment):
        device = _decide_device(owner, client_id, BLOCK_AGENT_DEVICE, "blocked")
    AgentDevice.objects.filter(pk=device.pk).update(blocked_at=moment)


def _viewer(moment):
    """A person who holds the viewer role, signed in once at ``moment``, as the
    sign-in names them and their role."""

    viewer = get_user_model().objects.create_user(username=VIEWER_USERNAME, password=None)
    with _on(moment), doors.page(doors.SIGN_IN):
        roles.assign(viewer, roles.Role.VIEWER)
        linked_accounts.bind_sign_in(viewer, sign_in_subject(settings.OIDC_ISSUER, VIEWER_PERSON))
    LinkedAccount.objects.filter(user=viewer, provider=linked_accounts.SIGN_IN).update(updated_at=moment)
    PersonRole.objects.filter(user=viewer).update(resolved_at=moment)


def _on(moment):
    """Every event the block records, dated ``moment``."""

    return stamped(lambda *_: {"created_at": moment})


def _first_call(interface):
    """The door of an agent's first call through ``interface``: the machine
    API's list of what it may run, or the MCP server."""

    return doors.page("hq_api:capabilities") if interface == API_INTERFACE else doors.mcp()


def _decide(owner):
    for capability, rule in RULES:
        CapabilityRule.objects.create(subject=AGENT, capability=capability, rule=rule, changed_by=owner)


def _expect(answer, met, meant):
    """Stop the estate where a step it builds on answered other than it was ``meant``."""

    if not met:
        raise RuntimeError(f"the estate's step was meant to be {meant}, and was answered {answer}")


def _tailnet_only():
    """The rewrite as declared, answering with the server's tailnet address."""

    record = ManagedResource.objects.get(key=REWRITE)
    return {"key": record.key, "kind": record.kind, "spec": {**record.spec, "answer": TAILNET}}
