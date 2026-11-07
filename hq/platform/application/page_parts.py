"""The parts a page is built from, as data, for a page built in code.

Each part has two spellings that draw through one partial: a block tag in a
template (``part_tags``) and a dataclass here, which ``hq_sdk.ui`` exports so
an extension builds the same part. docs/Design.md ("A section is drawn by
{% section %}") says what each part carries.
"""

from collections.abc import Mapping
from dataclasses import field
from datetime import date
from typing import Any

from django.utils.html import conditional_escape, format_html, format_html_join, strip_tags
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import slugify
lazy from django.template.loader import render_to_string

lazy from hq.platform.core.templatetags.value_tags import breakable

from .display_words import display
from .labels import DOT_MARKUP

# The arrow that ends a link onward, as {% arrow %} draws it.
ARROW = mark_safe('&nbsp;<span aria-hidden="true">&rarr;</span>')

SECTION_PARTIAL = "partials/_part_section.html"
# Where a section's head may be told to stand; "" leaves it to surfaces.css.
HEADS = frozenset({"", "above", "inside"})
# A section in a section heads it one level down, to the last level HTML has.
TOP_LEVEL, LAST_LEVEL = 2, 6


@display
class SectionPart:
    """A titled part of a page in its frame: a heading, a count of what it
    holds, a line under the heading, a line beside it, its body and the line
    under the body that says how to read it; or, with no body, the line that
    says what is missing and the link that fills it, or nothing at all.

    The heading is plain words; a moment it names is ``moment``, drawn after
    them as a ``<time>`` in the ``when`` form, and with no words it is the
    heading. ``template`` and ``context`` draw the body. ``id`` is the
    section's own, for a link to it or a region refreshed in place; its
    heading's is derived. ``fold`` draws it closed with its heading as the
    toggle (``open`` starts it open). ``more_label`` and ``more_url`` are the
    aside's link to the whole of what the section shows part of, drawn only
    when there is somewhere to go, so a page passes its address as it has
    one. ``figure``
    draws it as a figure, its head the caption. ``head`` overrides where the stylesheet
    stands the head, ``"above"`` or ``"inside"`` its frame. ``data`` holds the
    ``data-`` attributes a script reads.
    """

    heading: str = ""
    count: int | str | None = None
    lede: str = ""
    aside: str = ""
    empty: str = ""
    id: str = ""
    fold: bool = False
    open: bool = False
    level: int = TOP_LEVEL
    css: str = ""
    template: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)
    remedy_label: str = ""
    remedy_url: str = ""
    moment: date | None = None
    when: str = ""
    head: str = ""
    foot: str = ""
    more_label: str = ""
    more_url: str = ""
    figure: bool = False

    def __post_init__(self) -> None:
        if self.head not in HEADS:
            raise ValueError("A section's head stands above or inside its frame, or where the stylesheet puts it.")
        if not strip_tags(self.heading).strip() and self.moment is None:
            raise ValueError("A section's heading says what it holds, in words or as the moment it names.")
        if bool(self.remedy_label) != bool(self.remedy_url):
            raise ValueError("A section's remedy is a link: its label and its address.")
        if self.more_url and not self.more_label:
            raise ValueError("A section's link to the rest says where it goes: give it its label.")
        if self.more_url and self.fold:
            raise ValueError("A folded section's head is its toggle, which holds no link; put it in the body.")
        if self.figure and self.fold:
            raise ValueError("A figure is drawn open, with its caption as its head; it does not fold.")
        if self.when and self.moment is None:
            raise ValueError("A section's when is the form of the moment it names, and it names none.")
        if not TOP_LEVEL <= self.level <= LAST_LEVEL:
            raise ValueError(f"A section's heading is h{TOP_LEVEL} to h{LAST_LEVEL}.")

    @property
    def heading_id(self) -> str:
        named = slugify(strip_tags(self.heading)) or (self.moment.isoformat() if self.moment else "")
        return f"{self.id or named or 'section'}-heading"

    def drawn(
        self, *, body: str = "", aside: str = "", empty: str = "", foot: str = "", heading_id: str = ""
    ) -> dict[str, Any] | None:
        """What the partial draws: the frame, then the head, then the body
        and its foot or the empty line. None when there is neither, since
        nothing empty takes space (docs/Design.md)."""

        empty = empty or self.empty
        heading_id = heading_id or self.heading_id
        if not str(body).strip() and not empty:
            return None
        return {
            "section": self,
            "heading_id": heading_id,
            "body": body,
            # A line given in code is text unless it is marked safe, as a
            # template's own output is.
            "aside": self._aside(aside or conditional_escape(self.aside)),
            "empty": empty,
            "foot": foot or conditional_escape(self.foot),
            **self._landmark(heading_id),
        }

    def _aside(self, said: str) -> SafeString:
        """The aside, ended by the link to the rest when there is one."""

        if not self.more_url:
            return mark_safe(said)  # noqa: S308 - escaped above or the template's own output
        link = format_html(
            '<a class="subtle-link" href="{}">{}{}</a>', self.more_url, breakable(self.more_label), ARROW
        )
        # said is escaped above or the template's own output; the rest is HQ's own markup.
        return mark_safe(f"{said}{DOT_MARKUP if said else ''}{link}")  # noqa: S308 - see above

    def _landmark(self, heading_id: str) -> dict[str, SafeString]:
        """The section's opening and closing tags: the landmark its heading
        names, holding the head on the page and the frame of its body. A
        ``data`` value of True draws a bare attribute, and None, False or ""
        none, so a count of 0 is still drawn."""

        element = "details" if self.fold else "figure" if self.figure else "section"
        named = {
            "class": " ".join(
                filter(
                    None,
                    (
                        "section",
                        "section-fold" if self.fold else "",
                        self.css,
                    ),
                )
            ),
            "id": self.id,
            "aria-labelledby": heading_id,
            "open": self.fold and self.open,
            "data-head": self.head or None,
        }
        named |= {f"data-{name.replace('_', '-')}": value for name, value in self.data.items()}
        drawn = [
            (key, value) for key, value in named.items() if value is not None and value is not False and value != ""
        ]
        valued = format_html_join("", ' {}="{}"', ((key, value) for key, value in drawn if value is not True))
        bare = format_html_join("", " {}", ((key,) for key, value in drawn if value is True))
        # The element is one of three tag names, never text; the attributes are escaped above.
        return {"opening": format_html("<{}{}{}>", element, valued, bare), "closing": format_html("</{}>", element)}

    def __html__(self) -> SafeString:
        # Rendered by a template, so escaped already.
        body = mark_safe(render_to_string(self.template, dict(self.context)).strip()) if self.template else ""  # noqa: S308 - see above
        values = self.drawn(body=body)
        return SafeString(render_to_string(SECTION_PARTIAL, values).strip() if values else "")
