"""A Caddy route is one hostname and one upstream, never configuration of its own."""

from __future__ import annotations

from django.test import SimpleTestCase

from hq.domains.control_plane.providers import validate_spec


# Each would close the route's block and open another: a second site serving
# the edge's filesystem, or an import of a file on the edge.
INJECTED_UPSTREAMS = (
    "app:8080\n}\n:8443 {\n\tfile_server browse {\n\t\troot /\n\t}",
    "app:8080 {",
    "app:8080\r\nimport /etc/passwd",
    "app:8080 app:9090",
    '"app:8080"',
)
INJECTED_DOMAINS = (
    "a.example.com\n}\n:9000 {\n\trespond hi",
    "a.example.com {",
    "a.example.com, b.example.com",
)


class RouteValueTests(SimpleTestCase):
    def test_ordinary_routes_are_accepted(self):
        for domain, upstream in (
            ("app.example.com", "app:8080"),
            ("*.example.com", "127.0.0.1:8000"),
            ("app.example.com", "http://app:3000"),
            ("app.example.com", "https://origin.example.com"),
        ):
            with self.subTest(domain=domain, upstream=upstream):
                spec = validate_spec("caddy.route", {"connection_ref": "example-edge", "domain": domain, "upstream": upstream})
                self.assertEqual(spec["upstream"], upstream)

    def test_an_upstream_that_would_write_directives_is_refused(self):
        for upstream in INJECTED_UPSTREAMS:
            with self.subTest(upstream=upstream), self.assertRaises(ValueError):
                validate_spec("caddy.route", {"connection_ref": "example-edge", "domain": "a.example.com", "upstream": upstream})

    def test_a_domain_that_would_write_directives_is_refused(self):
        for domain in INJECTED_DOMAINS:
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                validate_spec("caddy.route", {"connection_ref": "example-edge", "domain": domain, "upstream": "app:8080"})
