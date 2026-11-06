"""The zone and DNS resource pages: what they show, what they cost, and the verbs they offer."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow, distinct

from ..adoption_testing import managing_everything
from ..security import cli_principal
from .markup import text, texts
from .zone_records import DNS_CONNECTIONS, RECORD_KIND, ZONE_KIND, record, sweep


class ZonePageTests(TestCase):
    def setUp(self):
        sweep()
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

    def test_the_index_goes_straight_to_a_managed_domain(self):
        ManagedResource.objects.create(
            key="example-com",
            kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": "cf-example"},
            enabled=True,
        )
        response = self.client.get(reverse("zones:index"))
        self.assertRedirects(response, reverse("zones:detail", kwargs={"zone": "example.com"}))

    def test_the_index_offers_what_is_there_when_nothing_is_managed(self):
        response = self.client.get(reverse("zones:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "example.com")

    def test_a_domain_page_lists_every_record_in_it(self):
        response = self.client.get(reverse("zones:detail", kwargs={"zone": "example.com"}))
        self.assertEqual(response.status_code, 200)
        self.assertIn("mx02.mail.example.net", text(response))
        self.assertIn("letsencrypt.org", text(response))

    def test_an_unknown_domain_is_a_404_not_an_empty_page(self):
        response = self.client.get(reverse("zones:detail", kwargs={"zone": "nope.example"}))
        self.assertEqual(response.status_code, 404)


class ProviderSurfaceTests(TestCase):
    def test_a_record_is_not_offered_by_the_generic_add_page(self):
        """It needs a zone, and the page it belongs on has already named one.

        Declared on the provider rather than excluded in the view, so the picker
        never grows a hand-maintained list of kinds to leave out.
        """

        self.assertEqual(PROVIDERS[RECORD_KIND].created_from, "zone")
        self.assertEqual(PROVIDERS[ZONE_KIND].created_from, "")

    def test_a_locked_kind_does_not_promise_to_apply_anything(self):
        """A locked kind's form does not promise that the resource will be
        applied.

        That promise is true of most kinds and false of any whose actions are
        locked, which is exactly the case where knowing that saving changes
        nothing matters most. The capability registry already says so, and the
        page asks it.
        """

        from hq.domains.control_plane.resource_form_views import _apply_note

        # Asserted on the promise, not on the sentence explaining its absence,
        # so the reason can be reworded without editing a test that is not
        # about it.
        self.assertNotIn("HQ applies it", _apply_note(ZONE_KIND))
        self.assertIn(PROVIDERS[ZONE_KIND].actions["reconcile"].reason, _apply_note(ZONE_KIND))
        self.assertIn("HQ applies it", _apply_note(RECORD_KIND))

    def test_a_zone_declares_no_service_facet(self):
        # A zone is a namespace, not a name that answers. Given a facet it would
        # put every managed domain on the services board with nothing behind it.
        self.assertEqual(PROVIDERS[ZONE_KIND].facet, "")
        self.assertIsNone(PROVIDERS[ZONE_KIND].hostnames)


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
        self.client.force_login(get_user_model().objects.create_user("operator", password="x" * 20))

        assert_cost_does_not_grow(
            self, sizes=(5, 60), arrange=self.arrange, draw=self.draw, rows=distinct(r"\bh\d+\.example\.com")
        )


class ResourceDetailIsProviderDeclaredTests(TestCase):
    """Every kind gets a detail card, including ones added after this page. The
    card is generic, because reaching into a provider's spec fields
    (``spec.forward_host``, ``spec.answer``) is the one thing nothing outside a
    provider may do, and a hand-written card per kind leaves new providers
    without one.
    """

    def setUp(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)

    def _detail(self, key, kind, spec, status=None):
        ManagedResource.objects.create(key=key, kind=kind, spec=spec, status=status or {}, enabled=True)
        return self.client.get(reverse("control_plane:detail", kwargs={"key": key}))

    def test_a_provider_added_later_still_describes_itself(self):
        response = self._detail(
            "a-record",
            RECORD_KIND,
            {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
                "proxied": False,
                "ttl": 1,
            },
        )

        self.assertContains(response, PROVIDERS[RECORD_KIND].label)
        self.assertContains(response, "203.0.113.1")

    def test_a_domain_lives_on_its_zone_page(self):
        response = self._detail(
            "a-domain",
            ZONE_KIND,
            {"zone": "example.com", "connection_ref": "cf-example"},
        )

        self.assertRedirects(
            response,
            reverse("zones:detail", args=["example.com"]),
            fetch_redirect_response=False,
        )
        page = self.client.get(response.url)
        self.assertContains(page, "cf-example")
        self.assertContains(page, reverse("control_plane:edit", args=["a-domain"]))
        self.assertContains(page, reverse("control_plane:remove", args=["a-domain"]))

    def test_drift_is_shown_against_what_was_declared(self):
        """The readout carries desired and observed together precisely so a
        page can say they disagree."""

        response = self._detail(
            "drifted",
            "adguard.rewrite",
            {"domain": "app.example.com", "answer": "10.0.0.10"},
            status={"answer": "10.0.0.99"},
        )

        self.assertContains(response, PROVIDERS["adguard.rewrite"].label)
        self.assertContains(response, "10.0.0.99")
        self.assertIn("HQ expects 10.0.0.10", text(response))


class ResourcePageLifecycleTests(TestCase):
    """Fixes for what driving the UI actually surfaced.

    Every case here is something that only showed up by adding a service,
    looking at it, and deleting it, not by reading the code.
    """

    def setUp(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        self.proxy = ManagedResource.objects.create(
            key="private-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["private.example.com"],
                "forward_scheme": "http",
                "forward_host": "10.0.0.5",
                "forward_port": 8081,
                "ssl_forced": True,
                "http2_support": True,
                "allow_websocket_upgrade": False,
                "caching_enabled": False,
                "block_exploits": True,
                "access_list_id": 0,
                "advanced_config": "",
                "hsts_enabled": False,
                "hsts_subdomains": False,
                "trust_forwarded_proto": False,
                "certificate_resource": "",
                "enabled": True,
            },
            enabled=True,
        )

    def test_a_resource_names_the_hostname_it_serves(self):
        """A record is identified by the hostname it serves, not only by the
        key HQ invented.
        """

        response = self.client.get(reverse("control_plane:detail", kwargs={"key": "private-proxy"}))

        self.assertContains(response, "private.example.com")
        self.assertContains(
            response,
            reverse(
                "control_plane:service",
                kwargs={"hostname": "private.example.com"},
            ),
        )

    def test_a_list_field_is_not_shown_as_a_python_repr(self):
        """The confirmation names the hostname, not a list literal."""

        response = self.client.get(reverse("control_plane:remove", kwargs={"key": "private-proxy"}))

        self.assertIn("private.example.com", text(response))
        self.assertNotContains(response, "[&#x27;private.example.com&#x27;]")

    def test_routine_settings_are_folded_away_before_a_deletion(self):
        """Fields the provider declares routine are folded away."""

        response = self.client.get(reverse("control_plane:remove", kwargs={"key": "private-proxy"}))

        self.assertContains(response, "Other settings")

    def test_an_unset_optional_is_not_shown_as_none(self):
        ManagedResource.objects.create(
            key="a-cname",
            kind=RECORD_KIND,
            spec={
                "zone": "example.com",
                "name": "public.example.com",
                "record_type": "CNAME",
                "content": "example.pages.dev",
                "priority": None,
                "proxied": False,
                "ttl": 1,
            },
            enabled=True,
        )

        response = self.client.get(reverse("control_plane:remove", kwargs={"key": "a-cname"}))

        self.assertNotIn("None", texts(response, "code"))

    def test_every_provider_says_what_removing_it_costs(self):
        """Only DNS record types declared this, so deleting a proxy host
        (which takes a service offline) asked "are you sure" about a table of
        fields and said nothing about the consequence."""

        for kind in ("npm.proxy_host", "adguard.rewrite", RECORD_KIND):
            with self.subTest(kind=kind):
                self.assertIsNotNone(PROVIDERS[kind].removal_note)


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

    def test_one_view_serves_every_verb(self):
        from hq.domains.control_plane import views

        self.assertFalse(hasattr(views, "ReconcileView"))
        self.assertFalse(hasattr(views, "RenewCertificateView"))
        self.assertTrue(hasattr(views, "OperationView"))

    def test_a_verbs_press_is_announced_in_its_buttons_words(self):
        """Its press is announced in the words its button uses."""

        from hq.domains.control_plane.views import requested

        self.assertEqual(
            requested(OperationRequest.Action.RENEW, "example-cert", queued=True),
            "Renew certificate requested for example-cert.",
        )

    def test_the_routes_carry_the_verb_rather_than_the_class(self):
        managing_everything()
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="a-rewrite",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.1"},
            enabled=True,
            generation=1,
        )

        response = self.client.post(reverse("control_plane:reconcile", kwargs={"key": "a-rewrite"}), form_keyed())

        self.assertEqual(response.status_code, 302)
        self.assertTrue(OperationRequest.objects.filter(action=OperationRequest.Action.RECONCILE).exists())


class PendingRemovalTests(TestCase):
    """A resource with a deletion in flight is on its way out.

    Shown unchanged, the page invited an operator to edit something about to
    stop existing, and to queue a convergence racing the deletion already
    waiting for the same controller.
    """

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        self.resource = ManagedResource.objects.create(
            key="going",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "10.0.0.1"},
            enabled=True,
            generation=1,
        )

    def _page(self):
        return self.client.get(reverse("control_plane:detail", kwargs={"key": "going"}))

    def _offered(self, response):
        return {action.url for action in response.context["page"].actions}

    def test_it_offers_the_usual_actions_while_nothing_is_pending(self):
        response = self._page()

        self.assertIn(reverse("control_plane:reconcile", kwargs={"key": "going"}), self._offered(response))
        self.assertFalse(response.context["removal_pending"])

    def test_a_queued_removal_withdraws_them_and_leaves_the_report(self):
        from ..resource_operations import OperationCommand, request_removal

        request_removal(
            OperationCommand(idempotency_key="r1"),
            principal=cli_principal(),
            current_key="going",
        )

        response = self._page()

        self.assertTrue(response.context["removal_pending"])
        self.assertNotIn(reverse("control_plane:reconcile", kwargs={"key": "going"}), self._offered(response))
        # The one thing still worth doing: reading what it was.
        self.assertContains(response, reverse("control_plane:report_download", args=["going"]))

    def test_the_topology_withdraws_them_too(self):
        from ..resource_operations import OperationCommand, request_removal
        from ..topology import derive_topology

        request_removal(
            OperationCommand(idempotency_key="r1"),
            principal=cli_principal(),
            current_key="going",
        )

        node = next(node for node in derive_topology(principal=cli_principal()).nodes if node.id == "resource:going")
        self.assertNotIn("reconcile", {action.name for action in node.actions})


class ResourceListReadabilityTests(TestCase):
    """The resource list stays readable with a zone's worth of records.

    Each row says what the record is, not only its key, and a domain (which has
    no controller action) never reads "Pending" or "Never observed".
    """

    def setUp(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="example-com-caa",
            kind=RECORD_KIND,
            spec={
                "zone": "example.com",
                "name": "example.com",
                "record_type": "CAA",
                "content": '0 issue "letsencrypt.org"',
                "proxied": False,
                "ttl": 1,
            },
            enabled=True,
            generation=1,
        )
        ManagedResource.objects.create(
            key="example-com",
            kind=ZONE_KIND,
            spec={"zone": "example.com", "connection_ref": "cf-example"},
            enabled=True,
            generation=1,
        )

    def row(self, page, key):
        start = page.index(f'href="{reverse("control_plane:detail", args=[key])}"')
        return page[start : page.index("</tr>", start)]

    def test_each_row_says_what_it_is_and_only_a_converging_one_waits(self):
        """A domain has no controller action, so nothing is ever coming to it."""

        page = self.client.get(reverse("control_plane:list")).content.decode()
        domain, caa = self.row(page, "example-com"), self.row(page, "example-com-caa")

        self.assertIn('CAA 0 issue "letsencrypt.org"', text(caa))
        self.assertIn("Change waiting to apply", caa)
        self.assertNotIn("Change waiting to apply", domain)


class LabelAndDensityTests(TestCase):
    """Two things that only show up on a real page with real records in it."""

    def test_an_acronym_survives_being_put_mid_sentence(self):
        """ "Add tLS certificate": the first letter lowered without looking at
        the word it belonged to."""

        from ..service_facets import ServiceFacet

        offers = dict(ServiceFacet(id="certificate", label="Certificate").declarable)
        dns = dict(ServiceFacet(id="dns", label="DNS").declarable)

        # Both ways of getting a certificate, offered where one is needed.
        self.assertLessEqual({"tls.certificate", "tls.uploaded_certificate"}, set(offers))
        self.assertEqual(offers["tls.certificate"], "TLS certificate")
        self.assertEqual(dns["adguard.rewrite"], "internal DNS record")
        self.assertEqual(dns[RECORD_KIND], "public DNS record")

    def test_a_long_value_does_not_take_three_lines_in_a_list(self):
        user = get_user_model().objects.create_user("op", password="x" * 20)
        self.client.force_login(user)
        ManagedResource.objects.create(
            key="dmarc",
            kind=RECORD_KIND,
            spec={
                "zone": "example.com",
                "name": "_dmarc.example.com",
                "record_type": "TXT",
                "content": ('"v=DMARC1; p=reject; sp=reject; rua=mailto:872342119743452993e40ddb97bc20d0@example.net"'),
                "proxied": False,
                "ttl": 1,
            },
            enabled=True,
        )

        response = self.client.get(reverse("control_plane:list"))

        self.assertContains(response, "…")
        self.assertNotContains(response, "872342119743452993e40ddb97bc20d0")
