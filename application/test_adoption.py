"""Only a connection that manages adopts, and an operator's "not managed" sticks."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from control_plane.models import ManagedResource, NotManaged, ProviderConnection
from core.models import AuditLog

from .adoption_testing import connection
from .infrastructure import OperationCommand, PolicyError, request_removal
from .inventory import AdoptCommand, adopt, record_connections, unmanaged
from .security import cli_principal
from .sweep import record_sweep
from .zones import find_zone

ZONE_KIND = "cloudflare.zone"
RECORD_KIND = "cloudflare.dns_record"
ZONES = [{"zone": "example.com", "connection_ref": "example-dns"}]
RECORDS = [
    {
        "zone": "example.com",
        "record_id": "r1",
        "name": "www.example.com",
        "record_type": "A",
        "content": "192.0.2.10",
        "priority": None,
        "proxied": False,
        "ttl": 1,
    }
]


def swept(zones=ZONES, records=RECORDS):
    return record_sweep(
        {
            ZONE_KIND: {"ok": True, "records": zones},
            RECORD_KIND: {"ok": True, "records": records},
        },
        principal=cli_principal(),
    )


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class ObservingConnectionTests(TestCase):
    """A connection that only observes adopts nothing."""

    def setUp(self):
        connection("cloudflare_dns", "example-dns", manages=False)

    def test_a_sweep_adopts_no_zone_and_no_record(self):
        result = swept()

        self.assertEqual(result["adopted"], [])
        self.assertFalse(ManagedResource.objects.exists())

    def test_what_it_reads_is_still_shown_as_observed(self):
        swept()

        zone = find_zone("example.com")
        self.assertFalse(zone.managed)
        self.assertTrue(zone.observed_only)
        self.assertEqual(zone.adopt_token, "")
        self.assertEqual(len(zone.records), 1)
        self.assertTrue(all(item.observed_only for item in unmanaged()))

    def test_adopting_by_hand_is_refused(self):
        swept()
        (item,) = [item for item in unmanaged() if item.kind == ZONE_KIND]

        with self.assertRaises(PolicyError):
            adopt(AdoptCommand(kind=ZONE_KIND, token=item.token), principal=cli_principal())
        self.assertFalse(ManagedResource.objects.exists())

    def test_the_domain_page_offers_no_manage_button(self):
        swept()
        user = get_user_model().objects.create_user("operator", password="x" * 20)
        self.client.force_login(user)

        response = self.client.get(reverse("zones:detail", kwargs={"zone": "example.com"}))

        self.assertContains(response, "only observes")
        self.assertNotContains(response, "Manage this domain")

    def test_no_connection_at_all_adopts_nothing(self):
        ProviderConnection.objects.all().delete()

        self.assertEqual(swept()["adopted"], [])


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class ManagingConnectionTests(TestCase):
    def test_a_managing_connection_adopts_the_zone_and_its_records(self):
        connection("cloudflare_dns", "example-dns")

        swept()

        self.assertEqual(
            sorted(ManagedResource.objects.values_list("kind", flat=True)),
            [RECORD_KIND, ZONE_KIND],
        )

    def test_a_record_follows_the_connection_it_names(self):
        connection("cloudflare_dns", "example-dns", manages=False)
        connection("cloudflare_dns", "example-other")

        swept()

        self.assertFalse(ManagedResource.objects.filter(kind=ZONE_KIND).exists())

    def test_a_record_naming_none_needs_every_connection_of_its_kind_to_manage(self):
        connection("cloudflare_dns", "example-dns")
        connection("cloudflare_dns", "example-other", manages=False)
        ManagedResource.objects.create(
            key="example-com",
            kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": "example-dns"},
        )

        swept()

        self.assertFalse(ManagedResource.objects.filter(kind=RECORD_KIND).exists())


class RecordedConnectionTests(TestCase):
    def test_only_an_explicit_true_manages(self):
        record_connections(
            [
                {"connection_ref": "example-a", "provider": "cloudflare_dns", "manages": True},
                {"connection_ref": "example-b", "provider": "cloudflare_dns", "manages": "1"},
                {"connection_ref": "example-c", "provider": "cloudflare_dns"},
            ],
            principal=cli_principal(),
        )

        self.assertEqual(
            dict(ProviderConnection.objects.values_list("connection_ref", "manages")),
            {"example-a": True, "example-b": False, "example-c": False},
        )


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class StopManagingSticksTests(TestCase):
    """Forgetting a domain is not undone by the next sweep."""

    def setUp(self):
        connection("cloudflare_dns", "example-dns")
        swept()
        self.user = get_user_model().objects.create_user("operator", password="x" * 20)
        self.client.force_login(self.user)

    def forget(self):
        key = ManagedResource.objects.get(kind=ZONE_KIND).key
        request_removal(
            OperationCommand(idempotency_key="forget-example"),
            principal=cli_principal(),
            current_key=key,
        )

    def test_a_forgotten_domain_stays_unmanaged_after_a_sweep(self):
        self.forget()

        swept()

        self.assertFalse(ManagedResource.objects.filter(kind=ZONE_KIND).exists())
        self.assertFalse(ManagedResource.objects.filter(kind=RECORD_KIND).exists())
        zone = find_zone("example.com")
        self.assertFalse(zone.managed)
        self.assertTrue(zone.adopt_token)

    def test_the_choice_is_recorded_with_who_and_audited(self):
        self.forget()

        row = NotManaged.objects.get(kind=ZONE_KIND)
        self.assertEqual(row.label, "example.com")
        self.assertTrue(row.actor)
        self.assertTrue(
            AuditLog.objects.filter(
                object_type="Not managed", action=AuditLog.Action.CREATED
            ).exists()
        )

    def test_managing_it_again_adopts_it_and_clears_the_choice(self):
        self.forget()
        swept()
        token = find_zone("example.com").adopt_token

        response = self.client.post(
            reverse("zones:adopt", kwargs={"zone": "example.com"}), {"token": token}
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ManagedResource.objects.filter(kind=ZONE_KIND).exists())
        self.assertFalse(NotManaged.objects.exists())
        self.assertTrue(
            AuditLog.objects.filter(
                object_type="Not managed", action=AuditLog.Action.DELETED
            ).exists()
        )

        swept()

        self.assertTrue(ManagedResource.objects.filter(kind=RECORD_KIND).exists())

    def test_the_removal_page_says_it_stays_unmanaged(self):
        key = ManagedResource.objects.get(kind=ZONE_KIND).key

        response = self.client.get(reverse("control_plane:remove", args=[key]))

        self.assertContains(response, "until you manage it again")


class StopManagingAnyKindTests(TestCase):
    """The same holds for every kind a sweep adopts, not only domains."""

    CONTAINER = {
        "name": "example-web",
        "host": "example-host",
        "connection_ref": "example-portainer",
        "stack": "example-project",
    }

    def sweep(self):
        record_sweep(
            {"portainer.container": {"ok": True, "records": [self.CONTAINER]}},
            principal=cli_principal(),
        )

    def test_a_forgotten_container_stays_unmanaged(self):
        connection("portainer", "example-portainer")
        self.sweep()
        key = ManagedResource.objects.get(kind="portainer.container").key

        request_removal(
            OperationCommand(idempotency_key="forget-container"),
            principal=cli_principal(),
            current_key=key,
        )
        self.sweep()

        self.assertFalse(ManagedResource.objects.filter(kind="portainer.container").exists())
        (item,) = unmanaged()
        adopt(AdoptCommand(kind=item.kind, token=item.token), principal=cli_principal())
        self.assertTrue(ManagedResource.objects.filter(kind="portainer.container").exists())
        self.assertFalse(NotManaged.objects.exists())
