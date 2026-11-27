"""A provider form posted as it was drawn saves what it showed.

Above all a menu drawn from live readings, which lag a record or miss it (no
controller has reported yet): a select without the held value shows nothing
chosen or posts its first option, a change of connection or machine nobody
asked for.
"""

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.markup_testing import all_elements
from hq.platform.core.testing import declare, managing_everything, operator


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
        managing_everything()
        operator(self.client)
        declare("web-container", "portainer.container", self.SPEC)
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
