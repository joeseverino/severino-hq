"""What a rendered page offers, read the way a browser reads it: links, hooks and text."""

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import override

_VOID = frozenset({"area", "br", "col", "hr", "img", "input", "link", "meta", "source", "wbr"})


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    text: str = ""
    ancestors: tuple[Element, ...] = field(default=(), repr=False, compare=False)
    chunks: list[str] = field(default_factory=list, repr=False, compare=False)

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    @property
    def words(self) -> str:
        """The element's text as a reader sees it: whitespace collapsed, a space between elements."""

        return " ".join(" ".join(self.chunks).split())


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self.words: list[str] = []
        self._open: list[Element] = []

    @override
    def handle_starttag(self, tag, attrs):
        element = Element(tag, {name: value or "" for name, value in attrs}, ancestors=tuple(self._open))
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
            element.chunks.append(data)


def _page(html) -> _Page:
    page = _Page()
    page.feed(html.content.decode() if hasattr(html, "content") else str(html))
    page.close()
    return page


def all_elements(html) -> list[Element]:
    """Every element, in page order."""

    return _page(html).elements


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


def with_class(html, name: str) -> list[Element]:
    """Every element whose class list includes ``name``, in page order."""

    return [element for element in _page(html).elements if name in element.classes]


def within(html, attribute: str, value: str | None = None) -> list[Element]:
    """Every element inside one carrying ``attribute``, in page order.

    With ``value``, only inside one whose attribute equals it; for ``class``,
    one whose class list includes it.
    """

    def holds(ancestor: Element) -> bool:
        if attribute not in ancestor.attrs:
            return False
        if value is None:
            return True
        return value in ancestor.classes if attribute == "class" else ancestor.attrs[attribute] == value

    return [element for element in _page(html).elements if any(holds(ancestor) for ancestor in element.ancestors)]


def links(html) -> list[tuple[str, str]]:
    """Each link's words and its target, in page order."""

    return [
        (element.words, element.attrs["href"])
        for element in _page(html).elements
        if element.tag == "a" and "href" in element.attrs
    ]


def texts(html, tag: str) -> list[str]:
    """The words of each ``tag`` element, whitespace collapsed, in page order."""

    return [element.words for element in elements(html, tag)]


def hrefs(html) -> list[str]:
    """Every link's target, in page order."""

    return [element.attrs["href"] for element in _page(html).elements if element.tag == "a" and "href" in element.attrs]


def actions(html) -> list[str]:
    """Every form's action, in page order."""

    return [element.attrs.get("action", "") for element in _page(html).elements if element.tag == "form"]


def text(html) -> str:
    """The words a reader sees, whitespace collapsed."""

    return " ".join(" ".join(_page(html).words).split())
