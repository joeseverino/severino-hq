"""A provider form posted as it was drawn saves what it showed.

Above all a menu drawn from live readings, which lag a record or miss it (no
controller has reported yet): a select without the held value shows nothing
chosen or posts its first option, a change of connection or machine nobody
asked for.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import cache
from typing import Annotated
from unittest import mock

from django import forms
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils.datastructures import MultiValueDict
from pydantic import TypeAdapter

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_spec import NameContext
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.application.tests.markup import all_elements, select

from ..plugins import import_reference
from ..provider_forms import spec_form_class

HELD = "held.example"
OTHER = (("other.example", "other.example"),)


@cache
def _live_fields() -> dict[str, tuple[str, ...]]:
    """Every provider field offered as a menu of live data, by kind.

    Read off each provider's own declaration, so a new menu is covered by
    declaring it.
    """

    found: dict[str, tuple[str, ...]] = {}
    for kind, provider in PROVIDERS.items():
        if not provider.choices:
            continue
        names = import_reference(provider.choices)(NameContext())
        fields = provider.spec_type.model_fields
        if offered := tuple(name for name in names if name in fields):
            found[kind] = offered
    return found


def offering_another(_context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Readings that offer one thing, never the one a record holds."""

    return {name: OTHER for names in _live_fields().values() for name in names}


