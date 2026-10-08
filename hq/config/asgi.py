"""ASGI entrypoint for the HQ web UI and tailnet-only MCP endpoint.

The controller bridge is started from here and served elsewhere: on a private
Unix socket, never on this application's listener.
"""

import contextlib
import os

from django.conf import settings
from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount

from hq.platform.core.headers import LowercaseHeaders
from hq.platform.core.network import TrustedNetworkASGI
from hq.platform.core.static import CachedStaticFiles

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")

django_application = get_asgi_application()
# Wrapped, because this mount sits above the Django stack and so never reaches
# the middleware that refuses untrusted callers everywhere else.
# No compressor: the image build compressed each asset once and the mount sends
# that copy.
static_application = TrustedNetworkASGI(
    CachedStaticFiles(directory=settings.STATIC_ROOT, check_dir=False)
)
# LowercaseHeaders inside the compressor, not outside it: the compressor has to
# see names it can match, and by the time the response leaves it the damage
# would already be two Content-Lengths.
compressed_django_application = GZipMiddleware(
    LowercaseHeaders(django_application),
    minimum_size=1000,
)

from asgiref.sync import sync_to_async  # noqa: E402

from hq.platform.api import security as api_security  # noqa: E402
from hq.platform.application.agent_access import agents_paused  # noqa: E402
from hq.platform.application.agent_registry import observe  # noqa: E402
from hq.platform.application.denials import record_denial  # noqa: E402
from hq.platform.mcp.identity import token_principal  # noqa: E402
from hq.platform.mcp.security import MCPBoundary  # noqa: E402
from hq.platform.mcp.server import mcp  # noqa: E402


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


mcp_application = MCPBoundary(
    mcp.streamable_http_app(),
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
    from hq.domains.control_plane.bridge_application import application as bridge_application
    from hq.platform.core.unix_server import serving

    async with serving(bridge_application, settings.SEVERINO_BRIDGE_SOCKET):
        yield


@contextlib.asynccontextmanager
async def deriving_ahead():
    """Derived answers asked again as their inputs change, for as long as the
    web application runs, so a request finds its answer stored."""

    from hq.platform.core.ahead import keeping

    with keeping():
        yield


@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run(), bridge_serving(), deriving_ahead():
        yield


application = Starlette(
    routes=[
        Mount("/mcp", app=mcp_application),
        # Collected assets, on the native async path and before the Django stack.
        Mount(settings.STATIC_URL.rstrip("/"), app=static_application),
        Mount("/", app=compressed_django_application),
    ],
    lifespan=lifespan,
)
