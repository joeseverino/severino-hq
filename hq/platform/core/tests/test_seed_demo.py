"""seed_demo writes the example estate into an empty database, and only there."""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.projects.models import Project
from hq.platform.core.example_estate import names, records, throwaway_media
from hq.platform.core.management.commands.seed_demo import records_held


def seed() -> str:
    out = StringIO()
    with throwaway_media():
        call_command("seed_demo", stdout=out)
    return out.getvalue()


class SeedDemoTests(TestCase):
    def test_an_empty_database_gets_the_example_estate(self):
        said = seed()

        self.assertEqual(
            set(Project.objects.values_list("slug", flat=True)), {slug for _name, slug, *_rest in records.PROJECTS}
        )
        self.assertEqual(
            set(DocumentationRecord.objects.values_list("doc_id", flat=True)),
            {doc_id for doc_id, *_rest in records.RUNBOOKS},
        )
        self.assertTrue(ManagedResource.objects.filter(key=names.MACHINE, kind="machine").exists())
        self.assertIn(f"projects.Project ({len(records.PROJECTS)})", said)

    def test_an_operator_signed_up_first_is_not_a_record(self):
        get_user_model().objects.create_superuser("operator", "operator@example.com", "unused-password")

        seed()

        self.assertTrue(Project.objects.exists())

    def test_a_database_that_holds_a_record_is_refused_and_left_as_it_was(self):
        Project.objects.create(name="Already here", slug="already-here", category="homelab", status="active")
        before = records_held()

        with self.assertRaisesMessage(CommandError, "already holds records") as refused:
            seed()

        self.assertIn("projects.Project (1)", str(refused.exception))
        self.assertEqual(records_held(), before)
