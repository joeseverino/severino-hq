"""ASGI entrypoint for the HQ web UI and tailnet-only MCP endpoint."""

import contextlib
import os

from django.conf import settings
from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount

from core.headers import LowercaseHeaders
from core.static import CachedStaticFiles

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_application = get_asgi_application()
static_application = GZipMiddleware(
    CachedStaticFiles(directory=settings.STATIC_ROOT, check_dir=False),
    minimum_size=500,
)
# LowercaseHeaders inside the compressor, not outside it: the compressor has to
# see names it can match, and by the time the response leaves it the damage
# would already be two Content-Lengths.
compressed_django_application = GZipMiddleware(
    LowercaseHeaders(django_application),
    minimum_size=1000,
)

from hq_mcp.security import MCPBoundary  # noqa: E402
from hq_mcp.server import mcp  # noqa: E402

mcp_application = MCPBoundary(
    mcp.streamable_http_app(),
    token=settings.SEVERINO_MCP_TOKEN,
    allowed_hosts=settings.SEVERINO_MCP_ALLOWED_HOSTS,
    allowed_networks=settings.SEVERINO_MCP_ALLOWED_NETWORKS,
    allowed_origins=settings.SEVERINO_MCP_ALLOWED_ORIGINS,
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
