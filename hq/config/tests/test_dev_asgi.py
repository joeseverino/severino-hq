"""A development server's reloaded worker brings the schema up to the code before it serves."""

import asyncio
import sys
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError
from django.test import TestCase, override_settings

from hq.config import asgi, dev_asgi
from hq.platform.core.health_views import shipped_migrations

NEW = ("example", "0001_from_this_reload")


def loaded_by_the_worker():
    """The factory called as uvicorn calls it: inside the worker's running event loop."""

    async def load():
        return dev_asgi.migrated_application()

    return asyncio.run(load())


class MigratedApplicationTests(TestCase):
    def test_a_migration_the_reload_brought_is_applied_before_the_application_is_served(self):
        with (
            patch.object(dev_asgi, "shipped_migrations", return_value=shipped_migrations() | {NEW}),
            patch.object(dev_asgi, "call_command") as command,
        ):
            served = loaded_by_the_worker()

        command.assert_called_once()
        self.assertEqual(command.call_args.args, ("migrate",))
        self.assertIs(served, asgi.application)

    def test_nothing_new_serves_without_migrating(self):
        with patch.object(dev_asgi, "call_command") as command:
            served = loaded_by_the_worker()

        command.assert_not_called()
        self.assertIs(served, asgi.application)

    def test_a_migration_that_fails_stops_the_worker(self):
        with (
            patch.object(dev_asgi, "shipped_migrations", return_value=shipped_migrations() | {NEW}),
            patch.object(dev_asgi, "call_command", side_effect=CommandError("conflicting migrations")),
            self.assertRaises(CommandError),
        ):
            loaded_by_the_worker()


LINK = "http://127.0.0.1:8123/dev/sign-in/example/"


async def lifespan_only(_scope, receive, send):
    """An application whose whole life is its lifespan."""

    await receive()
    await send({"type": "lifespan.startup.complete"})
    await receive()
    await send({"type": "lifespan.shutdown.complete"})


def log_of_a_start(served) -> list[str]:
    """What a server's log reads once it has started ``served`` and stopped it, uvicorn's own line included.

    Uvicorn writes its line once the application's startup message wakes it,
    as this server does.
    """

    said = StringIO()

    async def run():
        started = asyncio.Event()
        messages: asyncio.Queue = asyncio.Queue()

        async def send(message):
            if message["type"] == "lifespan.startup.complete":
                started.set()

        async def server():
            await started.wait()
            print("INFO:     Application startup complete.", file=sys.stderr)
            await messages.put({"type": "lifespan.shutdown"})

        await messages.put({"type": "lifespan.startup"})
        await asyncio.gather(served({"type": "lifespan"}, messages.get, send), server())

    with redirect_stderr(said):
        asyncio.run(run())
    return said.getvalue().splitlines()


@override_settings(SEVERINO_SCRATCH_ACCOUNT="operator")
class SignInLinkTests(TestCase):
    def log_of(self, **env: str) -> list[str]:
        with (
            patch.dict("os.environ", env),
            patch.object(dev_asgi, "application", lifespan_only),
            patch.object(dev_asgi, "sign_in_link", return_value=LINK),
        ):
            served = loaded_by_the_worker()
        return log_of_a_start(served)

    def test_the_scratch_servers_log_ends_on_its_sign_in_link(self):
        log = self.log_of(HQ_DEV_SEED="showcase")

        self.assertEqual(log[-1], f"Open: {LINK}")
        self.assertIn("Application startup complete.", log[-2])

    def test_an_empty_database_is_named_with_the_command_that_fills_it(self):
        log = self.log_of(HQ_DEV_SEED="empty")

        self.assertEqual(log[-1], f"Open: {LINK}")
        self.assertIn("Empty database", log[-2])
        self.assertIn("mise run dev:scratch --seed", log[-2])
