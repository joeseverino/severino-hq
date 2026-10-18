"""The core data migrations, run over the rows each one meets."""

from django.utils import timezone

from .migration_steps import MigrationStepTestCase


class ForgottenRowsTests(MigrationStepTestCase):
    def test_marks_keyed_by_content_and_the_kept_inbox_copy_are_dropped(self):
        apps = self.migrate(("core", "0009_upstream_reading"))
        person = apps.get_model("auth", "User").objects.create(username="example")
        apps.get_model("core", "ActionItemRead").objects.create(user=person, key="0" * 64)

        apps = self.migrate(("core", "0010_action_item_read_by_identity"))
        self.assertFalse(apps.get_model("core", "ActionItemRead").objects.exists())

        apps = self.migrate(("core", "0017_forget_the_admin"))
        Reading = apps.get_model("core", "UpstreamReading")
        for key in ("contacts.unread", "example.kept"):
            Reading.objects.create(key=key, value={"names": ["Example Person"]}, observed_at=timezone.now())

        apps = self.migrate(("core", "0018_forget_the_contact_copy"))
        self.assertEqual(
            list(apps.get_model("core", "UpstreamReading").objects.values_list("key", flat=True)), ["example.kept"]
        )
