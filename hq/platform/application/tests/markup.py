"""What a rendered page offers, read the way a browser reads it: links, hooks and text."""

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import override

_VOID = frozenset({"area", "br", "col", "hr", "img", "input", "link", "meta", "source", "wbr"})


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    text: str = ""


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self.words: list[str] = []
        self._open: list[Element] = []

    @override
    def handle_starttag(self, tag, attrs):
        element = Element(tag, {name: value or "" for name, value in attrs})
        self.elements.append(element)
        if tag not in _VOID:
            self._open.append(element)

    @override
    def handle_endtag(self, tag):
        for depth in range(len(self._open) - 1, -1, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]
                return

    @override
    def handle_data(self, data):
        self.words.append(data)
        for element in self._open:
            element.text += data


def _page(html) -> _Page:
    page = _Page()
    page.feed(html.content.decode() if hasattr(html, "content") else str(html))
    page.close()
    return page


def select(html, attribute: str, value: str | None = None) -> list[Element]:
    """Every element carrying ``attribute`` (equal to ``value`` when given)."""

    return [
        element
        for element in _page(html).elements
        if attribute in element.attrs and (value is None or element.attrs[attribute] == value)
    ]


def elements(html, tag: str) -> list[Element]:
    """Every ``tag`` element, in page order."""

    return [element for element in _page(html).elements if element.tag == tag]


def hrefs(html) -> list[str]:
    """Every link's target, in page order."""

    return [element.attrs["href"] for element in _page(html).elements if element.tag == "a" and "href" in element.attrs]


def actions(html) -> list[str]:
    """Every form's action, in page order."""

    return [element.attrs.get("action", "") for element in _page(html).elements if element.tag == "form"]


def text(html) -> str:
    """The words a reader sees, whitespace collapsed."""

    return " ".join(" ".join(_page(html).words).split())
