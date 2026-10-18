"""The content data migrations, run over the rows each one meets."""

from hq.platform.core.tests.migration_steps import MigrationStepTestCase


class StatusCollapseTests(MigrationStepTestCase):
    def test_every_working_state_becomes_a_draft_and_the_rest_stand(self):
        apps = self.migrate(("content", "0002_initial"))
        Item = apps.get_model("content", "ContentItem")
        before = ("idea", "researching", "drafting", "editing", "published", "archived")
        for status in before:
            Item.objects.create(title=status, slug=status, status=status)

        apps = self.migrate(("content", "0003_collapse_status_and_add_page_type"))

        after = dict(apps.get_model("content", "ContentItem").objects.values_list("slug", "status"))
        self.assertEqual(
            after,
            {
                "idea": "draft",
                "researching": "draft",
                "drafting": "draft",
                "editing": "draft",
                "published": "published",
                "archived": "archived",
            },
        )
