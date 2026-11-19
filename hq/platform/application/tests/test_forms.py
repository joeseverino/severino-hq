from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..plugin_testing import form_keyed


class BusinessUseTests(TestCase):
    """A record's form says its command's refusal of a share out of range
    beside the field, holding no copy of the rule."""

    PAGES = (
        ("assets:create", {"item_name": "Example asset"}),
        ("expenses:create", {"date": "2030-01-31", "vendor": "Example vendor", "item": "Example item"}),
    )

    def setUp(self):
        user = get_user_model().objects.create_superuser("example-operator", "operator@example.test", "x" * 20)
        self.client.force_login(user)

    def test_a_percentage_out_of_range_is_refused_on_the_field(self):
        for route, filled in self.PAGES:
            for value in (150, -1):
                with self.subTest(route=route, value=value):
                    page = self.client.post(
                        reverse(route), form_keyed({**filled, "business_use_percentage": str(value)})
                    )
                    self.assertTrue(page.context["form"].errors["business_use_percentage"])
