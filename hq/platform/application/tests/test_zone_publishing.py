"""Publishing a name: adopting its records, publishing a service, names served elsewhere, and letting a domain go."""

from django.test import TestCase
from django.urls import reverse

from hq.platform.application.plugin_testing import form_keyed
from hq.platform.core.testing import managing_everything, operator


class PublishAServiceTests(TestCase):
    """Starting from a name, which is the only thing HQ cannot work out.

    The name is typed once and seeds every resource the service needs.
    """

    def setUp(self):
        operator(self.client, "op")

    def test_the_hostname_is_all_it_asks_for(self):
        response = self.client.get(
            reverse("control_plane:service_start"),
            {"hostname": "New.Example.com."},
        )

        self.assertRedirects(
            response,
            reverse("control_plane:service", kwargs={"hostname": "new.example.com"}),
        )

    def test_a_name_that_is_not_a_name_is_refused(self):
        for sent in ("not a hostname", "an_app.example.com", "192.0.2.10"):
            with self.subTest(sent=sent):
                response = self.client.get(reverse("control_plane:service_start"), {"hostname": sent})

                self.assertEqual(response.status_code, 400)
                self.assertIn("hostname", response.context["form"].errors)

    def test_saving_returns_to_the_service_being_built(self):
        """Landing on each resource's own page made the next step a navigation
        problem. The service page is the thing being assembled."""

        managing_everything()
        response = self.client.post(
            reverse("control_plane:create") + "?kind=adguard.rewrite&hostname=new.example.com",
            {
                "domain": "new.example.com",
                "answer": "10.0.0.7",
                **form_keyed(),
            },
        )

        self.assertRedirects(
            response,
            reverse("control_plane:service", kwargs={"hostname": "new.example.com"}),
        )