def offering_nothing(_context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Readings from before any controller reported."""

    return {name: () for names in _live_fields().values() for name in names}


@contextmanager
def _reading(readings) -> Iterator[None]:
    """Every provider's menus drawn from ``readings`` instead of live data."""

    reference = f"{__name__}:{readings.__name__}"
    kinds = _live_fields()
    with mock.patch.dict(PROVIDERS, {kind: replace(PROVIDERS[kind], choices=reference) for kind in kinds}):
        yield


def _held(form, name: str):
    many = isinstance(form.fields[name], forms.MultipleChoiceField)
    return [HELD] if many else HELD


def _chosen(html: str) -> set[str]:
    """The values a rendered field has chosen: selected options, ticked boxes."""

    return {element.attrs.get("value", "") for element in (*select(html, "selected"), *select(html, "checked"))}


class HeldValueTests(TestCase):
    def test_the_guard_reads_every_declared_menu(self):
        """An empty map would pass every test below by checking nothing."""

        self.assertTrue(_live_fields())

    def test_an_edit_shows_each_held_value_chosen_though_no_reading_offers_it(self):
        for kind, names in _live_fields().items():
            with (
                self.subTest(kind=kind),
                _reading(offering_another),
            ):
                form_class = spec_form_class(kind, lock_identity=True)
                initial = {name: _held(form_class(), name) for name in names}
                form = form_class(initial=initial)
                for name in names:
                    self.assertEqual(_chosen(str(form[name])), {HELD}, name)

    def test_an_edit_posted_unchanged_keeps_each_held_value(self):
        for kind, names in _live_fields().items():
            with (
                self.subTest(kind=kind),
                _reading(offering_another),
            ):
                form_class = spec_form_class(kind, lock_identity=True)
                initial = {name: _held(form_class(), name) for name in names}
                form = form_class(initial, initial=initial)
                form.is_valid()
                for name in names:
                    self.assertNotIn(name, form.errors)
                    self.assertEqual(form.cleaned_data[name], initial[name])

    def test_an_edit_holding_nothing_starts_blank_rather_than_on_the_first_option(self):
        for kind, names in _live_fields().items():
            with (
                self.subTest(kind=kind),
                _reading(offering_another),
            ):
                form = spec_form_class(kind, lock_identity=True)(initial={})
                for name in names:
                    if isinstance(form.fields[name], forms.MultipleChoiceField):
                        continue
                    self.assertEqual(form.fields[name].choices[0][0], "", name)
                    self.assertNotIn("other.example", _chosen(str(form[name])))

    def test_an_empty_menu_says_why_beside_its_own_description(self):
        for kind, names in _live_fields().items():
            with (
                self.subTest(kind=kind),
                _reading(offering_nothing),
            ):
                form = spec_form_class(kind)()
                for name in names:
                    self.assertIn("Nothing to choose from yet", form.fields[name].help_text, name)


def _as_posted(page: str) -> dict[str, list[str]]:
    """What a browser submits from the page's form, touching nothing."""

    posted: dict[str, list[str]] = {}
    shown = all_elements(page)
    for element in shown:
        if not any(ancestor.tag == "form" and "form" in ancestor.classes for ancestor in element.ancestors):
            continue
        name = element.attrs.get("name")
        if not name or element.tag not in {"input", "textarea", "select"}:
            continue
        values = _submitted(element, shown)
        posted.setdefault(name, []).extend(values)
    return posted


def _submitted(element, shown) -> list[str]:
    if element.tag == "select":
        return [
            option.attrs.get("value", "")
            for option in shown
            if option.tag == "option" and "selected" in option.attrs and any(a is element for a in option.ancestors)
        ]
    if element.tag == "textarea":
        # A browser drops the newline that opens a textarea's text.
        return [element.text.removeprefix("\n")]
    if element.attrs.get("type") in {"checkbox", "radio"} and "checked" not in element.attrs:
        return []
    if element.attrs.get("type") in {"submit", "button"}:
        return []
    return [element.attrs.get("value", "")]


class EditPageTests(TestCase):
    """The edit page, with no controller readings at all."""

    SPEC = {"connection_ref": "example-portainer", "host": "example-host", "name": "web"}

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        self.client.force_login(get_user_model().objects.create_user(username="operator", password="test-only"))
        ManagedResource.objects.create(key="web-container", kind="portainer.container", spec=self.SPEC)
        self.url = reverse("control_plane:edit", kwargs={"key": "web-container"})

    def test_the_page_offers_the_held_portainer_and_machine_and_saves_them_unchanged(self):
        page = self.client.get(self.url).content.decode()
        posted = _as_posted(page)
        response = self.client.post(self.url, posted)

        self.assertEqual(posted["connection_ref"], ["example-portainer"])
        self.assertEqual(posted["host"], ["example-host"])
        self.assertEqual(response.status_code, 302)
        spec = ManagedResource.objects.get().spec
        self.assertEqual({name: spec.get(name) for name in self.SPEC}, self.SPEC)

    def test_a_value_shown_from_another_source_posts_back_unchanged(self):
        """A held value the page filled in from what resolution supplies."""

        authored = {name: value for name, value in self.SPEC.items() if name != "host"}
        ManagedResource.objects.filter(key="web-container").update(spec=authored)
        derived = mock.patch(
            "hq.domains.control_plane.resource_form_views.resolved_spec",
            return_value={**authored, "host": "derived-host"},
        )
        with derived:
            posted = _as_posted(self.client.get(self.url).content.decode())
            response = self.client.post(self.url, posted)

        self.assertEqual(posted["host"], ["derived-host"])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ManagedResource.objects.get().spec["host"], "derived-host")


class DefaultRoundTripTests(TestCase):
    def test_every_default_posted_as_drawn_is_one_the_model_accepts(self):
        """A field the form draws holding its default must read back as a value
        the model takes, or no record of its kind can be saved untouched. A
        withheld kind has no generic form to draw."""

        for kind, provider in PROVIDERS.items():
            if provider.withheld:
                continue
            form_class = spec_form_class(kind)
            for name, model_field in provider.spec_type.model_fields.items():
                if model_field.is_required():
                    continue
                with self.subTest(kind=kind, field=name):
                    drawn = f'<form class="form">{form_class()[name]}</form>'
                    form = form_class(MultiValueDict(_as_posted(drawn)))
                    form.is_valid()
                    self.assertNotIn(name, form.errors)
                    value = form.cleaned_data[name]
                    if value is not None:
                        TypeAdapter(Annotated[model_field.annotation, model_field]).validate_python(value)
