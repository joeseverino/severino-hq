"""A development server's reloaded worker brings the schema up to the code before it serves."""

import asyncio
from unittest.mock import patch

from django.core.management import CommandError
from django.test import TestCase

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
