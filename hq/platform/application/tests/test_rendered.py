"""Every page, as rendered, against the rules a page cannot be allowed to break.

The interface checks read source, and source can say one thing in many ways; a
page is one thing. This signs in, seeds the example estate, starts from every
route that takes no argument and follows the links it finds, so detail pages
are reached without anyone listing them. Extensions installed alongside HQ are
crawled the same way, which is how the extension gate covers their pages.

Every page it reaches must answer, and each must have one heading, no form inside a form, no id used
twice, and no em dash or count that disagrees with its noun ("1 resources",
"account(s)") in what it says. No answer, a fragment's included, writes a
style: HQ's policy takes one only from a sheet of its own origin.
"""

import re
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from hq.platform.api.testing import ContractClient
from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.core import example_estate, revisions
from hq.platform.core.example_estate import names, records
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.testing import SignedInTestCase

from ..routes import routed
from .addresses import address

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


def _routes():
    """Every route, with the path it answers: each argument a value its converter takes."""

    for route in routed():
        if "(?P" in route.path:
            yield route, ""
            continue
        yield route, re.sub(r"[\^$]", "", address(route.path))


def _page_routes():
    """Every route that takes no argument, as its path."""

    for route, path in _routes():
        if path and "<" not in route.path and not SKIPPED.search(path):
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
        # Each element that writes a style the policy refuses.
        self.styled: list[str] = []
        # Each open template, marked when it is a shadow root. What a shadow
        # root holds is a tree of its own (the Design page's examples, each in
        # the frame the component catalog holds it to): its headings, ids,
        # forms, links and styles are no part of the page's.
        self._templates: list[bool] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "table" and "data-table" in classes and not any(scroll for _tag, scroll in self._open):
            self.unwrapped_tables += 1
        if tag not in _VOID:
            self._open.append((tag, "table-scroll" in classes))
        if tag in {"script", "style", "template"}:
            self._hidden += 1
        if tag == "template":
            self._templates.append("shadowrootmode" in attrs)
        if any(self._templates):
            return
        if tag == "style":
            self.styled.append("<style>")
        elif "style" in attrs:
            self.styled.append(f'<{tag} style="{(attrs["style"] or "")[:80]}">')
        if tag == "form":
            self.form_depth += 1
            self.nested_forms += self.form_depth > 1
        if tag == "h1":
            self.h1 += 1
        if attrs.get("id"):
            self.ids[attrs["id"]] = self.ids.get(attrs["id"], 0) + 1
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                break
        if tag in {"script", "style", "template"}:
            self._hidden = max(self._hidden - 1, 0)
        if tag == "template" and self._templates:
            self._templates.pop()
            return
        if tag == "form" and not any(self._templates):
            self.form_depth = max(self.form_depth - 1, 0)

    def handle_data(self, data):
        if not self._hidden:
            self.text.append(data)


def _onward(links, seen: set[str]):
    """Each page of this site a page links to that the crawl has yet to read, by its path."""

    for href in links:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc or not parts.path.startswith("/"):
            continue
        if parts.query or SKIPPED.search(parts.path) or parts.path in seen:
            continue
        yield parts.path


class RenderedPageTests(SignedInTestCase):
    # The failure is the list of what the pages got wrong, said whole.
    maxDiff = None

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        with throwaway_media():
            example_estate.seed()

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
            # A route that refuses a GET is an action, not a page (GetHasNoEffectTests).
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
            if page.styled:
                refused.append(f"{path}: writes a style the policy refuses: {', '.join(page.styled[:5])}")
            # A fragment fetched into another page (a deferred panel) is judged
            # as part of that page, not as a page of its own.
            if "<main" in html:
                yield path, page
            queue.extend(_onward(page.links, seen))

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


def _declares_no_get(route) -> bool:
    """Whether a route's view says it answers no GET: an endpoint's methods, or a view class's."""

    methods = getattr(route.pattern.callback, "__hq_methods__", None)
    if methods is not None:
        return "GET" not in methods
    view = route.view
    return isinstance(view, type) and not ("get" in view.http_method_names and hasattr(view, "get"))


def _get(client: Client, path: str, **extra) -> tuple[HttpResponse, list[str]]:
    """``client``'s GET of ``path``, and each table it wrote."""

    before = revisions.read().counts
    response = client.get(path, **extra)
    after = revisions.read().counts
    return response, sorted(table for table in before.keys() | after.keys() if before.get(table) != after.get(table))


def _refused(response: HttpResponse) -> bool:
    """A method refusal, or the sign-in gate turning away somebody who has not signed in."""

    if response.status_code == 405:
        return True
    return response.status_code == 302 and response["Location"].startswith(settings.LOGIN_URL)


# The machine API configured, so its routes answer as they do when served.
@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE)
class GetHasNoEffectTests(TestCase):
    """A GET to a route that answers only POST is refused and writes nothing, whoever sends it.

    The routes are the URL configuration's, an installed extension's included.
    Which answer only POST is each one's own answer to an operator's GET, and a
    view that says it answers no GET is held to answering so.
    """

    maxDiff = None

    def test_a_get_to_an_action_is_refused_and_writes_nothing(self):
        operator = Client(raise_request_exception=False)
        operator.force_login(get_user_model().objects.create_superuser("example-operator"))
        anonymous = Client(raise_request_exception=False)
        problems: list[str] = []
        actions = 0
        for route, path in _routes():
            if not path:
                problems.append(f"{route.path}: no argument the walk can fill")
                continue
            answer, wrote = _get(operator, path)
            if answer.status_code != 405:
                if _declares_no_get(route):
                    problems.append(f"{path}: says it answers no GET, and answered one {answer.status_code}")
                continue
            actions += 1
            callers = [("an operator", answer, wrote), ("somebody signed out", *_get(anonymous, path))]
            if hasattr(route.pattern.callback, "__hq_methods__"):
                # Its answer held to the API's document as well, as every API test's is.
                with _serving():
                    bearer = _get(ContractClient(), path, headers={"Authorization": f"Bearer {_token()}"})
                callers.append(("an agent", *bearer))
            for caller, response, tables in callers:
                if not _refused(response):
                    problems.append(f"{path}: a GET from {caller} answered {response.status_code}")
                if tables:
                    problems.append(f"{path}: a GET from {caller} wrote {', '.join(tables)}")
        self.assertTrue(actions, "The walk found no route that answers only POST.")
        self.assertEqual(problems, [])
