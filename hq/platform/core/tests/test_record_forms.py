"""A record's form is its command's fields, and what it posts is saved."""

from datetime import date

from django.core.exceptions import ImproperlyConfigured
from django.forms import ModelForm
from django.test import TestCase
from django.urls import reverse
from django.views.generic import CreateView

from hq.domains.assets.models import Asset
from hq.domains.projects.models import Project
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.writes import RecordFormMixin
from hq.platform.core.testing import operator


class DerivedRecordFormTests(TestCase):
    def test_a_field_posted_on_the_form_is_saved(self):
        operator(self.client, "example-saver")

        self.client.post(
            reverse("assets:create"),
            form_keyed(
                {
                    "item_name": "Example asset",
                    "category": "other",
                    "total_cost": "1.00",
                    "business_use_percentage": "100",
                    "status": "active",
                    "warranty_date": "2030-01-31",
                }
            ),
        )

        self.assertEqual(Asset.objects.get(item_name="Example asset").warranty_date, date(2030, 1, 31))

    def test_a_record_view_that_names_a_form_of_its_own_is_refused_as_it_is_declared(self):
        with self.assertRaises(ImproperlyConfigured):
            type("NamesAForm", (RecordFormMixin, CreateView), {"model": Asset, "form_class": ModelForm})

    def test_a_slug_sent_with_spaces_around_it_is_saved_without_them(self):
        operator(self.client, "example-slugger")

        self.client.post(
            reverse("projects:create"),
            form_keyed({"name": "Example padded", "slug": "  example-padded  ", "category": "other", "status": "idea"}),
        )

        self.assertTrue(Project.objects.filter(slug="example-padded").exists())
