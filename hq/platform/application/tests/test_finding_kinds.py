"""Which kinds of resource a finding is raised for, and when what HQ knows of one has gone stale."""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.plugin_testing import form_keyed

from ..findings import derive_findings
from ..report_testing import bridged
from ..topology import derive_topology
from .finding_principals import MANAGE
from .host_only import host_only


class StalenessIsMeasuredOnlyWhereASweepGoesTests(TestCase):
    """`kind-never-swept` compares against the sweep interval, so it means
    something only for a kind a sweep visits.

    A certificate is observed when an operation issues or installs it, which is
    nothing like every sixty seconds. Measured on that cadence it is
    permanently overdue and no sweep or reconcile settles it. Expiry, which is
    the risk a certificate actually carries, is reported where it belongs.
    """

    def test_only_a_kind_a_collector_sweeps_is_judged_on_sweep_cadence(self):
        from hq.domains.control_plane.providers import PROVIDERS

        from ..finding_model import is_observable

        for kind in ("tls.certificate", "machine", "tls.delivery_target"):
            with self.subTest(kind=kind):
                self.assertTrue(PROVIDERS[kind].unobserved_reason, f"{kind} has no collector and should say so")
                self.assertFalse(is_observable(kind))
        # The exemption must not quietly turn staleness reporting off.
        for kind in ("adguard.rewrite", "tailscale.device", "npm.proxy_host"):
            with self.subTest(kind=kind):
                self.assertTrue(is_observable(kind))


class UnrecognizedContainerTests(TestCase):
    """A container no compose project declares is reported, never adopted.

    The sweep leaves it unmanaged, so it has no node of its own. Its machine
    carries it as a fact, and the finding reads it from there and offers the
    one adoption that takes exactly that record on.
    """

    STRAY = {
        "name": "a-stray",
        "host": "a-docker-host",
        "connection_ref": "a-portainer",
        "stack": "",
    }
    COMPOSED = {
        "name": "a-web",
        "host": "a-docker-host",
        "connection_ref": "a-portainer",
        "stack": "a-project",
    }

    def setUp(self):
        from ..adoption_testing import connection

        connection("portainer", "a-portainer")
        ManagedResource.objects.create(
            key="a-docker-host",
            kind="machine",
            spec={"name": "a-docker-host", "addresses": ["10.0.0.9"]},
        )
        bridged(
            "inventory",
            {
                "portainer.container": {
                    "ok": True,
                    "records": [self.COMPOSED, self.STRAY],
                }
            },
        )

    def projected(self):
        with host_only():
            return derive_topology(principal=MANAGE)

    def found(self):
        return [f for f in derive_findings(self.projected(), principal=MANAGE) if f.rule == "unrecognized-container"]

    def test_it_is_gathered_with_the_topology(self):
        """The fact is gathered while the topology is built, not by the rule."""

        from django.db import connection as database_connection
        from django.test.utils import CaptureQueriesContext

        projection = self.projected()
        with CaptureQueriesContext(database_connection) as captured:
            raised = derive_findings(projection, principal=MANAGE)

        self.assertEqual(len(captured), 0)
        self.assertEqual(len([f for f in raised if f.rule == "unrecognized-container"]), 1)

    def test_the_remedy_adopts_exactly_that_record(self):
        from django.urls import reverse

        from ..adoption import unmanaged
        from ..inventory import record_token

        (finding,) = self.found()
        (remedy,) = finding.presses
        (item,) = unmanaged()
        token = record_token("portainer.container", ("a-docker-host", "a-stray"))

        self.assertEqual(remedy.capability, "infrastructure.resource.create")
        self.assertEqual(remedy.method, "POST")
        # The same handle the adoption looks the record up by.
        self.assertEqual(token, item.token)
        self.assertEqual(
            remedy.url,
            reverse("control_plane:adopt_record", args=["portainer.container", token]),
        )

    def test_adopting_it_from_the_page_clears_the_finding(self):
        from django.contrib.auth import get_user_model

        (finding,) = self.found()
        self.client.force_login(
            get_user_model().objects.create_user(username="operator", password="test-only-password")
        )

        self.client.post(finding.presses[0].url, form_keyed())

        self.assertTrue(ManagedResource.objects.filter(kind="portainer.container", spec__name="a-stray").exists())
        self.assertEqual(self.found(), [])

    def test_adopting_it_through_the_service_clears_the_finding(self):
        from ..adoption import AdoptCommand, adopt, unmanaged
        from ..security import cli_principal

        (item,) = unmanaged()

        adopt(
            AdoptCommand(kind="portainer.container", token=item.token),
            principal=cli_principal(),
        )

        self.assertEqual(self.found(), [])
