"""What a rendered page offers, read the way a browser reads it: links, hooks and text."""

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import override

_VOID = frozenset({"area", "br", "col", "hr", "img", "input", "link", "meta", "source", "wbr"})
# The spans `|breakable` writes for a token, which are no edge in its text.
_TOKEN_PARTS = frozenset({"strut", "token", "cut", "whole", "part", "joined", "seam", "mark"})
# Elements whose text is their name or their value: a mark held inside one is the sentence's.
_NAMED = frozenset({"a", "button", "code"})


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    text: str = ""
    ancestors: tuple[Element, ...] = field(default=(), repr=False, compare=False)
    chunks: list[str] = field(default_factory=list, repr=False, compare=False)
    # In the line beneath a row (`data-row-detail`), which a reader meets only
    # once the row is opened: a copy of what the row holds back.
    beneath: bool = field(default=False, repr=False, compare=False)

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
        self._token: Element | None = None
        # Whether an element's edge, other than a token's own, stands between
        # this data and the last.
        self._edge = True

    @override
    def handle_starttag(self, tag, attrs):
        element = Element(tag, {name: value or "" for name, value in attrs}, ancestors=tuple(self._open))
        element.beneath = "data-row-detail" in element.attrs or any(open.beneath for open in self._open)
        self.elements.append(element)
        self._edge = self._edge or not _TOKEN_PARTS & element.classes
        if tag not in _VOID:
            self._open.append(element)

    @override
    def handle_endtag(self, tag):
        for depth in range(len(self._open) - 1, -1, -1):
            if self._open[depth].tag == tag:
                self._edge = self._edge or not _TOKEN_PARTS & self._open[depth].classes
                del self._open[depth:]
                return

    @override
    def handle_data(self, data):
        # A token (`|breakable`) marks where it may wrap with markup; read, it
        # is one word, with any text it touches.
        # The mark it holds to a value is read with the word it ends.
        token = next((element for element in self._open if {"token", "mark"} & element.classes), None)
        joined = self._joins(token, data)
        self._token = token
        self._edge = False
        beneath = any(element.beneath for element in self._open)
        if joined:
            self.words[-1] += data
        elif not beneath:
            self.words.append(data)
        # Text hidden from the accessibility tree is no part of the name of a
        # link or a button around it (a mark held to its name), though a
        # reader sees it.
        hidden = next((at for at, element in enumerate(self._open) if element.attrs.get("aria-hidden") == "true"), None)
        for at, element in enumerate(self._open):
            if hidden is not None and at < hidden and element.tag in _NAMED:
                continue
            if beneath and not element.beneath:
                continue
            element.text += data
            if joined and element.chunks:
                element.chunks[-1] += data
            else:
                element.chunks.append(data)

    def _joins(self, token: Element | None, data: str) -> bool:
        """Whether ``data`` continues the last word: the same token, or text touching one."""

        if token is not None and token is self._token:
            return True
        if token is None and self._token is None:
            return False
        return not (self._edge or not self.words or self.words[-1][-1:].isspace() or data[:1].isspace())


def _page(html) -> _Page:
    page = _Page()
    page.feed(html.content.decode() if hasattr(html, "content") else str(html))
    page.close()
    return page


def _shown(html) -> list[Element]:
    """Every element a reader meets before opening a row, in page order."""

    return [element for element in _page(html).elements if not element.beneath]


def all_elements(html) -> list[Element]:
    """Every element, in page order, the line beneath each row included."""

    return _page(html).elements


def select(html, attribute: str, value: str | None = None) -> list[Element]:
    """Every element carrying ``attribute`` (equal to ``value`` when given)."""

    return [
        element
        for element in _shown(html)
        if attribute in element.attrs and (value is None or element.attrs[attribute] == value)
    ]


def elements(html, tag: str) -> list[Element]:
    """Every ``tag`` element, in page order."""

    return [element for element in _shown(html) if element.tag == tag]


def with_class(html, name: str) -> list[Element]:
    """Every element whose class list includes ``name``, in page order."""

    return [element for element in _shown(html) if name in element.classes]


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

    return [element for element in _shown(html) if any(holds(ancestor) for ancestor in element.ancestors)]


def links(html) -> list[tuple[str, str]]:
    """Each link's words and its target, in page order."""

    return [
        (element.words, element.attrs["href"])
        for element in _shown(html)
        if element.tag == "a" and "href" in element.attrs
    ]


def texts(html, tag: str) -> list[str]:
    """The words of each ``tag`` element, whitespace collapsed, in page order."""

    return [element.words for element in elements(html, tag)]


def hrefs(html) -> list[str]:
    """Every link's target, in page order."""

    return [element.attrs["href"] for element in _shown(html) if element.tag == "a" and "href" in element.attrs]


def actions(html) -> list[str]:
    """Every form's action, in page order."""

    return [element.attrs.get("action", "") for element in _shown(html) if element.tag == "form"]


def text(html) -> str:
    """The words a reader sees, whitespace collapsed."""

    return " ".join(" ".join(_page(html).words).split())
