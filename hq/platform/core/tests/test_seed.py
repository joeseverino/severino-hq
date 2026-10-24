"""seed makes a new database a named data set, owned by the operator it signs up, and only there."""

import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import authenticate, get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.domains.projects.models import Project
from hq.platform.application import pins
from hq.platform.core.example_estate import FAVORITES, names, records, throwaway_media
from hq.platform.core.management.commands.seed import records_held


def seed(name: str, **options) -> str:
    out = StringIO()
    with throwaway_media(), mock.patch.dict(os.environ, {"DJANGO_SUPERUSER_PASSWORD": "an-example-password"}):
        call_command("seed", name, owner="operator", stdout=out, **options)
    return out.getvalue()


class SeedTests(TestCase):
    def test_the_operator_owns_the_estate_as_every_tool_draws_it_under_its_settings(self):
        env_file = Path(tempfile.mkdtemp()) / "seed.env"

        said = seed("showcase", env_file=env_file)

        self.assertTrue(ManagedResource.objects.filter(key=names.MACHINE, kind="machine").exists())
        self.assertIn(f"projects.Project ({len(records.PROJECTS)})", said)
        operator = authenticate(username="operator", password="an-example-password")
        self.assertTrue(operator.is_superuser)
        self.assertEqual(pins.pinned(operator, pins.SERVICE), set(FAVORITES))
        self.assertEqual(env_file.read_text(), "SEVERINO_POSTURE=advanced\n")

    def test_the_empty_set_is_its_operator_and_nothing_else(self):
        seed("empty")

        self.assertEqual(records_held(), {})
        self.assertEqual(list(get_user_model().objects.values_list("username", flat=True)), ["operator"])

    def test_a_database_that_holds_a_record_is_refused_and_left_as_it_was(self):
        Project.objects.create(name="Already here", slug="already-here", category="homelab", status="active")
        before = records_held()

        with self.assertRaisesMessage(CommandError, "projects.Project (1)"):
            seed("showcase")

        self.assertEqual(records_held(), before)
        self.assertFalse(get_user_model().objects.exists())
