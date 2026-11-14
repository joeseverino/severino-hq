"""The development servers' application: the production one, on a schema brought up to the code first.

Uvicorn's reloader starts a new worker for each change, and the worker calls
this factory before it takes a connection, so a change that brings a
migration is applied before any page reads the new schema, as the container
migrates at boot. When nothing is new it costs a directory listing and one
query.

On the scratch server it also writes the sign-in link (core/dev_sign_in.py) to
the log once the worker's startup is complete, a new one for each worker, and
says so when its database is empty.
"""

import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor

lazy from django.conf import settings
lazy from django.core.management import call_command
lazy from django.db import connection
lazy from django.db.migrations.recorder import MigrationRecorder
lazy from django.urls import reverse

lazy from hq.config.asgi import application
lazy from hq.platform.core.dev_sign_in import mint, scratch_account
lazy from hq.platform.core.health_views import shipped_migrations
lazy from hq.platform.core.outside_requests import connections_of_its_own


def migrate_new() -> None:
    """Apply what the checkout ships and the database has not recorded, by readiness's own measure."""

    applied = set(MigrationRecorder(connection).migration_qs.values_list("app", "name"))
    if not shipped_migrations() <= applied:
        call_command("migrate", interactive=False, verbosity=1)


# Said when the scratch server was started without --seed.
EMPTY = "Empty database: fill it with mise run dev:scratch --seed showcase"


def sign_in_link() -> str:
    """The scratch server's sign-in link, or "" where none is routed.

    At `localhost`, which the server answers on both loopback stacks
    (tools/dev/serve.py), so a browser that asks for ::1 first loads it too.
    """

    if not settings.SEVERINO_SCRATCH_ACCOUNT:
        return ""
    token = mint(scratch_account())
    return f"http://localhost:{os.environ['UVICORN_PORT']}{reverse('scratch_sign_in', args=[token])}"


def closing_lines() -> tuple[str, ...]:
    """What the scratch server's log ends on: the sign-in link last, after a word on an empty database."""

    link = sign_in_link()
    if not link:
        return ()
    empty = (EMPTY,) if os.environ.get("HQ_DEV_SEED") == "empty" else ()
    return (*empty, f"Open: {link}")


def prepared() -> tuple[str, ...]:
    """The schema migrated, then the closing lines, on a thread Django did not start, so its connection is its own."""

    with connections_of_its_own():
        migrate_new()
        return closing_lines()


class Announced:
    """An ASGI application that writes ``lines`` to the log once its startup is complete."""

    def __init__(self, app, lines: tuple[str, ...]):
        self.app = app
        self.lines = lines

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            return await self.app(scope, receive, send)

        async def sent(message):
            await send(message)
            if message["type"] == "lifespan.startup.complete":
                # Queued behind the server's own "Application startup complete",
                # which the message just released, so these are the last lines.
                asyncio.get_running_loop().call_soon(lambda: print(*self.lines, sep="\n", file=sys.stderr, flush=True))

        return await self.app(scope, receive, sent)


def migrated_application():
    """The ASGI application, once ``prepared`` has run.

    Uvicorn calls a factory inside its running event loop, where Django
    refuses a query, so the migration runs on a thread of its own while the
    worker waits for it, and its failure stops the worker.
    """

    # Loaded first, since loading it sets Django up for the query.
    served = application
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="migrate") as pool:
        lines = pool.submit(prepared).result()
    return Announced(served, lines) if lines else served
