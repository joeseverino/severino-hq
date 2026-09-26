"""Every page GET is one read projection; a write is not."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from control_plane.models import ManagedResource


class ProjectionMiddlewareTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="app-proxy", kind="npm.proxy_host",
            spec={"domain_names": ["app.example.com"], "forward_host": "10.0.0.5",
                  "forward_port": 8080},
            enabled=True,
        )

    def counted(self, method, url):
        from application import machines

        with mock.patch.object(
            machines, "machine_catalog", wraps=machines.machine_catalog
        ) as catalog:
            getattr(self.client, method)(url)
        return catalog.call_count

    def test_a_resource_page_builds_the_machine_catalogue_once(self):
        url = reverse("control_plane:detail", kwargs={"key": "app-proxy"})

        self.assertEqual(self.counted("get", url), 1)
