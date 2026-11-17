"""Which kinds of resource a finding is raised for, and when what HQ knows of one has gone stale."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.plugin_testing import form_keyed

from ..findings import derive_findings
from ..report_testing import bridged
from ..topology import derive_topology
from ..topology_model import Topology, TopologyNode
from .finding_principals import MANAGE
from .host_only import host_only


class DeclarationOnlyKindsTests(TestCase):
    """A kind nothing sweeps cannot be judged by when it was last swept.

    A printer, an offline CA, a certificate delivery target: HQ was told about
    them and no provider reports them back. Asked when they were last observed
    the answer is "never", which no sweep or reconcile can change.
    """

    def _kinds_raised(self, kind):
        from ..controller_findings import _kind_never_swept
        from ..finding_model import FindingEstate

        node = TopologyNode(
            f"resource:{kind}-one",
            "resource",
            "one",
            "Resource",
            kind_key=kind,
            managed=True,
        )
        estate = FindingEstate(
            topology=Topology(nodes=(node,), edges=()),
            now=timezone.now(),
            observed={},
            latest_by_kind={},
            declared_kinds=frozenset({kind}),
            declared_counts={kind: 1},
            governed={},
            controllers_by_kind={},
        )
        return tuple(f.scope for f in _kind_never_swept(estate))

    def test_a_declaration_only_kind_raises_nothing(self):
        self.assertEqual(self._kinds_raised("machine"), ())
        self.assertEqual(self._kinds_raised("tls.delivery_target"), ())

    def test_a_swept_kind_still_does(self):
        self.assertEqual(self._kinds_raised("tailscale.policy"), ("tailscale.policy",))

    def test_it_is_never_called_unobserved_one_record_at_a_time_either(self):
        from ..controller_findings import _never_observed
        from ..finding_model import FindingEstate

        seen = TopologyNode(
            "resource:seen",
            "resource",
            "seen",
            "Target",
            kind_key="tls.delivery_target",
            observed_at=timezone.now().isoformat(),
        )
        unseen = TopologyNode("resource:unseen", "resource", "unseen", "Target", kind_key="tls.delivery_target")
        estate = FindingEstate(
            topology=Topology(nodes=(seen, unseen), edges=()),
            now=timezone.now(),
            observed={seen.id: timezone.now()},
            latest_by_kind={"tls.delivery_target": timezone.now()},
            declared_kinds=frozenset({"tls.delivery_target"}),
            declared_counts={"tls.delivery_target": 2},
            governed=frozenset({unseen.id}),
            controllers_by_kind={},
        )

        self.assertEqual(_never_observed(estate), ())


class OnDemandContainerTests(TestCase):
    """A container that only runs now and then is removed while it is off."""

    def _missing(self, *, on_demand):
        from ..controller_findings import _skipped_by_a_sweep
        from ..finding_model import FindingEstate

        now = timezone.now()
        then = now - timedelta(days=2)
        running = TopologyNode(
            "resource:web",
            "resource",
            "web",
            "Container",
            kind_key="portainer.container",
            observed_at=now.isoformat(),
        )
        off = TopologyNode(
            "resource:tool",
            "resource",
            "tool",
            "Container",
            kind_key="portainer.container",
            observed_at=then.isoformat(),
            on_demand=on_demand,
        )
        estate = FindingEstate(
            topology=Topology(nodes=(running, off), edges=()),
            now=now,
            observed={running.id: now, off.id: then},
            latest_by_kind={"portainer.container": now},
            declared_kinds=frozenset({"portainer.container"}),
            declared_counts={"portainer.container": 2},
            governed=frozenset(),
            controllers_by_kind={},
        )
        return _skipped_by_a_sweep(estate)

    def test_missing_from_a_sweep_is_expected_when_declared_on_demand(self):
        self.assertEqual(self._missing(on_demand=True), ())

    def test_otherwise_it_offers_the_two_real_answers_and_not_the_locked_reconcile(self):
        (finding,) = self._missing(on_demand=False)

        self.assertEqual(
            [remedy.capability for remedy in finding.remedies],
            ["infrastructure.resource.update", "infrastructure.resource.remove"],
        )


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


class StalenessNeedsSomethingDeclaredTests(TestCase):
    """An inventory carries rows HQ declares nothing of.

    A finance feed and a vehicle lookup are written on their own schedule by
    something that is not the sweep. Measured against the sweep interval each
    would be overdue every time it is read, and no sweep or reconcile could
    settle it, because there is no declaration of that kind to settle.
    """

    def _raised(self, *, declared):
        from ..controller_findings import _kind_never_swept
        from ..finding_model import FindingEstate

        now = timezone.now()
        return tuple(
            f.scope
            for f in _kind_never_swept(
                FindingEstate(
                    topology=Topology(nodes=(), edges=()),
                    now=now,
                    observed={},
                    latest_by_kind={"adguard.rewrite": now - timedelta(days=2)},
                    declared_kinds=frozenset({"adguard.rewrite"} if declared else set()),
                    declared_counts={},
                    governed={},
                    controllers_by_kind={},
                )
            )
        )

    def test_an_observed_kind_nothing_declares_raises_nothing(self):
        self.assertEqual(self._raised(declared=False), ())

    def test_the_same_kind_with_a_declaration_still_does(self):
        self.assertEqual(self._raised(declared=True), ("adguard.rewrite",))


class WorkThatKeepsFailingTests(TestCase):
    """A connection that answers every probe and finishes none of its work."""

    def _findings(self, *facts):
        from hq.platform.application.controller_findings import _work_that_keeps_failing
        from hq.platform.application.findings import _estate
        from hq.platform.application.topology_model import Topology, TopologyNode

        node = TopologyNode(
            id="connection:infrastructure.controllers:a-host:shared-hosting",
            kind="connection",
            label="shared-hosting",
            subtitle="SSH",
            facts=tuple(facts),
        )
        return _work_that_keeps_failing(_estate(Topology(nodes=(node,), edges=())))

    def test_each_unfinished_step_is_claimed_against_its_connection(self):
        (finding,) = self._findings(
            ("Controller", "a-host"),
            ("work-unfinished", "SSH routes for shared-hosting (exit 126)"),
            ("work-unfinished", "SSH deploy for shared-hosting (exit 1)"),
        )

        self.assertEqual(finding.rule, "work-that-keeps-failing")
        self.assertIn("shared-hosting", finding.title)
        self.assertEqual(
            finding.evidence,
            (
                ("Keeps failing", "SSH routes for shared-hosting (exit 126)"),
                ("Keeps failing", "SSH deploy for shared-hosting (exit 1)"),
            ),
        )

    def test_a_connection_whose_work_finishes_raises_nothing(self):
        """Reachable and useful is the ordinary case and says nothing."""

        self.assertEqual(self._findings(("Controller", "a-host")), ())

    def test_a_reported_failing_step_reaches_the_finding_through_the_topology(self):
        from hq.domains.control_plane.models import ProviderConnection
        from hq.platform.application.findings import derive_findings
        from hq.platform.application.topology import derive_topology

        now = timezone.now()
        for controller in ("a-host", "b-host"):
            ProviderConnection.objects.create(
                connection_ref="shared-hosting",
                controller_id=controller,
                provider="ssh",
                endpoint="192.0.2.20:22",
                reachable=True,
                probed=True,
                observed_at=now,
                failing_steps=(
                    [{"step": "SSH routes for shared-hosting", "reason": "exit 126"}] if controller == "a-host" else []
                ),
            )
        with host_only():
            projection = derive_topology(principal=MANAGE)
        found = [
            finding
            for finding in derive_findings(projection, principal=MANAGE)
            if finding.rule == "work-that-keeps-failing"
        ]

        self.assertEqual(len(found), 1)
        self.assertIn(":a-host:", found[0].subject)
        self.assertEqual(
            found[0].evidence,
            (("Keeps failing", "SSH routes for shared-hosting (exit 126)"),),
        )


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

    def test_it_is_one_serious_finding_about_its_machine_gathered_with_the_topology(self):
        """The fact is gathered while the topology is built, not by the rule."""

        from django.db import connection as database_connection
        from django.test.utils import CaptureQueriesContext

        projection = self.projected()
        with CaptureQueriesContext(database_connection) as captured:
            raised = derive_findings(projection, principal=MANAGE)

        self.assertEqual(len(captured), 0)
        (finding,) = [f for f in raised if f.rule == "unrecognized-container"]
        self.assertEqual((finding.subject, finding.severity), ("machine:a-docker-host", "serious"))
        self.assertIn("a-stray", finding.title)

    def test_a_container_its_compose_project_declares_is_not_reported(self):
        self.assertNotIn("a-web", " ".join(f.title for f in self.found()))

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
