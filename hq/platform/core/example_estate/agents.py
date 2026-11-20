"""An agent HQ has seen, with its devices, a refusal, a held change and an open
stream, and the agents of the last week (``lately``).

Each step runs the way a verified token's call does in production, so the
Agents page and the approvals queue read what HQ itself records.
"""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import F

from hq.domains.control_plane.models import CapabilityRule, ManagedResource
from hq.platform.application import (
    agent_access,
    agent_admission,
    agent_limits,
    agent_registry,
    denials,
    linked_accounts,
    roles,
    tailnet,
)
from hq.platform.application.agent_access import SET_AGENT_BLOCK
from hq.platform.application.agent_devices import APPROVE_AGENT_DEVICE, BLOCK_AGENT_DEVICE
from hq.platform.application.agent_limits import Slot
from hq.platform.application.approvals import is_held
from hq.platform.application.capabilities import command_spec, run_capability
from hq.platform.application.capability_policy import SET_POLICY, set_rule
from hq.platform.application.linked_accounts import sign_in_subject
from hq.platform.application.own_devices import SET_OWN_DEVICE
from hq.platform.application.refusal_codes import Code
from hq.platform.application.security import (
    API_INTERFACE,
    MCP_INTERFACE,
    Capability,
    PolicyDenied,
    agent_grantable,
    agent_subject,
    web_principal,
)
from hq.platform.application.tokens import token_principal
from hq.platform.core import bindings
from hq.platform.core.attribution import from_the_shell, signing_in
from hq.platform.core.audit import stamped
from hq.platform.core.models import AgentDevice, AgentIdentity, LinkedAccount, OwnDevice, PersonRole

from . import doors
from .infra import CERTIFICATE
from .names import LAPTOP_NAME, MACHINE, PHONE_NAME, TAILNET, now, tailnet_v4
from .network import POLICY

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
# A tailnet address no device of the estate holds, which the assistant's token
# was once presented from: tailscaled placed that call on no device, so it was
# refused.
UNPLACED_AT = tailnet_v4(None)
# A reader a viewer signed in, whose token carries more than a viewer holds:
# the calendar writes are granted, and beyond its person's role. The viewer
# signed the assistant in too, from their phone: the same client as another
# person is a row of its own, with its own rules, block and devices.
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
    agent = _from(token_principal(CLAIMS, MCP_INTERFACE), MACHINE)
    with doors.mcp():
        reach = run_capability(
            "tailnet.reach.allow", {}, principal=agent, target=CERTIFICATE, idempotency_key="example-reach"
        ).answer
    _expect(reach, reach.get("error", {}).get("code") == PolicyDenied.code, "refused by the operator's rule")
    with doors.mcp():
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


# The tailnet policy as HQ keeps it in the seed that holds a change to it.
POLICY_KEY = "tailnet-policy"


def asks_to_open_the_policy():
    """The tailnet policy kept in HQ, its test that the server's SSH port is
    shut beside the owner's, and the agent asking to open that port to the
    admins and drop the test: held for the owner, as every change to the
    policy is."""

    kept = {**POLICY, "tests": [{**POLICY["tests"][0], "deny": ["tag:server:22"]}]}
    ManagedResource.objects.create(key=POLICY_KEY, kind="tailscale.policy", spec={"document": json.dumps(kept)})
    opened = {
        **POLICY,
        "grants": [*POLICY["grants"], {"src": ["group:admins"], "dst": ["tag:server"], "ip": ["22"]}],
    }
    agent = _from(token_principal(CLAIMS, MCP_INTERFACE), MACHINE)
    with doors.mcp():
        update = run_capability(
            "infrastructure.resource.update",
            {"key": POLICY_KEY, "kind": "tailscale.policy", "spec": {"document": json.dumps(opened)}},
            principal=agent,
            target=POLICY_KEY,
            idempotency_key="example-policy-update",
        ).answer
    _expect(update, is_held(update), "held for the operator")


