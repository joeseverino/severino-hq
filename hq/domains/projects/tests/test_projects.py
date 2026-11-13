"""The projects list and a project's page: what they call things and where they lead."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.plugin_testing import ComposedPluginTestCase, sibling
from hq.platform.application.plugins import NavigationItem
from hq.platform.application.projection import projection_scope
from hq.platform.application.projects import hq_sections, refresh_summary
from hq.platform.application.tests.markup import elements, hrefs, links, select, text, texts, with_class
from hq.platform.application.tests.test_relationships import estate
from hq.platform.application.where_it_runs import where_it_runs


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
            _document("note-example-design", "Design the example tool", "architecture_note"),
        ):
            record.related_projects.add(self.project)

        response = self.page()

        headings = [" ".join(heading.text.split()) for heading in elements(response, "h2")]
        self.assertIn("Runbooks 1", headings)
        self.assertIn("Architecture notes 1", headings)
        (link,) = select(response, "title", "rb-example-deploy")
        self.assertEqual(link.text.strip(), "Deploy the example tool")
        # The id is on hover, never the words of the link.
        self.assertNotContains(response, "rb-example-deploy ·")
        self.assertNotContains(response, ">note-example-design")

    def test_a_project_note_links_the_project_and_its_own_note_is_left_out(self):
        for record in (
            _document("project-example-tool", "Example Tool", "architecture_note"),
            _document("project-example-site", "Example Site notes", "architecture_note"),
            _document("project-example-unregistered", "A project HQ does not hold", "architecture_note"),
        ):
            record.related_projects.add(self.project)

        response = self.page()

        self.assertIn("Related projects 1", [" ".join(heading.text.split()) for heading in elements(response, "h2")])
        self.assertIn(("Example Site", self.other.get_absolute_url()), links(response))
        self.assertNotContains(response, "Example Site notes")
        self.assertNotContains(response, 'title="project-example-tool"')
        # A note for a project HQ does not hold stays a document.
        self.assertContains(response, "A project HQ does not hold")

    def test_related_records_are_named_once_and_linked(self):
        item = ContentItem.objects.create(title="Building the example tool", slug="building-example-tool")
        item.related_projects.add(self.project)
        expense = Expense.objects.create(
            date="2030-01-02",
            vendor="Example Host",
            item="Hosting",
            total_cost=Decimal("12.00"),
            related_project=self.project,
        )

        response = self.page()

        self.assertIn("Writeups and pages 1", [" ".join(heading.text.split()) for heading in elements(response, "h2")])
        self.assertNotContains(response, "Content items")
        self.assertContains(response, f'href="{item.get_absolute_url()}"')
        # The dot between who was paid and for what is drawn, never read.
        self.assertIn(("Example Host Hosting", expense.get_absolute_url()), links(response))

    def test_the_description_is_the_heads_lede_and_its_facts_a_line_of_their_own(self):
        self.project.description = "One place for every example."
        self.project.save()

        response = self.page()

        self.assertEqual(text(response.context["page"].lede), "One place for every example.")
        # The lede is prose at the reading measure; the facts take the head's width.
        self.assertNotIn("One place", text(response.context["page_facts"]))
        self.assertIn("Edited", text(response.context["page_facts"]))
        self.assertNotIn("Description", [heading.words for heading in elements(response, "h2")])

    def test_the_head_says_where_it_runs_and_names_each_place_once(self):
        estate(1)
        self.project.public_url = "https://s0.example.com/"
        self.project.save()
        with projection_scope():
            runs = where_it_runs(self.project.public_url)

        response = self.page()
        head = response.context["page_facts"]

        self.assertIn((runs.machine.label, runs.machine.url), links(head))
        self.assertIn(("Service page", runs.service.url), links(head))
        self.assertIn("https://s0.example.com/", hrefs(head))
        # Said once on the whole page: the head is where it is said.
        page = response.content.decode()
        self.assertEqual(hrefs(page).count(runs.service.url), 1)
        self.assertEqual(hrefs(page).count(runs.machine.url), 1)
        self.assertEqual(text(page).count("s0.example.com"), 1)

    def test_an_address_no_service_answers_at_is_only_the_address(self):
        self.project.public_url = "https://elsewhere.example.org/"
        self.project.save()

        head = self.page().context["page_facts"]

        self.assertEqual([href for href in hrefs(head) if href.startswith("/infrastructure/")], [])
        self.assertIn("https://elsewhere.example.org/", hrefs(head))

    def test_a_project_with_nothing_linked_draws_no_related_card(self):
        self.assertEqual(self.page().context["related"], ())

    def test_a_repository_github_is_not_read_for_is_still_a_link(self):
        self.project.repository_url = "https://github.com/example/tool"
        self.project.save()

        response = self.page()

        self.assertContains(response, 'href="https://github.com/example/tool"')
        self.assertIn(("github.com/example/tool", "https://github.com/example/tool"), links(response))
        self.assertContains(response, "Edited ")


class ProjectFormTests(TestCase):
    def test_the_edit_form_names_each_address_as_a_url(self):
        self.client.force_login(get_user_model().objects.create_user("project-editor", is_superuser=True))
        project = Project.objects.create(name="Example Tool", slug="example-tool")

        labels = texts(self.client.get(reverse("projects:edit", args=[project.slug])), "label")

        self.assertLessEqual({"Repository URL", "Public URL"}, set(labels), labels)


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
        self.assertIn(("Alpha in HQ", reverse("projects:list")), links(response))

    def test_an_extension_names_the_project_it_is_built_from(self):
        from hq_sdk.pages import built_from

        Project.objects.create(
            name="Example Tool", slug="example-tool", repository_url="https://github.com/example/tool"
        )
        self.assertIsNone(built_from("example.alpha"))
        built = Project.objects.create(
            name="Example Alpha", slug="example-alpha", repository_url="https://github.com/Example/example-alpha.git"
        )

        link = built_from("example.alpha")

        self.assertEqual((link.label, link.url), ("Example Alpha", built.get_absolute_url()))
        self.assertIsNone(built_from("example.missing"))

    def test_the_page_frame_says_what_a_page_is_built_from(self):
        from django.template.loader import render_to_string

        from hq.platform.application.pages import Page
        from hq_sdk.pages import built_from

        built = Project.objects.create(
            name="Example Alpha", slug="example-alpha", repository_url="https://github.com/example/example-alpha"
        )

        named = render_to_string("page.html", {"page": Page("Alpha"), "built_from": built_from("example.alpha")})
        unnamed = render_to_string("page.html", {"page": Page("Alpha"), "built_from": built_from("example.missing")})

        (line,) = with_class(named, "section-foot")
        self.assertTrue(line.words.startswith("Built from"))
        self.assertIn(("Example Alpha", built.get_absolute_url()), links(named))
        self.assertEqual(with_class(unnamed, "section-foot"), [])
        self.assertNotIn("Built from", text(unnamed))


class ProjectListTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("project-lister"))

    def test_a_slug_that_only_repeats_the_name_is_not_shown(self):
        Project.objects.create(name="Example", slug="example", technologies_used="Go, SQLite,Django,htmx, CSS")
        Project.objects.create(name="Example Tool", slug="tool")

        response = self.client.get(reverse("projects:list"))

        page = text(response)
        self.assertIn("Example Tool tool", page)
        self.assertNotIn("Example example", page)
        # The first three technologies, the rest behind the row's toggle.
        self.assertIn("Go SQLite Django +2", page)

    def test_every_cell_answers_its_heading(self):
        Project.objects.create(name="Example", slug="example")
        Project.objects.create(name="Example Tool", slug="tool", repository_url="https://github.com/example/tool")

        response = self.client.get(reverse("projects:list"))

        # A heading is its label, then any sort mark.
        headings = [heading.partition(" ")[0] for heading in texts(response, "th")]
        rows = {}
        for row in elements(response, "tr")[1:]:
            cells = [cell.words for cell in elements(response, "td") if row in cell.ancestors]
            rows[cells[headings.index("Name")]] = dict(zip(headings, cells, strict=True))
        # A repository column says only what the repository says; the record's
        # own edit is its own column.
        self.assertEqual(rows["Example"]["Repository"], "")
        self.assertEqual(rows["Example"]["Edited"], rows["Example Tool tool"]["Edited"])
        self.assertEqual(rows["Example Tool tool"]["Repository"], "Not read yet")

    def test_filters_and_sorts_say_what_they_do(self):
        response = self.client.get(reverse("projects:list"))

        for label in ("Active, nothing written yet", "No writeup", "No documents", "Status Z–A", "Category A–Z"):
            self.assertContains(response, label)
        for gone in ("Needs output", "Missing docs", "reverse"):
            self.assertNotIn(gone, text(response))


class ProjectToggleTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("project-toggler"))
        written = ContentItem.objects.create(title="Example writeup", slug="example-writeup")
        documented = _document("rb-example", "Example runbook")
        self.bare = Project.objects.create(name="Bare", slug="bare", status=Project.Status.ACTIVE)
        self.half = Project.objects.create(name="Half", slug="half", status=Project.Status.ACTIVE)
        self.whole = Project.objects.create(name="Whole", slug="whole", status=Project.Status.ACTIVE)
        self.idle = Project.objects.create(name="Idle", slug="idle", status=Project.Status.PAUSED)
        written.related_projects.add(self.half, self.whole)
        documented.related_projects.add(self.whole, self.idle)

    def listed(self, **query):
        response = self.client.get(reverse("projects:list"), query)
        return {project.slug for project in response.context["object_list"]}

    def test_each_toggle_lists_what_it_says(self):
        self.assertEqual(self.listed(needs_output="1"), {"bare", "half"})
        self.assertEqual(self.listed(no_content="1"), {"bare", "idle"})
        self.assertEqual(self.listed(no_docs="1"), {"bare", "half"})
        self.assertEqual(self.listed(no_content="1", no_docs="1"), {"bare"})
        self.assertEqual(self.listed(), {"bare", "half", "whole", "idle"})


class PostureProjectTests(TestCase):
    def test_a_repository_finds_its_project_whatever_the_case_of_its_url(self):
        from hq.platform.application.tests.markup import hrefs
        from hq.platform.application.tests.test_github_estate import store

        self.client.force_login(get_user_model().objects.create_user("posture-reader"))
        project = Project.objects.create(name="Alpha", slug="alpha", repository_url="https://github.com/Example/Alpha")
        store(private=True)

        response = self.client.get(reverse("posture"))

        self.assertIn(project.get_absolute_url(), hrefs(response))


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
