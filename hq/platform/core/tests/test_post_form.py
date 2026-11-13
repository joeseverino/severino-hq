"""A POST action that sends what it acts on besides its button's field: a form
of its own, drawn wherever a page action is."""

from django.template import RequestContext, Template
from django.test import RequestFactory, SimpleTestCase

from hq.platform.application.pages import PageAction
from hq.platform.application.tests.markup import elements


class PostFormTests(SimpleTestCase):
    def draw(self, *actions: PageAction) -> str:
        request = RequestFactory().get("/example/?page=2")
        return Template("<table><tr>{% row_actions actions %}</tr></table>").render(
            RequestContext(request, {"actions": actions})
        )

    def sent(self, html: str) -> dict[str, str]:
        return {
            element.attrs["name"]: element.attrs.get("value", "")
            for element in [*elements(html, "input"), *elements(html, "button")]
        }

    def test_it_posts_its_fields_its_button_and_a_token_and_returns_to_the_page(self):
        html = self.draw(
            PageAction(
                "Block", "/example/block/", method="post", field="blocked", value="1", fields={"client_id": "a b"}
            )
        )

        (form,) = elements(html, "form")
        self.assertEqual((form.attrs["method"], form.attrs["action"]), ("post", "/example/block/"))
        sent = self.sent(html)
        self.assertEqual(
            {name: sent[name] for name in ("client_id", "blocked", "next")},
            {"client_id": "a b", "blocked": "1", "next": "/example/?page=2"},
        )
        self.assertIn("csrfmiddlewaretoken", sent)
        self.assertIn("__retry_key", sent)

    def test_a_next_among_its_fields_is_where_it_returns(self):
        html = self.draw(
            PageAction("Block", "/example/block/", method="post", fields={"id": "1", "next": "/example/#row-1"})
        )

        self.assertEqual(self.sent(html)["next"], "/example/#row-1")

    def test_a_post_without_fields_is_still_the_shared_form_s_button(self):
        html = self.draw(PageAction("Read", "/example/read/", method="post", value="read"))

        self.assertEqual(elements(html, "form"), [])
        (button,) = elements(html, "button")
        self.assertEqual(button.attrs["formaction"], "/example/read/")

    def test_only_a_post_sends_fields_and_never_its_button_s_own(self):
        with self.assertRaises(ValueError):
            PageAction("Open", "/example/", fields={"id": "1"})
        with self.assertRaises(ValueError):
            PageAction("Block", "/example/", method="post", field="blocked", fields={"blocked": "1"})