def lately(owner=None):
    """The week behind the Agents page, after the months ``history.settle``
    dates: an over-rate assistant, an over-granted reader, a blocked runner
    and the owner's phone.
    """

    # The server was approved for the agent the day it was first seen (``history.settle``).
    AgentDevice.objects.filter(identity__client_id=AGENT, approved_at__isnull=False).update(approved_at=F("first_seen"))
    # Each step's events are dated to the day it happened, as its record is.
    first_seen, blocked_at, registered_at = (now() - timedelta(days=days) for days in (FIRST_SEEN, 1, REGISTERED))
    # The assistant acts for the owner, so their sign-in names them first; with no owner it is refused.
    if owner is not None:
        with _on(first_seen), doors.page(doors.SIGN_IN), signing_in(owner.get_username()):
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
            _observe_from(token_principal(_assistant_claims(VIEWER_PERSON), MCP_INTERFACE), PHONE_NAME)
        AgentIdentity.objects.filter(person__username=VIEWER_USERNAME).update(first_seen=joined)
        AgentDevice.objects.filter(identity__client_id=READER).update(
            first_seen=joined, last_seen=now() - timedelta(hours=3)
        )
        AgentDevice.objects.filter(identity__client_id=ASSISTANT, identity__person__username=VIEWER_USERNAME).update(
            first_seen=joined, last_seen=now() - timedelta(hours=3)
        )
    AgentIdentity.objects.filter(client_id=READER).update(last_seen=now() - timedelta(hours=3))
    AgentIdentity.objects.filter(client_id=ASSISTANT).update(last_seen=now() - timedelta(minutes=12))
    AgentIdentity.objects.filter(client_id=RUNNER).update(last_seen=now() - timedelta(days=2))
    # Refused for its rate: a minute of refusals, written as the one row that counts them.
    counted = {"created_at": now() - timedelta(minutes=14), "metadata": {denials.COUNT: RATE_LIMITED_TIMES}}
    with stamped(lambda _obj, _action, _type: counted), doors.mcp():
        denials.record_denial(interface=MCP_INTERFACE, reason=Code.RATE_LIMITED, actor=ASSISTANT)
    if owner is not None:
        # Stopped at its admission: the tailnet placed it on no device.
        unplaced = agent_access.unplaced(Code.DEVICE_UNKNOWN)
        stopped = agent_admission.Stopped(token_principal(_assistant_claims(), MCP_INTERFACE), unplaced)
        with _on(now() - timedelta(minutes=30)), doors.mcp():
            refused = agent_admission.refused(stopped, source=UNPLACED_AT)
        _expect(refused, refused is not None, "refused")
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


def _assistant_claims(person=PERSON):
    """The assistant's token as ``person`` signed it in: every permission HQ
    offers an agent, as a sign-in asks for them all, but ``NOT_CARRIED``."""

    granted = sorted(agent_grantable() - {NOT_CARRIED})
    return {"client_id": ASSISTANT, "sub": person, "scope": " ".join(granted)}


def _listen(principal, streams):
    """``principal`` holding ``streams`` update streams open, each opened as the MCP door opens one.

    A stream lives in the serving process's memory, so these last as long as
    the process that builds the estate, and a rebuild opens only those it
    lacks.
    """

    with doors.mcp():
        for _ in range(streams - agent_limits.limits().listening(agent_subject(principal))):
            held = agent_admission.stream_opened(principal)
            _expect(held, isinstance(held, Slot), "opened")


def _from(principal, device_name):
    """``principal``'s call placed on the tailnet device ``device_name``, as its admission places one."""

    return replace(principal, device=_device(device_name).node_id)


def _observe_from(principal, device_name):
    device = _device(device_name)
    agent_registry.observe(replace(principal, device=device.node_id), device)


def _device(device_name):
    """The estate's device ``device_name``, as tailscaled would place a call from it: the reading's record of it."""

    device = tailnet.device_at(tailnet_v4(device_name))
    if device is None:
        raise LookupError(f"the estate's tailnet reading names no device {device_name}")
    return device


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


def example_viewer():
    """The person the estate holds in the viewer role, or ``LookupError`` for a set that holds none."""

    viewer = get_user_model().objects.filter(username=VIEWER_USERNAME).first()
    if viewer is None:
        raise LookupError("This data set holds no viewer.")
    return viewer


def _viewer(moment):
    """A person who holds the viewer role, signed in once at ``moment``, as the
    sign-in names them and their role."""

    viewer = get_user_model().objects.create_user(username=VIEWER_USERNAME, password=None)
    with _on(moment), doors.page(doors.SIGN_IN), signing_in(viewer.get_username()):
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
    """The owner's rules for the agent, set when it was first seen."""

    with _on(now() - timedelta(days=FIRST_SEEN)):
        for capability, rule in RULES:
            ruled(owner, capability, rule)


def ruled(owner, capability, rule):
    """The owner's rule for the agent on ``capability``, set as the Agents page sets it."""

    with _deciding(owner):
        set_rule(subject=AGENT, spec=command_spec(capability), rule=rule, user=owner)


@contextmanager
def _deciding(owner):
    """The owner's rules for an agent, set on the Agents page; the shell's with no owner."""

    if owner is None:
        with from_the_shell(SET_POLICY):
            yield
        return
    with doors.page("agent_policy"), bindings.audit_operation(operation=SET_POLICY, principal=web_principal(owner)):
        yield


def _expect(answer, met, meant):
    """Stop the estate where a step it builds on answered other than it was ``meant``."""

    if not met:
        raise RuntimeError(f"the estate's step was meant to be {meant}, and was answered {answer}")


def _tailnet_only():
    """The rewrite as declared, answering with the server's tailnet address."""

    record = ManagedResource.objects.get(key=REWRITE)
    return {"key": record.key, "kind": record.kind, "spec": {**record.spec, "answer": TAILNET}}
