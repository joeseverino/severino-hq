"""ASGI entrypoint for the HQ web UI and tailnet-only MCP endpoint."""

import contextlib
import os

from django.conf import settings
from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount

from core.headers import LowercaseHeaders
from core.network import TrustedNetworkASGI
from core.static import CachedStaticFiles

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_application = get_asgi_application()
# Wrapped, because this mount sits above the Django stack and so never reaches
# the middleware that refuses untrusted callers everywhere else.
static_application = TrustedNetworkASGI(
    GZipMiddleware(
        CachedStaticFiles(directory=settings.STATIC_ROOT, check_dir=False),
        minimum_size=500,
    )
)
# LowercaseHeaders inside the compressor, not outside it: the compressor has to
# see names it can match, and by the time the response leaves it the damage
# would already be two Content-Lengths.
compressed_django_application = GZipMiddleware(
    LowercaseHeaders(django_application),
    minimum_size=1000,
)

from asgiref.sync import sync_to_async  # noqa: E402

from application.agent_access import agents_paused  # noqa: E402
from application.agent_registry import observe  # noqa: E402
from application.denials import record_denial  # noqa: E402
from hq_api import security as api_security  # noqa: E402
from hq_mcp.identity import token_principal  # noqa: E402
from hq_mcp.security import MCPBoundary  # noqa: E402
from hq_mcp.server import mcp  # noqa: E402


def verify_agent_token(bearer: str):
    """Turn a Pocket ID access token into the agent that presented it."""

    return token_principal(api_security.verify(bearer))


# Wired only when the machine API is configured. `is_configured` is false when
# `SEVERINO_API_RESOURCE` is empty, and without a resource to check `aud`
# against, a token minted for any other API on the same Pocket ID instance
# would verify here on signature alone. Absent means off, never "accept
# anything" -- and with no other credential, off means MCP is disabled.
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
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


application = Starlette(
    routes=[
        Mount("/mcp", app=mcp_application),
        # Serve collected assets on the native async path. WhiteNoise remains
        # the WSGI fallback, but its synchronous iterator never reaches Uvicorn.
        Mount(settings.STATIC_URL.rstrip("/"), app=static_application),
        Mount("/", app=compressed_django_application),
    ],
    lifespan=lifespan,
)
