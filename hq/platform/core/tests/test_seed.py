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
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.projects.models import Project
from hq.platform.application import pins
from hq.platform.application.linked_accounts import GITHUB, linked_login
from hq.platform.core.example_estate import FAVORITES, names, records, throwaway_media
from hq.platform.core.management.commands.seed import records_held

PASSWORD = "an-example-password"


def seed(name: str, **options) -> str:
    out = StringIO()
    with throwaway_media(), mock.patch.dict(os.environ, {"DJANGO_SUPERUSER_PASSWORD": PASSWORD}):
        call_command("seed", name, owner="operator", stdout=out, **options)
    return out.getvalue()


class SeedTests(TestCase):
    def test_an_empty_database_gets_the_example_estate(self):
        said = seed("standard")

        self.assertEqual(
            set(Project.objects.values_list("slug", flat=True)), {slug for _name, slug, *_rest in records.PROJECTS}
        )
        self.assertEqual(
            set(DocumentationRecord.objects.values_list("doc_id", flat=True)),
            {doc_id for doc_id, *_rest in records.RUNBOOKS},
        )
        self.assertTrue(ManagedResource.objects.filter(key=names.MACHINE, kind="machine").exists())
        self.assertIn(f"projects.Project ({len(records.PROJECTS)})", said)

    def test_the_operator_it_signs_up_owns_the_estate_as_every_other_tool_draws_it(self):
        seed("showcase")

        operator = authenticate(username="operator", password=PASSWORD)
        self.assertTrue(operator.is_superuser)
        self.assertEqual(set(pins.pinned(operator, pins.SERVICE)), set(FAVORITES))
        self.assertEqual(linked_login(operator, GITHUB), "example")

    def test_the_env_file_holds_the_settings_the_set_is_shown_under(self):
        folder = Path(tempfile.mkdtemp())

        seed("showcase", env_file=folder / "showcase.env")
        self.assertEqual((folder / "showcase.env").read_text(), "SEVERINO_POSTURE=advanced\n")

    def test_the_standard_set_is_shown_under_no_settings_of_its_own(self):
        folder = Path(tempfile.mkdtemp())

        seed("standard", env_file=folder / "standard.env")
        self.assertEqual((folder / "standard.env").read_text(), "")

    def test_the_empty_set_is_its_operator_and_nothing_else(self):
        seed("empty")

        self.assertEqual(records_held(), {})
        self.assertEqual(list(get_user_model().objects.values_list("username", flat=True)), ["operator"])

    def test_an_unknown_name_is_refused_with_the_known_ones(self):
        with self.assertRaisesMessage(CommandError, "showcase"):
            seed("no-such-set")

        self.assertFalse(get_user_model().objects.exists())

    def test_a_database_that_holds_a_record_is_refused_and_left_as_it_was(self):
        Project.objects.create(name="Already here", slug="already-here", category="homelab", status="active")
        before = records_held()

        with self.assertRaisesMessage(CommandError, "already holds records") as refused:
            seed("showcase")

        self.assertIn("projects.Project (1)", str(refused.exception))
        self.assertEqual(records_held(), before)
        self.assertFalse(get_user_model().objects.exists())
