"""The zone and DNS resource pages: what they show, what they cost, and the verbs they offer."""

from django.test import TestCase, override_settings
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.platform.application.markup_testing import hrefs
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.consent import consented
from hq.platform.core.testing import declare, managing_everything, operator
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow, distinct

from ..security import cli_principal
from .zone_records import DNS_CONNECTIONS, RECORD_KIND, ZONE_KIND, record, sweep


class ZonePageTests(TestCase):
    def setUp(self):
        sweep()
        operator(self.client, "op")

    def test_the_index_goes_straight_to_a_managed_domain(self):
        declare("example-com", ZONE_KIND, {"zone": "example.com", "connection_ref": "cf-example"}, enabled=True)
        response = self.client.get(reverse("zones:index"))
        self.assertRedirects(response, reverse("zones:detail", kwargs={"zone": "example.com"}))

    def test_an_unknown_domain_is_a_404_not_an_empty_page(self):
        response = self.client.get(reverse("zones:detail", kwargs={"zone": "nope.example"}))
        self.assertEqual(response.status_code, 404)

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
    def test_a_domain_s_record_page_sends_it_to_its_zone_page(self):
        declare("a-domain", ZONE_KIND, {"zone": "example.com", "connection_ref": "cf-example"}, enabled=True)
        response = self.client.get(reverse("control_plane:detail", kwargs={"key": "a-domain"}))

        self.assertRedirects(response, reverse("zones:detail", args=["example.com"]), fetch_redirect_response=False)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class DomainPageCostTests(TestCase):
    """The page must not cost more as a zone grows.

    A domain view is built from three reads (the declarations, the last sweep,
    and the unmanaged diff between them) and then sliced. Anything that scales
    with the number of records means a per-row query hiding in a property,
    which is invisible until a zone is large.
    """

    def setUp(self):
        managing_everything(*DNS_CONNECTIONS)

    def _zone_with(self, count):
        sweep(records=[record(f"h{i}.example.com", "A", "203.0.113.1", rid=f"r{i}") for i in range(count)])
        ManagedResource.objects.get_or_create(
            key="example-com",
            kind=ZONE_KIND,
            defaults={
                "spec": {"zone": "example.com", "connection_ref": "cf-example"},
                "enabled": True,
            },
        )

    def arrange(self, count):
        ManagedResource.objects.filter(kind=RECORD_KIND).delete()
        self._zone_with(count)

    def draw(self, _count):
        response = self.client.get(reverse("zones:detail", kwargs={"zone": "example.com"}))
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_page_does_not_cost_more_per_record(self):
        operator(self.client)

        assert_cost_does_not_grow(
            self, sizes=(5, 60), arrange=self.arrange, draw=self.draw, rows=distinct(r"\bh\d+\.example\.com")
        )


class PublicDnsOffTests(TestCase):
    """With public DNS changes off the records are read-only, so the page
    offers no control that changes them: every edit and removal there would
    be refused."""

    def setUp(self):
        managing_everything(*DNS_CONNECTIONS)
        sweep(records=[record("app.example.com", "A", "203.0.113.1", rid="r1")])
        ManagedResource.objects.get_or_create(
            key="example-com",
            kind=ZONE_KIND,
            defaults={"spec": {"zone": "example.com", "connection_ref": "cf-example"}, "enabled": True},
        )
        # A record HQ manages, so its row carries the controls that change it.
        declare(
            "example-com-app",
            RECORD_KIND,
            {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
                "proxied": False,
                "ttl": 1,
            },
            enabled=True,
        )
        operator(self.client)

    def changes_offered(self):
        response = self.client.get(reverse("zones:detail", kwargs={"zone": "example.com"}))
        self.assertEqual(response.status_code, 200)
        # Letting HQ stop managing the domain leaves its records live, so it
        # stays offered; a record's removal and any edit change them.
        forget = reverse("control_plane:remove", args=["example-com"])
        return [href for href in hrefs(response) if "/edit/" in href or ("/remove/" in href and href != forget)]

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
    def test_with_changes_on_the_records_and_the_domain_offer_them(self):
        offered = self.changes_offered()
        edit = reverse("control_plane:edit", args=["example-com-app"])
        remove = reverse("control_plane:remove", args=["example-com-app"])

        self.assertTrue(any(href.startswith(edit) for href in offered), offered)
        self.assertIn(remove, offered)
        self.assertTrue(any(href.startswith(reverse("control_plane:edit", args=["example-com"])) for href in offered))

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=False)
    def test_with_changes_off_nothing_offers_a_change_it_would_refuse(self):
        self.assertEqual(self.changes_offered(), [])


