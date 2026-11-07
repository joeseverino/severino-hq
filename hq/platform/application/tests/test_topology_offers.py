"""A page that shows a declaration offers no more of its edit and removal
than the page the declaration lives on."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from .markup import actions, hrefs

ROOT = Path(__file__).resolve().parents[4]
# The edit and removal routes, which only ``resource_capabilities.Offered`` builds a link to.
_ROUTES = re.compile(r"""["']control_plane:(?:edit|remove)["']""")
# A file that names a route without offering it: a benchmark visits each
# page, and the example estate records the door its rows came through.
_NAMES_WITHOUT_OFFERING = {
    "hq/platform/application/resource_capabilities.py",
    "hq/platform/core/management/commands/bench_pages.py",
    "hq/platform/core/example_estate/history.py",
}


def _offers(html, routes: dict[str, str]) -> dict[str, set[str]]:
    """The edit and removal addresses a page offers, by the declaration each
    acts on; ``routes`` maps each such address to its declaration's key."""

    found: dict[str, set[str]] = {}
    for address in (*hrefs(html), *actions(html)):
        path = urlsplit(address).path
        if path in routes:
            found.setdefault(routes[path], set()).add(path)
    return found


class PageOffersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def setUp(self):
        self.client.force_login(self.owner)
        self.keys = sorted(ManagedResource.objects.values_list("key", flat=True))
        self.routes = {
            reverse(f"control_plane:{route}", kwargs={"key": key}): key
            for key in self.keys
            for route in ("edit", "remove")
        }

    def _own(self, key: str) -> set[str]:
        page = self.client.get(reverse("control_plane:detail", kwargs={"key": key}), follow=True)
        self.assertEqual(page.status_code, 200)
        return _offers(page, self.routes).get(key, set())

    def _listed(self, index: str, prefix: str) -> list[str]:
        """Each page the list at ``index`` links under ``prefix``, and the
        one it opens on when it leads straight to it."""

        page = self.client.get(index, follow=True)
        self.assertEqual(page.status_code, 200)
        return sorted(
            path
            for path in {page.request["PATH_INFO"], *(urlsplit(href).path for href in hrefs(page))}
            if path.startswith(prefix) and path.count("/") == prefix.count("/") + 1
        )

    def _pages(self) -> list[tuple[str, dict]]:
        """Every page that shows other declarations: each node's panel on the
        map, each service, and each domain with its mail."""

        pages = [
            (f"panel {key}", {"path": reverse("control_plane:topology_node"), "data": {"node": f"resource:{key}"}})
            for key in self.keys
        ]
        services = reverse("control_plane:services")
        pages += [(path, {"path": path}) for path in self._listed(services, services)]
        domains = reverse("zones:index")
        for path in self._listed(domains, domains):
            pages += [(path, {"path": path}), (f"{path}mail/", {"path": f"{path}mail/"})]
        return pages

    def test_a_page_offers_only_what_each_declarations_own_page_offers(self):
        shown = 0
        for public_dns in (False, True):
            with override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=public_dns):
                own: dict[str, set[str]] = {}
                for name, request in self._pages():
                    response = self.client.get(request["path"], request.get("data"))
                    if response.status_code != 200:
                        # A declaration the map leaves out has no panel.
                        continue
                    for key, offered in _offers(response, self.routes).items():
                        shown += 1
                        if key not in own:
                            own[key] = self._own(key)
                        with self.subTest(page=name, key=key, public_dns=public_dns):
                            self.assertLessEqual(offered, own[key])
        # The estate's pages offer changes to declarations, so the comparison ran.
        self.assertGreater(shown, len(self.keys))


@tag("source", "tree")
class OfferedLinksTests(SimpleTestCase):
    def test_only_offered_builds_a_declarations_edit_or_removal_link(self):
        files = [*ROOT.joinpath("hq").rglob("*.py"), *ROOT.joinpath("templates").rglob("*.html")]
        naming = {
            relative
            for path in files
            if "tests" not in path.parts
            and (relative := path.relative_to(ROOT).as_posix()) not in _NAMES_WITHOUT_OFFERING
            and _ROUTES.search(path.read_text(encoding="utf-8"))
        }

        self.assertEqual(sorted(naming), [])
        # The guard reads the one file that is allowed to build them.
        self.assertTrue(_ROUTES.search((ROOT / "hq/platform/application/resource_capabilities.py").read_text()))
