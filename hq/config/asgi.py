"""ASGI entrypoint for the HQ web UI and tailnet-only MCP endpoint.

The controller bridge is started from here and served elsewhere: on a private
Unix socket, never on this application's listener.
"""

import asyncio
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


class StartedOnDemand:
    """An ASGI application built and started by its first request, and stopped with the lifespan.

    The MCP SDK and the tool contract it serves are a large share of the
    process's memory and start time, and most runs (a development server, a
    page pass, a gate) never take an MCP request.
    """

    def __init__(self, serving):
        self._serving = serving
        self._open = False
        self._ready: asyncio.Future | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @contextlib.asynccontextmanager
    async def running(self):
        self._open = True
        try:
            yield
        finally:
            self._open = False
            self._stop.set()
            if self._task is not None:
                await self._task
            self._task = self._ready = None
            self._stop = asyncio.Event()

    async def __call__(self, scope, receive, send):
        if not self._open:
            raise RuntimeError("an on-demand application is served only inside the application's lifespan")
        if self._ready is None:
            # No await between the check and these: the first request alone starts it.
            self._ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(self._serve(self._ready))
        application = await asyncio.shield(self._ready)
        await application(scope, receive, send)

    async def _serve(self, ready: asyncio.Future) -> None:
        # Entered and left in this one task, as a task group inside it requires.
        try:
            async with self._serving() as application:
                ready.set_result(application)
                await self._stop.wait()
        except Exception as error:
            if ready.done():
                raise
            # Every request waiting on the start fails with it, and the next one starts it again.
            ready.set_exception(error)
            self._ready = self._task = None


@contextlib.asynccontextmanager
async def mcp_serving():
    served = mcp.served()
    async with served.sessions.run():
        yield served.application


mcp_on_demand = StartedOnDemand(mcp_serving)
mcp_application = MCPBoundary(
    mcp_on_demand,
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
async def lifespan(_app):
    async with (
        mcp_on_demand.running(),
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