class CertificateEditFormTests(TestCase):
    """A certificate's edit form asks what it is, and folds what it tunes."""

    def _form(self, **initial):
        from ..provider_forms import spec_form_class

        return spec_form_class("tls.certificate", lock_identity=True)(initial=initial)

    def test_editing_asks_what_defines_the_certificate_and_a_changed_default(self):
        form = self._form(certificate_name="wildcard", domains=["example.com"])
        changed = self._form(certificate_name="wildcard", renewal_window_days=45)

        self.assertEqual(
            {"certificate_name", "domains", "install_on"},
            {field.name for field in form.primary},
        )
        self.assertIn("renewal_window_days", [f.name for f in changed.primary])

    def test_a_field_still_at_its_default_stays_folded_away(self):
        """Nobody arrives at a certificate to adjust how early it renews.

        HQ renews on its own, so the window is a knob rather than a question,
        and it holds the answer the model would have given anyway.
        """

        form = self._form(certificate_name="wildcard", renewal_window_days=30)

        self.assertIn("renewal_window_days", [f.name for f in form.advanced])


class NewVerbReadinessTests(TestCase):
    """A new controller verb needs no new view class."""

    def test_the_routes_carry_the_verb_rather_than_the_class(self):
        """A press runs its verb, and says so in the words its button uses."""

        from django.contrib.messages import get_messages

        from ..capabilities import capability_words
        from ..resource_operations import ACTION_CAPABILITY

        managing_everything()
        operator(self.client, "op")
        declare(
            "a-rewrite",
            "adguard.rewrite",
            {"domain": "app.example.com", "answer": "10.0.0.1"},
            enabled=True,
            generation=1,
        )

        response = self.client.post(reverse("control_plane:reconcile", kwargs={"key": "a-rewrite"}), form_keyed())

        self.assertEqual(response.status_code, 302)
        self.assertTrue(OperationRequest.objects.filter(action=OperationRequest.Action.RECONCILE).exists())
        said = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(len(said), 1)
        self.assertIn(capability_words(ACTION_CAPABILITY[OperationRequest.Action.RECONCILE]), said[0])
        self.assertIn("a-rewrite", said[0])


class PendingRemovalTests(TestCase):
    """A resource with a deletion in flight is on its way out.

    Shown unchanged, the page invited an operator to edit something about to
    stop existing, and to queue a convergence racing the deletion already
    waiting for the same controller.
    """

    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        operator(self.client, "op")
        self.resource = declare(
            "going", "adguard.rewrite", {"domain": "app.example.com", "answer": "10.0.0.1"}, enabled=True, generation=1
        )

    def _page(self):
        return self.client.get(reverse("control_plane:detail", kwargs={"key": "going"}))

    def _offered(self, response):
        return {action.url for action in response.context["page"].actions}

    def test_it_offers_the_usual_actions_while_nothing_is_pending(self):
        response = self._page()

        self.assertIn(reverse("control_plane:reconcile", kwargs={"key": "going"}), self._offered(response))
        self.assertFalse(response.context["resource"]["removal"]["pending"])

    def test_a_queued_removal_withdraws_them_and_leaves_the_report(self):
        from ..resource_operations import OperationCommand, request_removal

        request_removal(
            OperationCommand(),
            consented=consented(cli_principal()),
            current_key="going",
        )

        response = self._page()

        self.assertTrue(response.context["resource"]["removal"]["pending"])
        self.assertNotIn(reverse("control_plane:reconcile", kwargs={"key": "going"}), self._offered(response))

    def test_the_topology_withdraws_them_too(self):
        from ..resource_operations import OperationCommand, request_removal
        from ..topology import derive_topology

        request_removal(
            OperationCommand(),
            consented=consented(cli_principal()),
            current_key="going",
        )

        node = next(node for node in derive_topology(principal=cli_principal()).nodes if node.id == "resource:going")
        self.assertNotIn("reconcile", {action.name for action in node.actions})
