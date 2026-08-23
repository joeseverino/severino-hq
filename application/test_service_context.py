"""The name is the join, and nothing stores the tie twice.

HQ's infrastructure half relates to almost nothing and its knowledge half
relates to everything. What connects them is already on both sides: a project
says where it is published, a document says which system it is about, an audit
entry says which object it changed. These prove the page reads those rather than
asking for a column that points at infrastructure.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from control_plane.models import ManagedResource
from core.models import AuditLog
from projects.models import Project

from .service_context import sections_for
from .services import service_or_prospect


def a_service(hostname="probe.example.com"):
    ManagedResource.objects.create(
        key="probe-dns",
        kind="adguard.rewrite",
        spec={"domain": hostname, "answer": "10.0.0.1"},
        generation=1,
        observed_generation=1,
    )
    return service_or_prospect(hostname)


def a_project(**fields):
    return Project.objects.create(
        **{
            "name": "A Project",
            "slug": "a-project",
            "category": "homelab",
            "status": "active",
            "public_url": "https://probe.example.com",
            **fields,
        }
    )


def by_id(service):
    return {section.id: section for section in sections_for(service)}


class DeliveryTests(TestCase):
    def test_a_project_publishing_this_name_is_the_project_for_it(self):
        """No column points a service at a project. One says where it is
        published, which is the same statement read the other way."""

        a_project()

        self.assertIn("delivery", by_id(a_service()))

    def test_a_project_publishing_a_different_name_is_not(self):
        a_project(public_url="https://elsewhere.example.com")

        self.assertNotIn("delivery", by_id(a_service()))

    def test_the_project_and_the_repository_are_both_links(self):
        a_project(repository_url="https://github.com/example/a-project")

        project, repository, _ = by_id(a_service())["delivery"].records[0]

        self.assertTrue(project.url)
        self.assertEqual(repository.text, "example/a-project")
        self.assertEqual(repository.url, "https://github.com/example/a-project")
        self.assertTrue(repository.external)

    def test_a_project_without_a_repository_says_so_rather_than_linking(self):
        a_project(repository_url="")

        _, repository, _ = by_id(a_service())["delivery"].records[0]

        self.assertEqual(repository.url, "")
        self.assertTrue(repository.muted)


class ActivityTests(TestCase):
    def test_changes_to_the_resources_behind_the_name_are_shown(self):
        service = a_service()
        AuditLog.objects.create(
            action="update", object_type="ManagedResource",
            object_id="probe-dns", object_repr="probe-dns",
        )

        self.assertIn("activity", by_id(service))

    def test_changes_to_something_else_are_not(self):
        """This answers what changed here, not what changed."""

        service = a_service()
        AuditLog.objects.create(
            action="update", object_type="ManagedResource",
            object_id="unrelated", object_repr="unrelated",
        )

        self.assertNotIn("activity", by_id(service))


class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)

    def test_the_page_renders_what_the_registry_produced(self):
        a_project(name="A Project", repository_url="https://github.com/example/a")
        a_service()

        response = self.client.get(
            reverse("control_plane:service",
                    kwargs={"hostname": "probe.example.com"})
        )

        self.assertContains(response, "Delivery")
        self.assertContains(response, "example/a")
        self.assertContains(response, 'aria-label="On this page"')
        self.assertContains(response, 'href="#delivery"')
        self.assertContains(response, 'id="delivery"')
        self.assertContains(response, 'class="page-head-status ', count=1)

    def test_a_service_nothing_else_knows_about_grows_no_bands(self):
        a_service()

        response = self.client.get(
            reverse("control_plane:service",
                    kwargs={"hostname": "probe.example.com"})
        )

        self.assertNotContains(response, "Delivery")
        self.assertNotContains(response, "Recent changes")

    def test_the_registry_is_the_only_list_of_sections(self):
        """A band appears because a resolver produced it, so adding one is a
        function and an entry rather than an edit to the page."""

        from .service_context import SECTIONS

        self.assertEqual(
            [resolve.__name__ for resolve in SECTIONS],
            ["_delivery", "_activity"],
        )
