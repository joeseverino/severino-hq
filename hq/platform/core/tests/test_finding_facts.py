from django.test import SimpleTestCase

from hq.platform.application.action_links import read_link
from hq.platform.application.dashboard import finding_rows
from hq.platform.application.decisions import decision
from hq.platform.application.entity_links import EntityLink, entity_link
from hq.platform.application.finding_model import Finding
from hq.platform.application.findings import serialize_finding
from hq.platform.application.markup_testing import elements, with_class
from hq.platform.application.tone import Tone
from hq.platform.core.testing import render

_MACHINE = entity_link("machine", "example-host-01")
_PULL = EntityLink(
    "example-org/example-app#26", "https://github.example/example-org/example-app/pull/26", external=True
)


def _finding(*evidence, pages=()) -> Finding:
    return Finding(
        "example", "example-host-01", "example-host-01 waits", Tone.ATTENTION, "Example.", evidence, offers=pages
    )


def _opened(finding: Finding) -> dict:
    (item,) = finding_rows("example", "Example", (finding,))
    return decision(item)


class FactLinkTests(SimpleTestCase):
    """A fact that names a record is a link to it at every door, and only a web or HQ address is one."""

    def test_a_record_is_linked_in_the_answer_and_on_the_row(self):
        finding = _finding(("Machine", _MACHINE), ("Pull request", _PULL), ("Packages", "example-kernel"))

        urls = {fact["label"]: fact["url"] for fact in serialize_finding(finding)["evidence"]}
        self.assertEqual(urls, {"Machine": _MACHINE.url, "Pull request": _PULL.url, "Packages": ""})
        drawn = render('{% include "partials/_insight_facts.html" %}', facts=_opened(finding)["facts"])
        links = {link.words: link.attrs["href"] for link in elements(drawn, "a")}
        self.assertEqual(links, {"example-host-01": _MACHINE.url, "example-org/example-app#26": _PULL.url})

    def test_a_link_that_is_no_web_address_is_drawn_as_words(self):
        finding = _finding(("Page", EntityLink("example", "javascript:alert(1)")))

        drawn = render('{% include "partials/_insight_facts.html" %}', facts=_opened(finding)["facts"])
        self.assertEqual(elements(drawn, "a"), [])
        self.assertIn("example", drawn)


class PageLinkTests(SimpleTestCase):
    """Each page a row links to is said in words, an icon only ever beside them."""

    def test_every_link_says_what_it_opens(self):
        pages = (
            read_link("topology", "Map neighborhood", "/example/topology/"),
            read_link("open", "Open example-host-01", "/example/machines/example-host-01/"),
        )

        drawn = render('{% include "partials/_icon_links.html" %}', links=_opened(_finding(pages=pages))["links"])
        self.assertEqual([link.words for link in elements(drawn, "a")], [page.label for page in pages])
        self.assertEqual(with_class(drawn, "visually-hidden"), [])
