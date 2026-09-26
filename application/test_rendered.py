"""Every page, as rendered, against the rules a page cannot be allowed to break.

The interface checks read source, and source can say one thing in many ways; a
page is one thing. This signs in, seeds HQ's demo records, starts from every
route that takes no argument and follows the links it finds, so detail pages
are reached without anyone listing them. Extensions installed alongside HQ are
crawled the same way, which is how the extension gate covers their pages.

Each rendered page must have one heading, no form inside a form, no id used
twice, and no em dash or count that disagrees with its noun ("1 resources",
"account(s)") in what it says.
"""

from __future__ import annotations

import re
from io import StringIO
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver

# Routes that are not pages: machine endpoints, downloads, and anything that
# acts when fetched.
SKIPPED = re.compile(
    r"^/(api|mcp|static|media|admin|accounts|oidc|health|csp-report)/"
    r"|/(export|download|report|count|glance|logout|refresh|callback|complete)/"
    r"|\.(json|csv|md|pem|txt|ics)$"
)
PAGE_LIMIT = 400

# Words ending in "s" that are not plurals, so "1 needs" and "1 status" pass.
NOT_PLURAL = frozenset(
    {
        "access", "across", "address", "alias", "always", "analysis", "bonus",
        "canvas", "class", "does", "focus", "has", "is", "its", "less", "lens",
        "news", "needs", "pass", "plus", "process", "series", "status", "this",
        "unless", "was", "yes", "ms",
    }
)
ONE_PLURAL = re.compile(r"(?<![\d.,:/-])\b1 ([a-z]+s)\b")
BRACKETED = re.compile(r"[a-z]\((?:s|es|ies)\)")


def _page_routes(patterns=None, prefix=""):
    for entry in get_resolver().url_patterns if patterns is None else patterns:
        pattern = str(entry.pattern)
        if isinstance(entry, URLResolver):
            if "<" not in pattern and "(?P" not in pattern:
                yield from _page_routes(entry.url_patterns, prefix + pattern)
        elif isinstance(entry, URLPattern) and "<" not in pattern and "(?P" not in pattern:
            path = "/" + (prefix + pattern).lstrip("^").rstrip("$")
            if not SKIPPED.search(path):
                yield path


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

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
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
        call_command("seed_demo", stdout=StringIO())

    def setUp(self):
        self.client.force_login(self.user)

    def crawl(self):
        queue = deque(sorted(set(_page_routes())))
        seen: set[str] = set()
        while queue and len(seen) < PAGE_LIMIT:
            path = queue.popleft()
            if path in seen:
                continue
            seen.add(path)
            response = self.client.get(path)
            if response.status_code != 200 or "text/html" not in response.get(
                "Content-Type", ""
            ):
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
        problems = []
        crawled = 0
        for path, page in self.crawl():
            crawled += 1
            text = " ".join(" ".join(page.text).split())
            if page.h1 != 1:
                problems.append(f"{path}: {page.h1} h1 elements")
            if page.nested_forms:
                problems.append(f"{path}: a form inside a form")
            doubled = sorted(name for name, count in page.ids.items() if count > 1)
            if doubled:
                problems.append(f"{path}: ids used twice: {', '.join(doubled[:5])}")
            if "—" in text:
                at = text.index("—")
                problems.append(f"{path}: em dash in {text[max(at - 30, 0):at + 30]!r}")
            for noun in ONE_PLURAL.findall(text):
                # "-ous" words are adjectives ("1 serious"), not plurals.
                if noun not in NOT_PLURAL and not noun.endswith("ous"):
                    problems.append(f"{path}: \"1 {noun}\"")
            if BRACKETED.search(text):
                problems.append(f"{path}: plural in brackets: {BRACKETED.search(text).group()!r}")
        self.assertGreater(crawled, 20, "The crawl reached too few pages to mean anything.")
        self.assertEqual(problems, [])
