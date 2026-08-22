"""The one-way move from an authored document to declarations HQ owns.

A data migration runs once, against real rows, and cannot be tried again. This
runs it here instead -- on a snapshot shaped exactly like the one it will meet --
so that what it produces is something to read before it is something to undo.

The fixture is the shape, not the deployment: names, roles and addresses are
made up, and the ranges are the ones reserved for writing about addresses.
"""

from __future__ import annotations

from importlib import import_module

from django.test import TestCase

from control_plane.models import ManagedResource

# A migration module's name starts with a digit, so it is reached by import
# rather than named in a from-import.
own_the_topology = import_module(
    "control_plane.migrations.0012_hq_owns_the_topology"
).own_the_topology


SNAPSHOT = {
    "version": 3,
    "hosts": [
        {
            "id": "a-docker-host",
            "role": "Docker host · every container · DNS server",
            "lan_ip": "192.0.2.10",
            "ts_ip": "100.64.0.10",
            "containers": [
                {"id": "proxy", "ports": "80, 443, 81"},
                {"id": "app", "ports": "8000"},
            ],
        },
        {
            "id": "an-edge-host",
            "role": "Cloud outpost · exit node",
            "public_ip": "198.51.100.10",
            "containers": [{"id": "caddy", "ports": "443"}],
        },
        {"id": "a-printer", "role": "Network printer"},
    ],
    "pki": [
        {"id": "a-root-ca", "kind": "internal-ca"},
        {
            "id": "a-wildcard",
            "certificate_name": "example",
            "domains": ["example.com", "*.example.com"],
        },
    ],
    "externals": [{"id": "a-shared-host", "connection_ref": "a-shared-host"}],
    "dependencies": [
        {
            "from": "container:a-docker-host/proxy",
            "relation": "consumes",
            "to": "pki:a-wildcard",
            "attributes": {
                "kind": "npm",
                "connection_ref": "a-proxy",
                "name": "example_wildcard",
                "discover_covered_hosts": True,
                "verify_domains": [],
            },
        },
        {
            "from": "container:an-edge-host/caddy",
            "relation": "consumes",
            "to": "pki:a-wildcard",
            "attributes": {
                "kind": "caddy",
                "connection_ref": "an-edge",
                "name": "edge-caddy",
                "certificate_directory": "/opt/apps/caddy/certs",
                "verify_domains": ["health.example.com"],
            },
        },
        {
            "from": "external:a-shared-host",
            "relation": "consumes",
            "to": "pki:a-wildcard",
            "attributes": {
                "kind": "cpanel",
                "connection_ref": "a-shared-host",
                "name": "a-shared-host",
                "install_domains": ["quiz.example.com", "*.example.com"],
                "verify_domains": ["quiz.example.com"],
            },
        },
    ],
}


class Apps:
    """Just enough of the migration's ``apps`` to run it against live models."""

    @staticmethod
    def get_model(app_label, name):
        from control_plane import models

        if name == "TopologySnapshot":
            return _Snapshot
        return getattr(models, name)


class _Snapshot:
    """The table the migration reads and then drops.

    Stood in for rather than resurrected: the model is gone from ``models.py``,
    and a test that re-declared it would be asserting against something that
    does not exist by the time the migration finishes.
    """

    payload = SNAPSHOT

    class objects:
        @staticmethod
        def filter(**_):
            return _Snapshot.objects

        @staticmethod
        def first():
            return _Snapshot


class TopologyHandoverTests(TestCase):
    def setUp(self):
        ManagedResource.objects.create(
            key="a-wildcard",
            kind="tls.certificate",
            spec={"topology_ref": "pki:a-wildcard", "renewal_window_days": 30},
        )
        ManagedResource.objects.create(
            key="a-docker-host-proxy",
            kind="portainer.container",
            spec={
                "connection_ref": "a-portainer",
                "host": "a-docker-host",
                "name": "proxy",
            },
        )
        own_the_topology(Apps, None)

    def spec(self, key):
        return ManagedResource.objects.get(key=key).spec

    def test_every_host_becomes_a_machine_with_its_addresses(self):
        self.assertEqual(
            self.spec("a-docker-host"),
            {
                "name": "a-docker-host",
                "role": "Docker host · every container · DNS server",
                "addresses": ["192.0.2.10", "100.64.0.10"],
            },
        )

    def test_a_machine_nothing_reaches_is_kept(self):
        """The printer is why declarations exist: nothing will ever sweep it."""

        self.assertEqual(self.spec("a-printer")["role"], "Network printer")

    def test_a_declared_container_learns_the_ports_it_answers_on(self):
        self.assertEqual(self.spec("a-docker-host-proxy")["serves_ports"], [80, 443, 81])

    def test_a_container_hq_does_not_watch_is_not_created_by_this(self):
        """A port list is not a reason to start watching something."""

        self.assertFalse(
            ManagedResource.objects.filter(key__endswith="-caddy").exists()
        )

    def test_the_certificate_states_its_own_names_and_targets(self):
        self.assertEqual(
            self.spec("a-wildcard"),
            {
                "certificate_name": "example",
                "domains": ["example.com", "*.example.com"],
                "install_on": ["a-proxy", "an-edge", "a-shared-host"],
                "renewal_window_days": 30,
            },
        )

    def test_each_target_keeps_the_name_it_is_already_installed_under(self):
        """Renaming one would put a second certificate beside the first."""

        self.assertEqual(
            [
                (self.spec(key)["name"], self.spec(key)["certificate_resource"])
                for key in (
                    "a-proxy-certificate-target",
                    "an-edge-certificate-target",
                    "a-shared-host-certificate-target",
                )
            ],
            [
                ("example_wildcard", "a-wildcard"),
                ("edge-caddy", "a-wildcard"),
                ("a-shared-host", "a-wildcard"),
            ],
        )

    def test_a_targets_own_settings_come_across(self):
        self.assertEqual(
            self.spec("an-edge-certificate-target")["certificate_directory"],
            "/opt/apps/caddy/certs",
        )
        self.assertTrue(
            self.spec("a-proxy-certificate-target")["discover_covered_hosts"]
        )

    def test_a_wildcard_is_dropped_from_what_shared_hosting_installs(self):
        """cPanel takes one certificate per name and will not take a wildcard."""

        self.assertEqual(
            self.spec("a-shared-host-certificate-target")["install_domains"],
            ["quiz.example.com"],
        )

    def test_everything_it_wrote_is_valid_to_the_provider_that_owns_it(self):
        """The migration writes specs; nothing else validates them afterwards."""

        from control_plane.providers import validate_spec

        for resource in ManagedResource.objects.all():
            validate_spec(resource.kind, resource.spec)

    def test_the_certificate_resolves_without_the_document(self):
        from application.infrastructure import resolved_spec

        resolved = resolved_spec(ManagedResource.objects.get(key="a-wildcard"))

        self.assertEqual(
            [consumer["name"] for consumer in resolved["consumers"]],
            ["example_wildcard", "edge-caddy", "a-shared-host"],
        )
