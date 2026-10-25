"""A page is first drawn whole: rendering waits until its content is parsed."""

from html.parser import HTMLParser
from typing import override

from django.contrib.auth import get_user_model
from django.test import TestCase


class _Order(HTMLParser):
    """Where the render-blocking target sits against the end of `<main>`."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.targets: list[str] = []
        self.seen: dict[str, bool] = {}
        self.main_closed = False

    @override
    def handle_starttag(self, tag, attrs):
        named = dict(attrs)
        if tag == "link" and named.get("rel") == "expect" and named.get("blocking") == "render":
            self.targets.append(named.get("href", ""))
        if named.get("id"):
            self.seen[named["id"]] = self.main_closed

    @override
    def handle_endtag(self, tag):
        if tag == "main":
            self.main_closed = True


class FirstPaintTests(TestCase):
    def test_rendering_waits_for_an_element_after_the_content(self):
        """A paint of a partly parsed page lets later rows widen a table
        already drawn. Chromium holds rendering for an expect link only until
        the element it names is inserted, so that element must come after
        `</main>`, not be `<main>` or anything in it."""

        user = get_user_model().objects.create_superuser(username="viewer", email="v@example.com", password="x")
        self.client.force_login(user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = _Order()
        page.feed(response.content.decode())
        self.assertTrue(page.main_closed, "the page has no <main>")
        self.assertEqual(len(page.targets), 1, page.targets)
        (target,) = page.targets
        self.assertTrue(target.startswith("#"), target)
        self.assertIs(page.seen.get(target[1:]), True, f"{target} is not an element after </main>")
