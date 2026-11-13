"""What every family of the catalog draws its entries through: the entry,
and the page of its own it is drawn on."""

from dataclasses import dataclass, field
from typing import Any, Literal

from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.utils.html import escape
from django.utils.safestring import mark_safe

from hq.platform.application.pages import Page
lazy from hq.platform.core.templatetags.stylesheet_tags import stylesheets


@dataclass(frozen=True)
class Entry:
    """A part as a template writes it, with what it is given.

    ``premise`` is what the entry must draw to stand for its state, as a probe
    and its argument returning what falls short of it: a value meant to wrap
    that fits on one line holds nothing the survey could find wrong.
    ``heads_the_page`` says the part is a page's own head; any other is drawn
    under one, as every HQ page is. ``frame`` says the part is the page
    frame's own, which base.html draws beside the page's main rather than in
    it, ``"before"`` or ``"after"``, so it is drawn there, beside a main that
    holds a page head alone."""

    template: str
    context: dict[str, Any] = field(default_factory=dict)
    premise: tuple[str, Any] | None = None
    heads_the_page: bool = False
    frame: Literal["", "before", "after"] = ""
    # The address the part is drawn at, for a part that reads its request: a
    # page's query, or the row its address opens.
    address: str = ""
    # The settings the part is drawn under, for a part only some deployments draw.
    settings: dict[str, Any] = field(default_factory=dict)

    def drawn(self, name):
        with override_settings(**self.settings):
            head = "" if self.heads_the_page or self.frame else _head(name)
            context = _Asked(self.context, self.address) if self.address else Context(self.context)
            return head + Template(self.template).render(context)

    def page(self, name):
        """The entry on a page of its own: in its main, or beside it for a part of the frame."""

        drawn = self.drawn(name)
        return part_page(name, _head(name), **{self.frame: drawn}) if self.frame else part_page(name, drawn)


def _head(name):
    return render_to_string(HEAD, {"page": Page(title=name)})


class _Asked(Context):
    """A context drawn for a request at ``address``, which it carries as
    ``{% querystring %}`` and ``{% row_editor %}`` read it."""

    def __init__(self, values, address):
        self.request = RequestFactory().get(address)
        super().__init__({**values, "request": self.request})


HEAD = "partials/_page_head.html"
MAIN = "partials/_page_main.html"


def part_page(name, markup, before="", after=""):
    """A page titled ``name``, of HQ's stylesheets, with ``markup`` alone in
    the content column base.html draws, and the frame's parts it draws before
    and after it."""

    main = render_to_string(MAIN, {"content": mark_safe(markup)})  # noqa: S308 - the catalog's own rendered markup
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(name)}</title>{stylesheets()}</head>"
        f"<body>{before}{main}{after}</body></html>"
    )
