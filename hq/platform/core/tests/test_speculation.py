"""A page fetched on a guess: where pages point the browser, and what the guess may not do."""

import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from hq.platform.core import revisions, speculation
from hq.platform.core.bench import seed
from hq.platform.core.management.commands.bench_pages import _pages
from hq.platform.core.models import AuditLog

PREFETCH = {"Sec-Purpose": "prefetch"}


class RulesTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("operator"))

    def test_a_page_names_the_rules_and_a_part_of_one_does_not(self):
        page = self.client.get(reverse("calendar:month"))
        part = self.client.get(
            reverse("calendar:month"), headers={"X-Requested-With": "XMLHttpRequest"}
        )

        self.assertEqual(page["Speculation-Rules"], f'"{reverse("speculation_rules")}"')
        self.assertNotIn("Speculation-Rules", part)

    def test_a_signed_out_page_names_none(self):
        self.assertNotIn("Speculation-Rules", Client().get(reverse("login")))

    def test_the_document_is_served_as_rules_to_a_session_only(self):
        self.assertEqual(Client().get(reverse("speculation_rules")).status_code, 302)
        response = self.client.get(reverse("speculation_rules"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], speculation.RULES_TYPE)
        self.assertEqual(json.loads(response.content), speculation.rules())

    def test_it_prefetches_on_press_and_never_prerenders(self):
        rules = speculation.rules()

        self.assertEqual(list(rules), ["prefetch"])
        self.assertEqual([rule["eagerness"] for rule in rules["prefetch"]], ["conservative"])

    def test_the_routes_that_answer_without_a_session_are_left_out(self):
        left_out = speculation.sessionless()

        for name in ("logout", "login", "oidc_authentication_init", "oidc_authentication_callback"):
            with self.subTest(route=name):
                self.assertIn(reverse(name), left_out)
        self.assertNotIn(reverse("dashboard"), left_out)
        excluded = speculation.rules()["prefetch"][0]["where"]["and"][1]["not"]["href_matches"]
        self.assertEqual(excluded, list(left_out))


class SpeculativeRequestTests(TestCase):
    """A speculative request has no effect, on any page there is."""

    def test_no_page_writes_when_it_is_only_prefetched(self):
        seeded = seed(0.05)
        self.client.force_login(seeded.user)
        pages, _ = _pages(seeded)
        self.assertGreater(len(pages), 50)
        refused = []
        for name, url in pages:
            if "?" in name:
                continue  # the same view asked a narrower question
            # Once as a person, so what the page derives is already stored.
            self.client.get(url)
            before = revisions.read().counts
            response = self.client.get(url, headers=PREFETCH)
            with self.subTest(page=name):
                self.assertEqual(revisions.read().counts, before)
            if response.status_code == 503:
                refused.append(name)

        # The exports record that they were taken: each is refused, unrecorded.
        self.assertTrue(refused)
        self.assertTrue(all(name.startswith(("reports:", "hq_api:")) for name in refused), refused)

    def test_signing_out_is_refused_before_it_runs(self):
        self.client.force_login(get_user_model().objects.create_superuser("operator"))

        response = self.client.post(reverse("logout"), headers=PREFETCH)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_an_export_is_refused_and_leaves_no_record(self):
        self.client.force_login(get_user_model().objects.create_superuser("operator"))
        url = reverse("reports:expenses_csv")

        guessed = self.client.get(url, headers=PREFETCH)
        self.assertEqual(guessed.status_code, 503)
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.EXPORTED).exists())

        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.EXPORTED).exists())

    def test_a_prerender_is_refused_whole(self):
        self.client.force_login(get_user_model().objects.create_superuser("operator"))

        response = self.client.get(
            reverse("dashboard"), headers={"Sec-Purpose": "prefetch;prerender"}
        )

        self.assertEqual(response.status_code, 503)

    def test_a_prefetched_page_is_the_page(self):
        self.client.force_login(get_user_model().objects.create_superuser("operator"))

        response = self.client.get(reverse("calendar:month"), headers=PREFETCH)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Sec-Purpose", response["Vary"])

    def test_the_outbound_boundary_refuses_a_guess(self):
        from hq.platform.core.outbound import allowed

        token = speculation._refused.set([])
        try:
            with self.assertRaises(speculation.Speculative), allowed("lookup"):
                self.fail("a speculative request reached outside")
        finally:
            speculation._refused.reset(token)

    def test_a_request_somebody_made_is_untouched(self):
        speculation.refuse("nothing")
