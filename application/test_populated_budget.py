"""What the dashboard costs once the estate holds something."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.db import connection as database
from django.test import TestCase

from application.security import cli_principal
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from control_plane.models import (
    DashboardConfiguration,
    ManagedResource,
    ProviderConnection,
    ProviderInventory,
    WeatherObservation,
)
from projects.models import Project

from .dashboard import dashboard_highlights, operating_snapshot
from .projection import projection_scope

# The host dashboard (snapshot and highlights, one projection) over ``populate``'s
# machines, services, zones, readings and connections; empty: HOST_QUERY_BUDGET.
POPULATED_QUERY_BUDGET = 27


def _store(kind, records, **extra):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": records,
            "reachable": True,
            "connected": True,
            "observed_at": timezone.now(),
            "controller_id": "example-controller",
            **extra,
        },
    )


def populate(size: int = 8) -> None:
    now = timezone.now()
    Project.objects.create(name="Budget", slug="budget", status=Project.Status.ACTIVE)
    _store(
        "tailscale.device",
        [
            {
                "name": f"example-host-{index}",
                "online": index % 3 != 0,
                "tags": ["tag:server"],
                "addresses": [f"100.64.0.{index + 1}"],
                "last_seen": (now - timedelta(hours=index)).isoformat(),
                "key_expires": (now + timedelta(days=90 + index)).isoformat(),
            }
            for index in range(size)
        ],
    )
    _store(
        "tailscale.policy",
        [
            {
                "record": "policy",
                "settings": {"devicesApprovalOn": True},
                "dns": {"dns": ["100.64.0.1"]},
                "groups": [{"name": "group:admins", "members": ["someone@example.com"]}],
                "grants": [{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:22"]}],
            }
        ],
    )
    _store("tailscale.dns", [{"record": "dns", "nameservers": ["100.64.0.1"]}])
    for zone in ("example.com", "example.net"):
        ManagedResource.objects.create(
            key=zone.replace(".", "-"),
            kind="cloudflare.zone",
            spec={"zone": zone, "connection_ref": "example-dns"},
        )
    _store(
        "cloudflare.zone",
        [
            {
                "zone": zone,
                "connection_ref": "example-dns",
                "registration": {
                    "expires_at": (now + timedelta(days=200)).isoformat(),
                    "auto_renew": True,
                },
            }
            for zone in ("example.com", "example.net")
        ],
    )
    _store(
        "cloudflare.edge_certificate",
        [
            {
                "connection_ref": "example-dns",
                "zone": "example.com",
                "id": "e1",
                "hosts": ["example.com", "*.example.com"],
                "status": "active",
                "expires_on": (now + timedelta(days=60)).isoformat(),
            }
        ],
    )
    _store(
        "portainer.container",
        [
            {"host": f"example-host-{index}", "name": f"app-{index}", "ports": [8000 + index]}
            for index in range(size)
        ],
    )
    for index in range(size):
        ManagedResource.objects.create(
            key=f"example-host-{index}",
            kind="machine",
            spec={"name": f"example-host-{index}", "addresses": [f"100.64.0.{index + 1}"]},
        )
        ManagedResource.objects.create(
            key=f"record-{index}",
            kind="cloudflare.dns_record",
            spec={
                "zone": "example.com",
                "name": f"app{index}.example.com",
                "record_type": "A",
                "content": f"100.64.0.{index + 1}",
                "connection_ref": "example-dns",
            },
            conditions=[{"type": "Ready", "status": True}],
            observed_generation=1,
            last_observed_at=now,
        )
        ProviderConnection.objects.create(
            connection_ref=f"example-host-{index}",
            controller_id="example-controller",
            provider="ssh",
            endpoint=f"100.64.0.{index + 1}:22",
            reachable=index != 1,
            probed=True,
            observed_at=now,
        )
    ManagedResource.objects.create(
        key="example-cert",
        kind="tls.certificate",
        spec={"certificate_name": "example-cert", "domains": ["app0.example.com"]},
        status={"not_after": (now + timedelta(days=20)).isoformat()},
    )
    for ref, provider in (("example-dns", "cloudflare_api"), ("example-tailnet", "tailscale")):
        ProviderConnection.objects.create(
            connection_ref=ref,
            controller_id="example-controller",
            provider=provider,
            endpoint="https://api.example.com",
            reachable=True,
            probed=True,
            observed_at=now,
        )
    DashboardConfiguration.objects.create(pk=1, weather_point="41.8781,-87.6298")
    WeatherObservation.objects.create(
        point="41.8781,-87.6298",
        payload={"metrics": [{"label": "Now", "value": "Clear"}]},
        observed_at=now,
    )


def dashboard_queries() -> list[str]:
    with (
        mock.patch("application.domains.extension_domains", return_value=()),
        mock.patch("application.plugins.plugin_connection_specs", return_value=()),
        mock.patch("contacts.d1.query", side_effect=AssertionError("a page render called D1")),
        CaptureQueriesContext(database) as queries,
        projection_scope(),
    ):
        operating_snapshot(principal=cli_principal())
        dashboard_highlights()
    return [query["sql"] for query in queries.captured_queries]


class PopulatedDashboardBudgetTests(TestCase):
    def test_a_populated_estate_stays_within_its_budget(self):
        populate()

        used = len(dashboard_queries())

        self.assertLessEqual(
            used,
            POPULATED_QUERY_BUDGET,
            f"Populated dashboard used {used} queries against a budget of "
            f"{POPULATED_QUERY_BUDGET}. Run this locally to see them.",
        )

    def test_the_cost_does_not_grow_with_the_estate(self):
        populate(size=3)
        small = len(dashboard_queries())
        for model in (ManagedResource, ProviderConnection, ProviderInventory, Project):
            model.objects.all().delete()
        DashboardConfiguration.objects.all().delete()
        WeatherObservation.objects.all().delete()
        populate(size=9)

        self.assertEqual(len(dashboard_queries()), small)
