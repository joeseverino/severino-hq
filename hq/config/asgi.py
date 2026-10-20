"""ASGI entrypoint for the HQ web UI and tailnet-only MCP endpoint.

The controller bridge is started from here and served elsewhere: on a private
Unix socket, never on this application's listener.
"""

import contextlib
import os

from django.conf import settings
from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.routing import Mount
lazy from asgiref.sync import sync_to_async

from hq.platform.core.network import TrustedNetworkASGI
from hq.platform.core.static import CachedStaticFiles
lazy from hq.domains.control_plane.bridge_application import application as bridge_application
lazy from hq.platform.api import security as api_security
lazy from hq.platform.application.agent_access import agents_paused
lazy from hq.platform.application.agent_registry import observe
lazy from hq.platform.application.denials import record_denial
lazy from hq.platform.core import doorbell, unix_server
lazy from hq.platform.core.ahead import keeping
lazy from hq.platform.mcp.identity import token_principal
lazy from hq.platform.mcp.security import MCPBoundary
lazy from hq.platform.mcp.server import mcp

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")

django_application = get_asgi_application()
# Wrapped, because this mount sits above the Django stack and so never reaches
# the middleware that refuses untrusted callers everywhere else.
# No compressor: the image build compressed each asset once and the mount sends
# that copy.
static_application = TrustedNetworkASGI(CachedStaticFiles(directory=settings.STATIC_ROOT, check_dir=False))


def verify_agent_token(bearer: str):
    """Turn a Pocket ID access token into the agent that presented it."""

    return token_principal(api_security.verify(bearer))


# Wired only when the machine API is configured. `is_configured` is false when
# `SEVERINO_API_RESOURCE` is empty, and without a resource to check `aud`
# against, a token minted for any other API on the same Pocket ID instance
# would verify here on signature alone. Absent means off, never "accept
# anything", and with no other credential, off means MCP is disabled.
mcp_verifier = verify_agent_token if api_security.is_configured() else None


async def agents_allowed() -> bool:
    return not await sync_to_async(agents_paused)()


async def record_mcp_denial(**fields) -> None:
    await sync_to_async(record_denial)(interface="mcp", **fields)


async def observe_agent(principal) -> None:
    await sync_to_async(observe)(principal)


mcp_served = mcp.served()
mcp_application = MCPBoundary(
    mcp_served.application,
    allowed_hosts=settings.SEVERINO_MCP_ALLOWED_HOSTS,
    allowed_networks=settings.SEVERINO_MCP_ALLOWED_NETWORKS,
    allowed_origins=settings.SEVERINO_MCP_ALLOWED_ORIGINS,
    verifier=mcp_verifier,
    gate=agents_allowed,
    on_denied=record_mcp_denial,
    observer=observe_agent,
)


@contextlib.asynccontextmanager
async def bridge_serving():
    """The controller bridge, on its own listener for as long as the web application runs.

    The bridge application is given to that listener and to nothing below: it
    is not a route here, so no network request reaches it. A socket path that
    cannot be served safely stops the process from starting.
    """

    if not settings.SEVERINO_BRIDGE_SOCKET:
        yield
        return

    async with unix_server.serving(bridge_application, settings.SEVERINO_BRIDGE_SOCKET):
        yield


@contextlib.asynccontextmanager
async def deriving_ahead():
    """Derived answers asked again as their inputs change, for as long as the
    web application runs, so a request finds its answer stored."""

    with keeping():
        yield


@contextlib.asynccontextmanager
async def ringing_when_due():
    """Run the doorbell (hq.platform.core.doorbell) for the application's lifetime."""

    with doorbell.ringing_when_due():
        yield


@contextlib.asynccontextmanager
async def keeping_signing_keys():
    """Hold the identity provider's signing keys, read outside every request."""

    with api_security.keeping_keys():
        yield


@contextlib.asynccontextmanager
async def lifespan(app):
    async with (
        mcp_served.sessions.run(),
        bridge_serving(),
        deriving_ahead(),
        ringing_when_due(),
        keeping_signing_keys(),
    ):
        yield


application = Starlette(
    routes=[
        Mount("/mcp", app=mcp_application),
        # Collected assets, on the native async path and before the Django stack.
        Mount(settings.STATIC_URL.rstrip("/"), app=static_application),
        Mount("/", app=django_application),
    ],
    lifespan=lifespan,
)
