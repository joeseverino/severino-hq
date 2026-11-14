"""What every family of the catalog draws its entries through: the entry,
and the page of its own it is drawn on."""

from dataclasses import dataclass, field
from typing import Any, Literal, override
from unittest import mock

from django.forms import BaseForm
from django.template import Context, Template
from django.template.context import RenderContext
from django.template.defaulttags import CsrfTokenNode
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from hq.platform.application.pages import Page
from hq.platform.core.templatetags import write_tags
lazy from hq.platform.core.templatetags.stylesheet_tags import stylesheets

# The retry key every example form carries. The catalog draws no CSRF token,
# so an entry's form can act nowhere, and one fixed key makes each entry the
# same markup on every run, the image's gallery with it.
EXAMPLE_KEY = "web:example"

# The moment every entry is drawn at, and its example times are told from,
# so a time a part counts from now draws the same between two renders.
DRAWN_AT = timezone.now()


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

    def drawn(self, name, page: RenderContext | None = None):
        """The entry under its name, drawn as a part of ``page``, the state one
        render of a page keeps (the section ids it drew), where it shares one."""

        with (
            override_settings(**self.settings),
            mock.patch.object(write_tags, "new_key", return_value=EXAMPLE_KEY),
            mock.patch.object(write_tags, "CsrfTokenNode", _NoToken),
            mock.patch.object(timezone, "now", return_value=DRAWN_AT),
        ):
            head = "" if self.heads_the_page or self.frame else head_of(name)
            # A form names its fields' ids after the entry, as a page drawing
            # two forms gives each its own.
            for form in (value for value in self.context.values() if isinstance(value, BaseForm)):
                form.auto_id = f"{slugify(name)}-%s"
            context = _Asked(self.context, self.address) if self.address else Context(self.context)
            if page is not None:
                context.render_context = page
            return head + Template(self.template).render(context)

    def page(self, name):
        """The entry on a page of its own: in its main, or beside it for a part of the frame."""

        drawn = self.drawn(name)
        return part_page(name, head_of(name), **{self.frame: drawn}) if self.frame else part_page(name, drawn)


def head_of(name):
    """The page head every page draws, titled `name`."""

    return render_to_string(HEAD, {"page": Page(title=name)})


class _NoToken(CsrfTokenNode):
    """The CSRF field of a form that may act nowhere: nothing, where Django's
    own node, given no token, warns that one went missing."""

    @override
    def render(self, context):
        return ""


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
