"""``plugin_testing.form_keyed``: an extension's test posts the key a browser's form does."""

from html.parser import HTMLParser

from django.middleware.csrf import get_token
from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase

from .. import plugin_testing


class _Inputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.names.add(dict(attrs)["name"])


class FormKeyedTests(SimpleTestCase):
    def test_the_key_travels_in_the_field_the_form_draws_it_in(self):
        request = RequestFactory().get("/")
        drawn = Template("{% write_token %}").render(Context({"request": request, "csrf_token": get_token(request)}))
        inputs = _Inputs()
        inputs.feed(drawn)
        (key_field,) = inputs.names - {"csrfmiddlewaretoken"}

        posted = plugin_testing.form_keyed({"name": "example"})

        self.assertEqual(posted["name"], "example")
        self.assertTrue(posted[key_field])

    def test_each_form_is_drawn_a_key_of_its_own(self):
        self.assertNotEqual(plugin_testing.form_keyed(), plugin_testing.form_keyed())
