"""An agent HQ has seen: it called over the API and MCP, was refused a change
to the tailnet the operator blocked it from, and asked to answer the photos
library's name with the server's tailnet address, which waits because the
operator asks to see the records it changes first. Beside it, the agents of
the last week (``lately``).

Each step runs the way a verified token's call does in production, so the
Agents page and the approvals queue read what HQ itself records.
"""

from datetime import timedelta

from django.conf import settings
from django.test import override_settings

from hq.domains.control_plane.models import CapabilityRule, ManagedResource
from hq.platform.application import agent_registry, denials, linked_accounts
from hq.platform.application.agent_access import SET_AGENT_BLOCK
from hq.platform.application.agent_limits import RATE_LIMITED
from hq.platform.application.approvals import is_held
from hq.platform.application.capabilities import run_capability
from hq.platform.application.linked_accounts import sign_in_subject
from hq.platform.application.own_devices import SET_OWN_DEVICE
from hq.platform.application.security import API_INTERFACE, MCP_INTERFACE, PolicyDenied, web_principal
from hq.platform.application.tokens import token_principal
from hq.platform.core.audit import stamped
from hq.platform.core.models import AgentIdentity, LinkedAccount, OwnDevice

from . import doors
from .infra import CERTIFICATE
from .names import TAILNET, now

AGENT = "example-agent"
# What its token carries: reading HQ, mirroring the documentation index, and
# keeping the infrastructure records.
CLAIMS = {"client_id": AGENT, "scope": "read sync_documentation manage_infrastructure"}
# What the operator decided for it: a record it changes waits for them, and the
# tailnet policy is not its to touch.
RULES = (
    ("infrastructure.resource.update", CapabilityRule.Rule.APPROVE),
    ("tailnet.reach.allow", CapabilityRule.Rule.DENY),
)
# The name it asks to move: the photos library, answered on the tailnet like HQ.
REWRITE = "immich-dns"

# The owner's subject at the identity provider, which a token they signed in names.
PERSON = "example-owner-subject"
# An assistant the owner signed in, reading HQ over MCP for them.
ASSISTANT = "example-assistant"
ASSISTANT_CLAIMS = {"client_id": ASSISTANT, "sub": PERSON, "scope": "read write_calendar"}
# How many of its calls one minute refused for calling too fast.
RATE_LIMITED_TIMES = 14
# A build runner calling as itself, its subject the client.
RUNNER = "example-runner"
RUNNER_CLAIMS = {"client_id": RUNNER, "sub": f"client-{RUNNER}", "scope": "read sync_documentation"}
# The owner's phone, registered as their own device.
PHONE = "example-phone"
# How many days ago the assistant and the runner first called, and the phone was registered.
FIRST_SEEN = 40
REGISTERED = 30


# The estate is a deployment that lets agents keep the infrastructure records
# and mirror the documentation index, so the grant above is held as granted.
@override_settings(SEVERINO_AGENT_ENABLE_INFRASTRUCTURE=True, SEVERINO_AGENT_ENABLE_DOC_SYNC=True)
def seen(owner=None):
    for interface in (API_INTERFACE, MCP_INTERFACE):
        with _first_call(interface):
            agent_registry.observe(token_principal(CLAIMS, interface))
    _decide(owner)
    agent = token_principal(CLAIMS, API_INTERFACE)
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
    # Its last call, a few minutes ago.
    AgentIdentity.objects.filter(client_id=AGENT).update(last_seen=now() - timedelta(minutes=4))


def lately(owner=None):
    """The week behind the Agents page, after the months ``history.settle`` dates.

    An assistant the owner signed in through their own sign-in, calling over
    MCP faster than HQ lets it; a build runner calling as itself, which the
    owner blocked; and the owner's phone, registered as their own device.
    """

    # Each step's events are dated to the day it happened, as its record is.
    first_seen, blocked_at, registered_at = (now() - timedelta(days=days) for days in (FIRST_SEEN, 1, REGISTERED))
    # The assistant acts for the owner, so their sign-in names them first; with no owner it is refused.
    if owner is not None:
        with _on(first_seen), doors.page(doors.SIGN_IN):
            linked_accounts.bind_sign_in(owner, sign_in_subject(settings.OIDC_ISSUER, PERSON))
        LinkedAccount.objects.filter(user=owner, provider=linked_accounts.SIGN_IN).update(updated_at=first_seen)
    signed_in = ((ASSISTANT_CLAIMS, MCP_INTERFACE),) if owner is not None else ()
    for claims, interface in (*signed_in, (RUNNER_CLAIMS, API_INTERFACE)):
        with _on(first_seen), _first_call(interface):
            agent_registry.observe(token_principal(claims, interface))
    AgentIdentity.objects.filter(client_id__in=(ASSISTANT, RUNNER)).update(first_seen=first_seen)
    AgentIdentity.objects.filter(client_id=ASSISTANT).update(last_seen=now() - timedelta(minutes=12))
    AgentIdentity.objects.filter(client_id=RUNNER).update(last_seen=now() - timedelta(days=2))
    # Refused for its rate: a minute of refusals, written as the one row that counts them.
    counted = {"created_at": now() - timedelta(minutes=14), "metadata": {denials.COUNT: RATE_LIMITED_TIMES}}
    with stamped(lambda _obj, _action, _type: counted), doors.mcp():
        denials.record_denial(interface=MCP_INTERFACE, reason=RATE_LIMITED, actor=ASSISTANT)
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


def _on(moment):
    """Every event the block records, dated ``moment``."""

    return stamped(lambda *_: {"created_at": moment})


def _first_call(interface):
    """The door of an agent's first call through ``interface``: the machine
    API's list of what it may run, or the MCP server."""

    return doors.page("hq_api:capabilities") if interface == API_INTERFACE else doors.mcp()


def _decide(owner):
    for capability, rule in RULES:
        CapabilityRule.objects.create(
            scope=CapabilityRule.Scope.AGENT, subject=AGENT, capability=capability, rule=rule, changed_by=owner
        )


def _expect(answer, met, meant):
    """Stop the estate where a step it builds on answered other than it was ``meant``."""

    if not met:
        raise RuntimeError(f"the estate's step was meant to be {meant}, and was answered {answer}")


def _tailnet_only():
    """The rewrite as declared, answering with the server's tailnet address."""

    record = ManagedResource.objects.get(key=REWRITE)
    return {"key": record.key, "kind": record.kind, "spec": {**record.spec, "answer": TAILNET}}
