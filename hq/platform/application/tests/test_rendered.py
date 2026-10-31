"""Every page, as rendered, against the rules a page cannot be allowed to break.

The interface checks read source, and source can say one thing in many ways; a
page is one thing. This signs in, seeds the example estate, starts from every
route that takes no argument and follows the links it finds, so detail pages
are reached without anyone listing them. Extensions installed alongside HQ are
crawled the same way, which is how the extension gate covers their pages.

Every page it reaches must answer, and each must have one heading, no form inside a form, no id used
twice, and no em dash or count that disagrees with its noun ("1 resources",
"account(s)") in what it says.
"""

import re
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.core import example_estate
from hq.platform.core.example_estate import names, records
from hq.platform.core.example_estate.testing import throwaway_media

from ..routes import routed

# Routes that are not pages: machine endpoints, downloads, polled status, and
# anything that acts when fetched.
SKIPPED = re.compile(
    r"^/(api|mcp|static|media|accounts|oidc|health|csp-report)/"
    r"|/(export|download|file|report|count|glance|logout|refresh|callback|complete|status)/"
    r"|\.(json|csv|md|pem|txt|ics)$"
)
PAGE_LIMIT = 400

# Words ending in "s" that are not plurals, so "1 needs" and "1 status" pass.
NOT_PLURAL = frozenset(
    {
        "access",
        "across",
        "address",
        "alias",
        "always",
        "analysis",
        "bonus",
        "canvas",
        "class",
        "does",
        "focus",
        "has",
        "is",
        "its",
        "less",
        "lens",
        "news",
        "needs",
        "pass",
        "plus",
        "process",
        "series",
        "status",
        "this",
        "unless",
        "was",
        "yes",
        "ms",
    }
)
ONE_PLURAL = re.compile(r"(?<![\d.,:/-])\b1 ([a-z]+s)\b")
BRACKETED = re.compile(r"[a-z]\((?:s|es|ies)\)")


def _page_routes():
    """Every route that takes no argument, as its path."""

    for route in routed():
        joined = route.path.removeprefix("/")
        if "<" in joined or "(?P" in joined:
            continue
        path = "/" + joined.lstrip("^").rstrip("$")
        if not SKIPPED.search(path):
            yield path


# Elements with no end tag, so never left open.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.form_depth = 0
        self.nested_forms = 0
        self.ids: dict[str, int] = {}
        self.h1 = 0
        self.links: list[str] = []
        self.text: list[str] = []
        self._hidden = 0
        # Open elements, each marked when it is a sideways scroll container.
        self._open: list[tuple[str, bool]] = []
        self.unwrapped_tables = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "table" and "data-table" in classes and not any(scroll for _tag, scroll in self._open):
            self.unwrapped_tables += 1
        if tag not in _VOID:
            self._open.append((tag, "table-scroll" in classes))
        if tag == "form":
            self.form_depth += 1
            self.nested_forms += self.form_depth > 1
        if tag == "h1":
            self.h1 += 1
        if attrs.get("id"):
            self.ids[attrs["id"]] = self.ids.get(attrs["id"], 0) + 1
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag in {"script", "style", "template"}:
            self._hidden += 1

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                break
        if tag == "form":
            self.form_depth = max(self.form_depth - 1, 0)
        if tag in {"script", "style", "template"}:
            self._hidden = max(self._hidden - 1, 0)

    def handle_data(self, data):
        if not self._hidden:
            self.text.append(data)


class RenderedPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            "rendered-pages", "rendered@example.test", "unused-password"
        )
        with throwaway_media():
            example_estate.seed()

    def setUp(self):
        self.client.force_login(self.user)

    def crawl(self, refused: list[str]):
        routes = set(_page_routes())
        queue = deque(sorted(routes))
        seen: set[str] = set()
        while queue and len(seen) < PAGE_LIMIT:
            path = queue.popleft()
            if path in seen or SKIPPED.search(path):
                continue
            seen.add(path)
            response = self.client.get(path)
            if response.status_code in {301, 302, 303, 307, 308}:
                queue.append(urlsplit(response["Location"]).path)
                continue
            # A route that takes no argument and refuses a GET is an action, not a page.
            if response.status_code == 405 and path in routes:
                continue
            if response.status_code != 200:
                refused.append(f"{path}: answered {response.status_code}")
                continue
            if "text/html" not in response.get("Content-Type", ""):
                continue
            html = response.content.decode()
            page = _Page()
            page.feed(html)
            # A fragment fetched into another page (a deferred panel) is judged
            # as part of that page, not as a page of its own.
            if "<main" in html:
                yield path, page
            for href in page.links:
                parts = urlsplit(href)
                if parts.scheme or parts.netloc or not parts.path.startswith("/"):
                    continue
                if parts.query or SKIPPED.search(parts.path) or parts.path in seen:
                    continue
                queue.append(parts.path)

    def test_every_page_keeps_the_rules(self):
        problems: list[str] = []
        crawled = set()
        for path, page in self.crawl(problems):
            crawled.add(path)
            text = " ".join(" ".join(page.text).split())
            if page.h1 != 1:
                problems.append(f"{path}: {page.h1} h1 elements")
            if page.nested_forms:
                problems.append(f"{path}: a form inside a form")
            # A table is the one thing allowed to scroll sideways, and only
            # inside its own scroll box: unwrapped, a phone scrolls the page.
            if page.unwrapped_tables:
                problems.append(f"{path}: a data table outside .table-scroll")
            doubled = sorted(name for name, count in page.ids.items() if count > 1)
            if doubled:
                problems.append(f"{path}: ids used twice: {', '.join(doubled[:5])}")
            if "\u2014" in text:
                at = text.index("\u2014")
                problems.append(f"{path}: em dash in {text[max(at - 30, 0) : at + 30]!r}")
            # "-ous" words are adjectives ("1 serious"), not plurals.
            problems.extend(
                f'{path}: "1 {noun}"'
                for noun in ONE_PLURAL.findall(text)
                if noun not in NOT_PLURAL and not noun.endswith("ous")
            )
            if BRACKETED.search(text):
                problems.append(f"{path}: plural in brackets: {BRACKETED.search(text).group()!r}")
        self.assertGreater(len(crawled), 20, "The crawl reached too few pages to mean anything.")
        # The estate's own records are reached from the pages, not listed.
        estate_pages = {
            reverse("control_plane:machine", args=[names.MACHINE]),
            *(reverse("projects:detail", args=[slug]) for _name, slug, *_rest in records.PROJECTS),
        }
        self.assertEqual(estate_pages - crawled, set())
        self.assertEqual(problems, [])
