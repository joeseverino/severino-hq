"""An agent HQ has seen: it called over the API and MCP, was refused a change
to the tailnet the operator blocked it from, and asked to answer the photos
library's name with the server's tailnet address, which waits because the
operator asks to see the records it changes first.

Each step runs the way a verified token's call does in production, so the
Agents page and the approvals queue read what HQ itself records.
"""

from datetime import timedelta

from hq.domains.control_plane.models import CapabilityRule, ManagedResource
from hq.platform.api.security import api_principal
from hq.platform.application import agent_registry
from hq.platform.application.capabilities import execute_capability
from hq.platform.core.models import AgentIdentity
from hq.platform.mcp.identity import token_principal

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


def seen(owner=None):
    for principal in (api_principal(CLAIMS), token_principal(CLAIMS)):
        agent_registry.observe(principal)
    _decide(owner)
    execute_capability("tailnet.reach.allow", {}, principal=api_principal(CLAIMS), target=CERTIFICATE)
    execute_capability(
        "infrastructure.resource.update", _tailnet_only(), principal=api_principal(CLAIMS), target=REWRITE
    )
    # Its last call, a few minutes ago.
    AgentIdentity.objects.filter(client_id=AGENT).update(last_seen=now() - timedelta(minutes=4))


def _decide(owner):
    for capability, rule in RULES:
        CapabilityRule.objects.create(
            scope=CapabilityRule.Scope.AGENT, subject=AGENT, capability=capability, rule=rule, changed_by=owner
        )


def _tailnet_only():
    """The rewrite as declared, answering with the server's tailnet address."""

    record = ManagedResource.objects.get(key=REWRITE)
    return {"key": record.key, "kind": record.kind, "spec": {**record.spec, "answer": TAILNET}}
