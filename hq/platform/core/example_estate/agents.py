"""An agent HQ has seen: it called over the API and MCP with a read grant, and
was refused a change to the tailnet it was not granted.

Each step runs the way a verified token's call does in production, so the
Agents page reads what HQ itself records.
"""

from datetime import timedelta

from hq.platform.api.security import api_principal
from hq.platform.application import agent_registry
from hq.platform.application.capabilities import execute_capability
from hq.platform.core.models import AgentIdentity
from hq.platform.mcp.identity import token_principal

from .infra import CERTIFICATE
from .names import now

AGENT = "example-agent"
# What its token carries: reading HQ, and mirroring the documentation index.
CLAIMS = {"client_id": AGENT, "scope": "read sync_documentation"}


def seen():
    for principal in (api_principal(CLAIMS), token_principal(CLAIMS)):
        agent_registry.observe(principal)
    execute_capability("tailnet.reach.allow", {}, principal=token_principal(CLAIMS), target=CERTIFICATE)
    # Its last call, a few minutes ago.
    AgentIdentity.objects.filter(client_id=AGENT).update(last_seen=now() - timedelta(minutes=4))
