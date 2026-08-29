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


rename_tailnet_keys = import_module(
    "control_plane.migrations.0015_tailnet_device_keys_say_what_they_are"
)


class _RealApps:
    """The live models, for a migration whose logic is model-shaped."""

    @staticmethod
    def get_model(app_label, model_name):
        from django.apps import apps

        return apps.get_model(app_label, model_name)


class TailnetKeyRenameTests(TestCase):
    """A key that recorded arrival order becomes one that says what it is.

    The migration runs once against real rows, so it runs here first: the
    interesting cases are a device already filed under a collision suffix, the
    audit trail that references it by string, and a target key that is occupied.
    """

    def device(self, key, name):
        return ManagedResource.objects.create(
            key=key, kind="tailscale.device", spec={"name": name}
        )

    def test_a_suffixed_device_is_renamed_to_say_what_it_is(self):
        ManagedResource.objects.create(key="box", kind="machine", spec={"name": "box"})
        self.device("box-2", "box")

        rename_tailnet_keys.forwards(_RealApps, None)

        self.assertTrue(ManagedResource.objects.filter(key="box-tailnet").exists())
        self.assertFalse(ManagedResource.objects.filter(key="box-2").exists())
        # The machine keeps the plain name it always had.
        self.assertEqual(ManagedResource.objects.get(key="box").kind, "machine")

    def test_the_audit_trail_follows_the_rename(self):
        """Audit references a resource by string so history outlives deletion.

        A rename that ignored it would sever every record of what was done to
        these devices, which is worse than the key it set out to fix.
        """

        from core.models import AuditLog

        self.device("box-2", "box")
        AuditLog.objects.create(
            action="update", object_type="ManagedResource",
            object_id="box-2", object_repr="box-2",
        )

        rename_tailnet_keys.forwards(_RealApps, None)

        self.assertTrue(
            AuditLog.objects.filter(object_type="ManagedResource", object_id="box-tailnet").exists()
        )

    def test_an_occupied_target_is_left_alone(self):
        """A rename onto an existing key would break the uniqueness it restores."""

        self.device("box-2", "box")
        ManagedResource.objects.create(
            key="box-tailnet", kind="machine", spec={"name": "something-else"}
        )

        rename_tailnet_keys.forwards(_RealApps, None)

        self.assertTrue(ManagedResource.objects.filter(key="box-2").exists())

    def test_it_is_reversible(self):
        self.device("box-2", "box")

        rename_tailnet_keys.forwards(_RealApps, None)
        rename_tailnet_keys.backwards(_RealApps, None)

        self.assertTrue(ManagedResource.objects.filter(key="box").exists())

    def test_a_device_with_no_name_is_left_alone(self):
        ManagedResource.objects.create(key="nameless", kind="tailscale.device", spec={})

        rename_tailnet_keys.forwards(_RealApps, None)

        self.assertTrue(ManagedResource.objects.filter(key="nameless").exists())

    def test_a_display_name_becomes_a_key_a_url_can_carry(self):
        """The mistake this nearly shipped with.

        A device name is a display string — "Joseph's MacBook Pro" — and a key
        appears in a URL. Renaming without slugifying turned a working key into
        one the resource route cannot match, which is worse than the suffix it
        set out to remove.
        """

        import re

        self.device("josephs-macbook-pro", "Joseph’s MacBook Pro")

        rename_tailnet_keys.forwards(_RealApps, None)

        key = ManagedResource.objects.get(kind="tailscale.device").key
        self.assertEqual(key, "josephs-macbook-pro-tailnet")
        self.assertTrue(re.fullmatch(r"[-a-zA-Z0-9_]+", key))

    def test_a_name_with_dots_separates_rather_than_running_together(self):
        self.device("box", "box.example.com")

        rename_tailnet_keys.forwards(_RealApps, None)

        self.assertEqual(
            ManagedResource.objects.get(kind="tailscale.device").key,
            "box-example-com-tailnet",
        )
