"""What every family of the catalog draws its entries through: an entry,
a part's ``Example``, drawn alone and on the page of its own."""

from typing import override
from unittest import mock

from django.forms import BaseForm
from django.template import Context, Template
from django.template.defaulttags import CsrfTokenNode
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from hq.platform.application import references
from hq.platform.application.entity_links import EntityLink
from hq.platform.application.pages import Page
from hq.platform.core.part_examples import DRAWN_AT
from hq.platform.core.template_parts import Example
from hq.platform.core.templatetags import write_tags
from hq.platform.core.tests.browser.serving import NONCE

# What names a thing, for {% referenced_by %}: the gallery is built with no
# database, so the rows a reference is held in are these.
EXAMPLE_MENTIONS = (
    references.Mentions(
        "Example records",
        (
            references.Mention(EntityLink("example-record-01", "/example/1/"), "An example note"),
            references.Mention(EntityLink("example-record-02", "/example/2/")),
        ),
    ),
)

# The retry key every example form carries. The catalog draws no CSRF token,
# so an entry's form can act nowhere, and one fixed key makes each entry the
# same markup on every run, the image's gallery with it.
EXAMPLE_KEY = "web:example"


def drawn(example: Example, name: str) -> str:
    """The example under its name, as a page draws it under its head."""

    head = "" if example.heads_the_page or example.frame else head_of(name)
    return head + alone(example, name)


def alone(example: Example, name: str) -> str:
    """The part alone, as its template draws it."""

    with (
        override_settings(**example.settings),
        mock.patch.object(write_tags, "new_key", return_value=EXAMPLE_KEY),
        mock.patch.object(write_tags, "CsrfTokenNode", _NoToken),
        mock.patch.object(timezone, "now", return_value=DRAWN_AT),
        mock.patch.object(references, "referenced_by", return_value=EXAMPLE_MENTIONS),
    ):
        given = example.given()
        # A form names its fields' ids after the entry, as a page drawing
        # two forms gives each its own.
        for form in (value for value in given.values() if isinstance(value, BaseForm)):
            form.auto_id = f"{slugify(name)}-%s"
        context = _Asked(given, example.address) if example.address else Context(given)
        return Template(example.template).render(context)


def page(example: Example, name: str) -> str:
    """The example on a page of its own: in its main, or beside it for a part of the frame."""

    markup = drawn(example, name)
    if example.frame == "main":
        return part_page(name, "", main=markup)
    return part_page(name, head_of(name), **{example.frame: markup}) if example.frame else part_page(name, markup)


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
COLOR_SCHEME = "partials/_color_scheme.html"
FIRST_FRAME = "partials/_first_frame.html"
MAIN = "partials/_page_main.html"
SCRIPTS = "scripts/page.html"


def part_page(name, markup, before="", after="", main=""):
    """A page titled ``name``, of HQ's stylesheets and scripts, as base.html
    loads them, with ``markup`` alone in the content column base.html draws,
    or ``main`` drawn as that column, and the frame's parts it draws before
    and after it. A browser with script off has the page as the server drew
    it, one with script on the page a reader has: one document, both states."""

    main = main or render_to_string(MAIN, {"content": mark_safe(markup)})  # noqa: S308 - the catalog's own rendered markup
    # The nonce the gates answer a page a test put together with (gate.policy).
    nonce = {"csp_nonce": NONCE}
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{render_to_string(COLOR_SCHEME)}<title>{escape(name)}</title>{render_to_string(FIRST_FRAME, nonce)}</head>"
        f"<body>{before}{main}{after}{render_to_string(SCRIPTS, nonce)}</body></html>"
    )
