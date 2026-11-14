"""The filter bar: every query a page asks of what it lists, in one bar.

``{% filter_bar filtered=narrowed clear=every_url shown=3 total=12 %}<input type="search" name="q">{% choices %}{% select "kind" kinds every="kind" %}{% endfilter_bar %}``
draws ``partials/_filter_bar.html``. A list's table toolbar is the same bar
(``partials/_table_toolbar.html``), never a second one.
"""

import hashlib
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import override

from django import template
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.templatetags.value_tags import slotted

register = template.Library()

FILTER_BAR_PARTIAL = "partials/_filter_bar.html"


def filter_bar_frame(name: str, given: dict) -> dict:
    """What the partial draws from what a page gave: its classes, the verb
    that sends it (Filter unless told, and Apply where it stands with choices
    that fold, so it never stands beside the Filters control that opens
    them), the way back to everything when it
    narrows, its count and what it holds back, and its ``data-`` attributes."""

    filtered = bool(given.get("filtered"))
    if filtered and not given.get("clear"):
        raise template.TemplateSyntaxError(f"{name} filtered=True is given clear=, the page with nothing narrowed")
    shown, total = (given.get(option) for option in ("shown", "total"))
    counted = shown not in (None, "")
    if counted != (total not in (None, "")):
        raise template.TemplateSyntaxError(f"{name} takes shown= and total= together")
    held = tuple(given.get("held") or ())
    if held and not given.get("more"):
        raise template.TemplateSyntaxError(f"{name} held= is given more=, the page that shows what it holds back")
    return {
        "classes": " ".join(("filter-bar", given.get("css") or "")).strip(),
        "action": given.get("action") or "",
        "verb": given.get("verb") or "Filter",
        "applies": given.get("verb") or "Apply",
        "clear": given.get("clear") if filtered else "",
        "shown": f"{shown:,}" if counted else "",
        "total": f"{total:,}" if counted else "",
        "held": held,
        "more": given.get("more") if held else "",
        "named": given.get("id") or "",
        "data": html_attributes(
            {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


slotted(
    "filter_bar",
    FILTER_BAR_PARTIAL,
    ("content", "choices", "summary"),
    frozenset(("filtered", "clear", "verb", "action", "css", "shown", "total", "held", "more", "id")),
    shape=filter_bar_frame,
    library=register,
)


@dataclass(frozen=True)
class Fold:
    """What the Filters control that folds a bar's choices needs: the id of
    the panel it opens, and how many of the choices are set, so a folded bar
    never hides that its list is narrowed."""

    id: str
    set: int


@dataclass
class _Group:
    """A group of checkboxes counted as one choice: its element, whether a
    checked box narrows or an unchecked one, whether one does, and how deep
    the parse is inside it."""

    tag: str
    narrows_checked: bool
    narrowed: bool = False
    depth: int = 1


class _SetChoices(HTMLParser):
    """Counts the choices that narrow what the page sends for: a select whose
    empty choice ("Any ...", from ``every=``) is not the one chosen, a menu
    of checkboxes (a table's filter) with any checked, a set of toggles
    (``_toggle_set.html``, each a thing to show) with any off, and a lone
    checkbox that is checked. A select with no empty choice picks a view or
    an order; an unnamed checkbox narrows only what the page's script holds."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.set = 0
        self.names: list[str] = []
        self._options: list[tuple[str, bool]] | None = None
        self._group: _Group | None = None

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        named = dict(attrs)
        if named.get("name"):
            self.names.append(str(named["name"]))
        classes = (named.get("class") or "").split()
        if self._group is not None and tag == self._group.tag:
            self._group.depth += 1
        if tag == "select":
            self._options = []
        elif tag == "option" and self._options is not None:
            self._options.append((named.get("value") or "", "selected" in named))
        elif self._group is None and ("table-filter-menu" in classes or "toggle-set" in classes):
            self._group = _Group(tag, narrows_checked="toggle-set" not in classes)
        elif tag == "input" and named.get("type") == "checkbox" and named.get("name"):
            checked = "checked" in named
            if self._group is None:
                self.set += checked
            elif checked == self._group.narrows_checked:
                self._group.narrowed = True

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._options is not None:
            chosen = next((value for value, selected in self._options if selected), "")
            if chosen and any(not value for value, _ in self._options):
                self.set += 1
            self._options = None
        elif self._group is not None and tag == self._group.tag:
            self._group.depth -= 1
            if not self._group.depth:
                self.set += self._group.narrowed
                self._group = None


@register.filter
def folded(choices: str, named: str = "") -> Fold:
    """The fold of a bar's ``{% choices %}``: its panel's id, drawn from the
    bar's ``id=`` where a page draws two bars sending the same names, else from
    the names they send, so the panel a reader opened is the same one in the
    answer to a choice (fragment.js raises it again by its id), and how many
    of them are set."""

    counted = _SetChoices()
    counted.feed(str(choices))
    counted.close()
    digest = hashlib.blake2b(" ".join(dict.fromkeys(counted.names)).encode(), digest_size=4).hexdigest()
    return Fold(f"filter-bar-choices-{named or digest}", counted.set)


# What makes a control one of the page's: what a form sends it as, what a
# label, a script or a link finds it by, and what it does when pressed.
_HOOKS = frozenset(("id", "name", "for", "href", "popovertarget", "autofocus", "form"))


class _Unnamed(HTMLParser):
    """Markup again, each tag without the attributes that make it a working
    control of the page: ``_HOOKS`` and every ``data-`` hook."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        kept = "".join(
            f" {name}" if value is None else f' {name}="{escape(value)}"'
            for name, value in attrs
            if name not in _HOOKS and not name.startswith("data-")
        )
        self.parts.append(f"<{tag}{kept}>")

    @override
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    @override
    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    @override
    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    @override
    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    @override
    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")


@register.filter
def unnamed(markup: str) -> SafeString:
    """``markup`` copied as no working control: what a bar's sizer draws to
    hold the room its choices take unfolded, which nothing sends, labels,
    finds or presses."""

    copied = _Unnamed()
    copied.feed(str(markup))
    copied.close()
    return mark_safe("".join(copied.parts))  # noqa: S308 - the bar's own escaped markup, each value escaped again as it is written
