"""A refused edit shows its form again, under the record's head as stored.

Every routed edit view is found from the URL configuration and given a record
derived from its model's own fields. Each is posted an edit that changes what
its form lets it change (a required text field rewritten, an optional one
emptied, a choice moved) and that the model then refuses, as a model rule does
once the form has cleaned its fields.
"""

from unittest import mock

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models
from django.test import TestCase
from django.urls import reverse
from django.views.generic.edit import BaseUpdateView

from hq.platform.core.tests.test_browser_derived_fixtures import _Fill

from ..routes import routed

REFUSAL = "Refused by the model."


def edit_routes():
    """Every routed host view that edits a stored record, with its route."""

    return [
        route
        for route in routed()
        if isinstance(route.view, type)
        and issubclass(route.view, BaseUpdateView)
        and route.view.__module__.startswith("hq.")
    ]


def _model(view):
    return view.model or view.queryset.model


def _address(route, record):
    """The edit page's URL for ``record``, from the kwargs its route takes."""

    view = route.view
    attribute = {view.pk_url_kwarg: "pk", view.slug_url_kwarg: view.slug_field}
    taken = route.pattern.pattern.regex.groupindex
    return reverse(route.name, kwargs={name: getattr(record, attribute[name]) for name in taken})


def _stored(record):
    """``record``, with each file it names written to its storage, since a
    derived row names a file nobody wrote."""

    for field in record._meta.concrete_fields:
        if isinstance(field, models.FileField) and (stored := getattr(record, field.name)):
            field.storage.save(stored.name, ContentFile(b"example"))
    return record


def _edited(form):
    """The form's fields as it shows them, each one a person may change set to
    something other than what is stored."""

    data = {}
    for name, field in form.fields.items():
        stored = form[name].value()
        if isinstance(field, forms.FileField):
            continue
        if isinstance(field, forms.ChoiceField) and not isinstance(field, forms.ModelChoiceField):
            others = [value for value, _label in field.choices if value not in ("", stored)]
            data[name] = others[0] if others else stored
        elif isinstance(field, forms.CharField):
            data[name] = "an-unsaved-edit" if field.required else ""
        elif stored is not None:
            data[name] = stored
    return data


def _refuse(_record):
    raise ValidationError(REFUSAL)


class RefusedEditTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("operator", password="unused-test-pass")
        self.client.force_login(user)
        self.fill = _Fill(user)

    def test_a_refused_edit_shows_its_errors_under_the_stored_head(self):
        routes = edit_routes()
        self.assertTrue(routes, "the URL configuration routes no edit view")
        for route in routes:
            with self.subTest(route.name):
                model = _model(route.view)
                url = _address(route, _stored(self.fill.rows_for(model)[0]))
                shown = self.client.get(url)
                self.assertEqual(shown.status_code, 200)
                with mock.patch.object(model, "clean", _refuse):
                    refused = self.client.post(url, _edited(shown.context["form"]))
                self.assertEqual(refused.status_code, 200)
                self.assertIn(REFUSAL, refused.context["form"].non_field_errors())
                self.assertEqual(refused.context["page"], shown.context["page"])
