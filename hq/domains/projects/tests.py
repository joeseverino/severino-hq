"""The projects list and a project's page: what they call things and where they lead."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.plugins import NavigationItem
from hq.platform.application.plugin_testing import ComposedPluginTestCase, sibling
from hq.platform.application.projects import hq_sections, refresh_summary


def _document(doc_id, title, doc_type="runbook", status="active"):
    return DocumentationRecord.objects.create(doc_id=doc_id, title=title, doc_type=doc_type, status=status)


class ProjectPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("project-reader"))
        self.project = Project.objects.create(name="Example Tool", slug="example-tool")
        self.other = Project.objects.create(name="Example Site", slug="example-site")

    def page(self):
        return self.client.get(reverse("projects:detail", args=[self.project.slug]))

    def test_documents_are_grouped_by_type_and_named_by_title(self):
        for record in (
            _document("rb-example-deploy", "Deploy the example tool"),
            _document("task-example-open", "Write the example guide", "task", "open"),
            _document("task-example-done", "Ship the example tool", "task", "done"),
            _document("task-example-dropped", "Port the example tool", "task", "wontfix"),
        ):
            record.related_projects.add(self.project)

        response = self.page()

        self.assertContains(response, '<h3>Runbooks <span class="heading-aside">1</span></h3>', html=True)
        self.assertContains(response, '<h3>Open tasks <span class="heading-aside">1</span></h3>', html=True)
        # Closed tasks wait behind their count.
        self.assertContains(
            response,
            '<details class="section-fold"><summary><h3>Done tasks <span class="heading-aside">2</span></h3></summary>',
        )
        self.assertContains(response, 'title="rb-example-deploy">Deploy the example tool</a>')
        # The id is on hover, never the words of the link.
        self.assertNotContains(response, "rb-example-deploy ·")
        self.assertNotContains(response, ">task-example-open")

    def test_a_project_note_links_the_project_and_its_own_note_is_left_out(self):
        for record in (
            _document("project-example-tool", "Example Tool", "architecture_note"),
            _document("project-example-site", "Example Site notes", "architecture_note"),
            _document("project-example-unregistered", "A project HQ does not hold", "architecture_note"),
        ):
            record.related_projects.add(self.project)

        response = self.page()

        self.assertContains(response, "<h3>Related projects</h3>")
        self.assertContains(response, f'<a href="{self.other.get_absolute_url()}" data-entity="Project">Example Site</a>')
        self.assertNotContains(response, "Example Site notes")
        self.assertNotContains(response, 'title="project-example-tool"')
        # A note for a project HQ does not hold stays a document.
        self.assertContains(response, "A project HQ does not hold")

    def test_related_records_are_named_once_and_linked(self):
        item = ContentItem.objects.create(title="Building the example tool", slug="building-example-tool")
        item.related_projects.add(self.project)
        expense = Expense.objects.create(
            date="2030-01-02", vendor="Example Host", item="Hosting", total_cost=Decimal("12.00"),
            related_project=self.project,
        )

        response = self.page()

        self.assertContains(response, "<h3>Writeups and pages</h3>")
        self.assertNotContains(response, "Content items")
        self.assertContains(response, f'href="{item.get_absolute_url()}"')
        self.assertContains(response, f'<a href="{expense.get_absolute_url()}" data-entity="Expense">Example Host · Hosting</a>')

    def test_a_project_with_nothing_linked_says_so_plainly(self):
        self.assertContains(self.page(), "Nothing is linked to this project yet.")

    def test_a_repository_github_is_not_read_for_is_still_a_link(self):
        self.project.repository_url = "https://github.com/example/tool"
        self.project.save()

        response = self.page()

        self.assertContains(response, 'href="https://github.com/example/tool"')
        self.assertContains(response, "github.com/example/tool</a>")
        self.assertContains(response, "Edited ")


class ProjectSectionTests(ComposedPluginTestCase, TestCase):
    """An extension names the repository it is built from, and so does a project."""

    siblings = (
        sibling(
            identifier="example.alpha",
            name="Alpha",
            navigation=(NavigationItem("Projects", "projects:list", "projects"),),
        ),
    )

    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_user("section-reader"))

    def test_the_section_of_hq_built_from_the_repository_is_one_tap_away(self):
        built = Project.objects.create(
            name="Example Alpha", slug="example-alpha", repository_url="https://github.com/example/example-alpha"
        )
        other = Project.objects.create(
            name="Example Tool", slug="example-tool", repository_url="https://github.com/example/tool"
        )

        self.assertEqual(hq_sections(built), (("Alpha", reverse("projects:list")),))
        self.assertEqual(hq_sections(other), ())
        response = self.client.get(reverse("projects:detail", args=[built.slug]))
        self.assertContains(response, f'<a href="{reverse("projects:list")}">Alpha in HQ</a>')

    def test_an_extension_names_the_project_it_is_built_from(self):
        from hq_sdk.pages import built_from

        Project.objects.create(name="Example Tool", slug="example-tool", repository_url="https://github.com/example/tool")
        self.assertIsNone(built_from("example.alpha"))
        built = Project.objects.create(
            name="Example Alpha", slug="example-alpha", repository_url="https://github.com/Example/example-alpha.git"
        )

        link = built_from("example.alpha")

        self.assertEqual((link.label, link.url), ("Example Alpha", built.get_absolute_url()))
        self.assertIsNone(built_from("example.missing"))


class ProjectListTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("project-lister"))

    def test_a_slug_that_only_repeats_the_name_is_not_shown(self):
        Project.objects.create(name="Example", slug="example", technologies_used="Go, SQLite,Django,htmx, CSS")
        Project.objects.create(name="Example Tool", slug="tool")

        response = self.client.get(reverse("projects:list"))

        self.assertNotContains(response, '<span class="muted">example</span>')
        self.assertContains(response, '<span class="muted">tool</span>')
        # The first three technologies, the rest behind the row's toggle.
        self.assertContains(response, '<span class="muted row-less">+2</span>')
        self.assertContains(response, "Edited ")

    def test_filters_and_sorts_say_what_they_do(self):
        response = self.client.get(reverse("projects:list"))

        for label in ("Active, nothing written yet", "No writeup", "No documents", "Status Z–A", "Category A–Z"):
            self.assertContains(response, label)
        for gone in ("Needs output", "Missing docs", "reverse"):
            self.assertNotContains(response, gone)


class RefreshNoteTests(TestCase):
    def test_a_finished_read_says_what_it_read(self):
        said = refresh_summary(
            {
                "content": {"ok": True, "total": 3, "created": 1, "updated": 2},
                "github_app": None,
                "github": {"ok": True},
            }
        )

        self.assertEqual(
            said, "Read 3 writeups and pages from the site (1 new, 2 changed). Read the last push from GitHub."
        )
