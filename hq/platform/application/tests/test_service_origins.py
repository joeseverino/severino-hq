"""Where a service is served from: how its origin is resolved, where that came from, and how it is worded."""

from django.test import TestCase
from django.urls import reverse
from django.utils.html import strip_tags

from hq.platform.core.testing import declare, operator

from .service_world import declare_world, healthy


class OriginProvenanceTests(TestCase):
    """A page must not describe an ingress a name does not have."""

    def setUp(self):
        declare_world(containers=True)

    def _page(self, hostname="app.example.com"):
        operator(self.client)
        return self.client.get(reverse("control_plane:service", kwargs={"hostname": hostname}))

    def test_a_proxied_name_still_says_its_ingress_forwards(self):
        healthy(
            declare(
                "app-proxy",
                "npm.proxy_host",
                {
                    "domain_names": ["app.example.com"],
                    "forward_scheme": "http",
                    "forward_host": "10.0.0.10",
                    "forward_port": 6379,
                },
            )
        )

        response = self._page()

        self.assertIn("10.0.0.10:6379", strip_tags(response.content.decode()))
