"""The parts a page is built from, as data, for a page built in code.

Each part has two spellings that draw through one partial: a block tag in a
template (``part_tags``) and a dataclass here, which ``hq_sdk.ui`` exports so
an extension builds the same part. ``section``'s rules say what each part
carries.
"""

from collections.abc import Mapping
from dataclasses import field
from datetime import date
from typing import Any, Literal, get_args

from django.utils.html import conditional_escape, strip_tags
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import slugify
lazy from django.template.loader import render_to_string

from hq.platform.core.held_values import format_html
lazy from hq.platform.core import live
lazy from hq.platform.core.templatetags.value_tags import breakable

from .display_words import display
from .tone import Tone

# The arrow that ends a link onward, written inside its last word's box as
# breakable writes a mark, so a wrapped label never leaves it alone on a line.
ARROW = "\N{NO-BREAK SPACE}\N{RIGHTWARDS ARROW}"


def html_attributes(named: Mapping[str, object]) -> SafeString:
    """A part's attributes as its opening tag writes them, each escaped: True
    drawn bare, and None, False or "" not at all, so a count of 0 is still
    drawn. A flag is read by its type, which ``isinstance`` sees through a
    lazy object for, so a context processor's lazy False draws nothing and
    never ``="False"``. Every part with ``data_*`` options draws them here."""

    if follows := named.get("data-fragment-live"):
        # A part drawn from a read set core.live declares is a region of its page.
        live.region(follows)
        named = {"data-fragment": True} | named
    flags = {key: True if value else None for key, value in named.items() if isinstance(value, bool | None)}
    drawn = [(key, value) for key, value in (named | flags).items() if value is not None and value != ""]
    valued = "".join(
        f' {conditional_escape(key)}="{conditional_escape(value)}"' for key, value in drawn if value is not True
    )
    bare = "".join(f" {conditional_escape(key)}" for key, value in drawn if value is True)
    return mark_safe(valued + bare)  # noqa: S308 - each name and value escaped


SECTION_PARTIAL = "partials/_part_section.html"
# Where a section's head may be told to stand; "" leaves it to surfaces.css.
Head = Literal["", "above", "inside"]
HEADS = frozenset(get_args(Head))
# A section in a section heads it one level down, to the last level HTML has.
TOP_LEVEL, LAST_LEVEL = 2, 6


@display
class SectionPart:
    """A titled part of a page in its frame: a heading, a count of what it
    holds, a line under the heading, a line beside it, its body and the line
    under the body that says how to read it; or, with no body, the line that
    says what is missing and the link that fills it, or nothing at all. Each
    field says what it does; ``{% section %}`` takes them as its options.
    """

    heading: str = field(default="", doc="Plain words; with none, the moment it names is the heading.")
    count: int | str | None = field(default=None, doc="How many things it holds, beside the heading.")
    lede: str = field(default="", doc="A line under the heading.")
    aside: str = field(default="", doc="A line, a link or a control beside the heading.")
    empty: str = field(default="", doc="With no body, what is missing.")
    id: str = field(default="", doc="Its own `id`, for a link to it or a region refreshed in place.")
    fold: bool = field(default=False, doc="Drawn closed, its heading the toggle.")
    open: bool = field(default=False, doc="A folded section drawn open.")
    line: bool = field(
        default=False, doc="A fold drawn as one line, its heading and aside the summary, its body under it, unframed."
    )
    level: int = TOP_LEVEL
    css: str = field(default="", doc="The classes of where it stands.")
    template: str = field(default="", doc="The template that draws the body, with `context`.")
    context: Mapping[str, Any] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict, doc="The `data-` attributes a script reads.")
    remedy_label: str = field(default="", doc="The label of the link the empty line offers.")
    remedy_url: str = field(default="", doc="Where the empty line's link leads.")
    moment: date | None = field(default=None, doc="A moment the heading names, drawn after its words.")
    when: str = field(default="", doc="The `when` form the moment is drawn in.")
    head: Head = field(default="", doc="Where its head stands; left empty, the stylesheet decides.")
    foot: str = field(default="", doc="Under the body, how to read it.")
    more_label: str = field(default="", doc="The label of the aside's link to the whole of what it shows part of.")
    more_url: str = field(default="", doc="Where that link leads, drawn only when there is somewhere to go.")
    figure: bool = field(default=False, doc="Drawn as a figure, its head the caption.")
    edge: Tone | Literal[""] = field(
        default="", doc="Its frame's starting edge in a status tone, while it stands out from those beside it."
    )

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
        if self.line and not self.fold:
            raise ValueError("A section drawn as one line is a fold, its line the toggle.")
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
        self, *, body: str = "", lead: str = "", aside: str = "", empty: str = "", foot: str = "", heading_id: str = ""
    ) -> dict[str, Any] | None:
        """What the partial draws: the frame, then the head with the body's
        lead, then the body and its foot or the empty line. None when there
        is neither, since nothing empty takes space
        (``partials/_empty_state.html``'s rules)."""

        if lead and (self.fold or self.figure):
            raise ValueError("A section's lead is drawn in its head, which a fold's toggle or a caption cannot hold.")
        empty = empty or self.empty
        heading_id = heading_id or self.heading_id
        if not str(body).strip() and not lead and not empty:
            return None
        return {
            "section": self,
            "heading_id": heading_id,
            "body": body,
            "lead": lead,
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
        # The label is words, also when a template's literal arrives marked
        # safe: as a plain str, breakable writes the arrow inside its last
        # word's box.
        label = breakable(str.__str__(strip_tags(self.more_label)), ARROW)
        link = format_html('<a class="subtle-link" href="{}">{}</a>', self.more_url, label)
        if not said:
            return link
        # The link moves to a line of its own whole, and no line ends with
        # the dot before it (`.end-run`, page.css). said is escaped above or
        # the template's own output; the rest is HQ's own markup.
        return mark_safe(f'<span class="end-run"><span>{said}</span>{link}</span>')  # noqa: S308 - see above

    def _landmark(self, heading_id: str) -> dict[str, SafeString]:
        """The section's opening and closing tags: the landmark its heading
        names, holding the head on the page and the frame of its body. A
        ``data`` value is drawn as ``html_attributes`` draws one."""

        element = "details" if self.fold else "figure" if self.figure else "section"
        named = {
            "class": " ".join(
                filter(
                    None,
                    (
                        "section-line" if self.line else "section",
                        "section-fold" if self.fold and not self.line else "",
                        f"section-edge tone-{self.edge}" if self.edge else "",
                        self.css,
                    ),
                )
            ),
            "id": self.id,
            "aria-labelledby": heading_id,
            # A flag, whatever a template passes for it (a form's errors).
            "open": bool(self.fold and self.open),
            "data-head": self.head or None,
        }
        named |= {f"data-{name.replace('_', '-')}": value for name, value in self.data.items()}
        # The element is one of three tag names, never text; the attributes are escaped there.
        return {
            "opening": format_html("<{}{}>", element, html_attributes(named)),
            "closing": format_html("</{}>", element),
        }

    def __html__(self) -> SafeString:
        # Rendered by a template, so escaped already.
        body = mark_safe(render_to_string(self.template, dict(self.context)).strip()) if self.template else ""  # noqa: S308 - see above
        values = self.drawn(body=body)
        return SafeString(render_to_string(SECTION_PARTIAL, values).strip() if values else "")
