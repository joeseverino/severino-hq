"""ASGI entrypoint for the HQ web UI and tailnet-only MCP endpoint."""

import contextlib
import os

from django.conf import settings
from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.routing import Mount

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_application = get_asgi_application()

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
        Mount("/", app=django_application),
    ],
    lifespan=lifespan,
)
