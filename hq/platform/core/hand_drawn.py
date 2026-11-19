"""The shapes a shared part draws, which no other template writes by hand.

Each ``Shape`` names what it finds in a template's source, what a page writes
instead, and the templates that draw it themselves with why. HQ's template
loaders (``template_engine``) refuse a template of HQ or an extension that
draws one anywhere else as it loads, and the extension check reads every
template of an extension through ``hand_drawn``. Each shape carries the
sources it refuses and keeps, which one test walks.
"""

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property

from django.template.base import tag_re

# A template comment, which no page receives, and a template tag or value.
_COMMENT_TAG = re.compile(r"\{%-?\s*(end)?comment\b.*?%\}", re.DOTALL)
_DJANGO = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
# An element's start tag, read where template tags are masked; an HTML
# comment or an end tag, which bound a run of text too.
_START = re.compile(r"<([a-zA-Z][\w-]*)\b[^<>]*>")
_BOUND = re.compile(r"<!--.*?-->|</?[a-zA-Z][^<>]*>", re.DOTALL)
_RAW = re.compile(r"<(script|style)\b[^<>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);")


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


class Source:
    """A template's source as its shapes read it: its comments blanked, so a
    position is still its line, and its start tags and runs of text found
    with every template tag and value masked."""

    def __init__(self, source: str) -> None:
        pieces, commented = [], False
        for index, piece in enumerate(tag_re.split(source)):
            if index % 2:
                if piece.startswith("{#"):
                    piece = _blank(piece)
                elif match := _COMMENT_TAG.fullmatch(piece):
                    commented, piece = not match.group(1), _blank(piece)
            pieces.append(_blank(piece) if commented else piece)
        self.text = "".join(pieces)
        self.masked = _DJANGO.sub(lambda match: re.sub(r"[^\n]", "\0", match.group()), self.text)

    def line(self, at: int) -> int:
        return self.text.count("\n", 0, at) + 1

    @cached_property
    def tags(self) -> list[tuple[str, int, int]]:
        """Each start tag: its element's name, lowercased, and its span."""

        return [(match.group(1).lower(), *match.span()) for match in _START.finditer(self.masked)]

    @cached_property
    def runs(self) -> list[list[tuple[int, str]]]:
        """Each run of text between two tags, outside a script or a style,
        as its pieces split at each entity: a piece's position and its text,
        stripped; an entity is a piece of its own."""

        masked = _RAW.sub(lambda match: _blank(match.group()), self.masked)
        bounds = [match.span() for match in _BOUND.finditer(masked)]
        found = []
        for start, end in zip(
            [0, *(end for _, end in bounds)], [*(start for start, _ in bounds), len(masked)], strict=True
        ):
            pieces, at = [], start
            for part in re.split(f"({_ENTITY.pattern})", self.text[start:end]):
                if stripped := part.strip():
                    pieces.append((at + part.index(stripped[0]), stripped))
                at += len(part)
            if pieces:
                found.append(pieces)
        return found


type Find = Callable[[Source], Iterator[int]]


def matched(pattern: str, flags: int = 0) -> Find:
    """Each match of ``pattern`` in the source."""

    compiled = re.compile(pattern, flags)
    return lambda source: (match.start() for match in compiled.finditer(source.text))


def tagged(name: str, attributes: str = "", *, without: str = "") -> Find:
    """Each start tag of an element whose name is ``name``, holding
    ``attributes`` and not ``without`` where given, each a pattern over the
    tag as written."""

    named, holding, lacking = re.compile(f"(?:{name})"), re.compile(attributes), re.compile(without)

    def find(source: Source) -> Iterator[int]:
        for element, start, end in source.tags:
            written = source.text[start:end]
            if named.fullmatch(element) and holding.search(written) and not (without and lacking.search(written)):
                yield start

    return find


def within(name: str, pattern: str) -> Find:
    """Each element ``name`` whose content up to its end tag holds ``pattern``."""

    held = re.compile(pattern)

    def find(source: Source) -> Iterator[int]:
        for element, start, end in source.tags:
            if element == name:
                closing = source.masked.find(f"</{name}", end)
                if held.search(source.text, end, len(source.text) if closing < 0 else closing):
                    yield start

    return find


def _outside_a_region(source: Source) -> Iterator[int]:
    regions = [(match.start(), match.group(1)) for match in re.finditer(r"\{%\s*(end)?table_region\b", source.text)]
    for element, start, _ in source.tags:
        if element == "table":
            before = [opened for at, opened in regions if at < start]
            if not before or before[-1]:
                yield start


# The value tags that write a figure together with its own words, each joined
# where a line could break inside it (value_tags, through line_breaks.held).
WORDED_FIGURES = {
    "bytes": "a byte count",
    "counted": "a count and the words for it",
    "expiry": "a date and a count of days",
    "measured": "a figure and its unit",
    "money": "an amount",
    "seconds": "a run time",
    "signed_in": "an age in words",
    "span": "a length of time",
    "when_text": "a moment in words",
}
# The filters whose value is words, so no unit follows it.
_WORDED = (*WORDED_FIGURES, "capfirst", "door_words", "interface_label", "kind_label", "lower", "readable")
_WORDED += ("title", "upper", "when", "yesno")

# What a figure is when a template writes it: a value, or a number.
_FIGURE = r"(?:\{\{\s*[\w.]+(?:\|\w+(?::(?:\"[^\"]*\"|'[^']*'|[\w.-]+))?)*\s*\}\}|(?:^|[^\w.,:$#/+-])\d+(?:[.,:]\d+)*)"
# A unit or a counted word after it.
_WORD = r"(?:(?:[a-z][A-Za-z]*|[A-Z]+s)(?:[^\w'-]|$)|%(?:[^}]|$))"
_FIGURE_THEN_WORD = re.compile(rf"{_FIGURE}\s+{_WORD}")
_ENDS_ON_FIGURE = re.compile(rf"{_FIGURE}$")
_STARTS_ON_WORD = re.compile(rf"^{_WORD}")
_NOT_A_FIGURE = re.compile(
    "|".join(
        (
            # A value that holds a name.
            r"\{\{\s*(?:\w+\.)*(?:get_username|host|kind|label|name|risk|status|title|username)\s*(?:\|[^}]*)?\}\}(?:\s+[A-Za-z%]|$)",
            # A value whose last filter writes its own words.
            rf"\|(?:{'|'.join(_WORDED)})(?::(?:\"[^\"]*\"|'[^']*'|[\w.-]+))?\s*\}}\}}(?:\s+[A-Za-z%]|$)",
            # A word that follows a name in a sentence.
            (
                r"(?:\}\}|\d)\s+(?:a|across|after|an|and|are|as|at|be|because|been|before|beyond|but|by|can|cannot|could|did"
                r"|do|does|for|from|had|has|have|he|her|his|if|in|into|is|it|its|itself|may|might|must|nor|not|now|of|on"
                r"|once|only|or|over|per|she|should|since|so|still|than|that|the|their|them|then|these|they|this|those"
                r"|through|to|under|until|was|we|were|when|where|which|while|who|will|with|within|without|would|yet|you)\b"
            ),
            # A number inside a template tag, which no reader sees.
            r"\d(?:[.,:]\d+)*\s+(?:[a-z][A-Za-z]*|[A-Z]+s)[^{}]*[%}]\}",
        )
    )
)


def _split_figures(source: Source) -> Iterator[int]:
    for run in source.runs:
        for index, (at, text) in enumerate(run):
            joined = run[index + 1 : index + 3]
            split = _FIGURE_THEN_WORD.search(text) or (
                _ENDS_ON_FIGURE.search(text)
                and len(joined) == 2
                and joined[0][1] in ("&nbsp;", "&#160;")
                and _STARTS_ON_WORD.match(joined[1][1])
            )
            if split and not _NOT_A_FIGURE.search(text):
                yield at


@dataclass(frozen=True, kw_only=True)
class Shape:
    """A shape a shared part draws, refused anywhere it is drawn by hand."""

    name: str
    said: str
    instead: str
    find: Find
    refuses: tuple[str, ...]
    keeps: tuple[str, ...] = ()
    # The templates that draw it themselves, each with why.
    drawn_by: Mapping[str, str] = field(default_factory=dict)


_FORMS_OF_THE_FRAME = "the frame's menu verbs, each one button, and the one form every post button names"
_SCRIPTED_FORM = "a hidden form the page's script submits, with no fields"

SHAPES = (
    Shape(
        name="fold",
        said="A fold drawn by hand, where the section part draws it.",
        instead='{% section "Heading" fold=True %}body{% endsection %}',
        find=tagged("details"),
        refuses=("<details open><summary>Notes</summary>B</details>",),
        keeps=('{% section "A" fold=True %}B{% endsection %}',),
        drawn_by={
            "partials/_check_rung.html": "the check ladder's rung, a part of its own",
            "partials/_connection_peering.html": "the carriage's lookup: its summary stays on the line",
        },
    ),
    Shape(
        name="heading",
        said="A heading drawn by hand, where the section part or the page frame draws it.",
        instead='the heading of {% section "Heading" %}, a sub-head a {% section %} inside it; the title is the frame\'s',
        find=tagged("h[1-6]"),
        refuses=("<h2>Example</h2>",),
        keeps=('{% section "Example" %}A{% endsection %}',),
        drawn_by={
            "auth/login.html": "the sign-in page stands outside the page frame, so it titles itself",
            "document_page.html": "the title of a page that is one document, spoken alone",
            "partials/_error_page.html": "an error page stands outside the page frame, so it titles itself",
            "partials/_modal.html": "a dialog's title, which labels the dialog",
            "partials/_page_head.html": "the page's title, the frame's own",
            "partials/_problems.html": "the urgent problems' notice, titled for a screen reader alone",
        },
    ),
    Shape(
        name="line-break",
        said="A line broken by hand with <br>, where the cell line or the stack list part draws the lines.",
        instead="{% cell_line %}first line{% detail %}second line{% endcell_line %}, or {% list %} for lines read down",
        find=tagged("br"),
        refuses=("A<BR/>B",),
        keeps=("{% cell_line %}A{% detail %}B{% endcell_line %}",),
    ),
    Shape(
        name="section",
        said="A <section> element drawn by hand, where the section part draws it.",
        instead='{% section "Heading" %}body{% endsection %}, or a <div> for one that only groups a layout',
        find=tagged("section"),
        refuses=("<section><h2>A</h2>B</section>",),
        keeps=('<div class="stack">A</div>',),
        drawn_by={"partials/_topology_map.html": "the map's details pane, a live region titled by the node it shows"},
    ),
    Shape(
        name="icon",
        said="An icon drawn by hand, where the icon part draws it.",
        instead='{% include "partials/_icon.html" with name="..." %}; a drawing that says something is role="img"',
        find=tagged("svg", r'[\s}]aria-hidden="true"'),
        refuses=('<svg aria-hidden="true"><use href="#x"></use></svg>',),
        keeps=('<svg role="img" aria-label="A"></svg>',),
        drawn_by={
            "partials/_icon.html": "the icon part itself",
            "partials/_chart.html": "a chart part's own drawing",
            "partials/_figure_drawing.html": "a figure's trend line, drawn by the figure part",
        },
    ),
    Shape(
        name="form",
        said="A form that writes drawn by hand, where the form part draws it.",
        instead='{% include "partials/_form.html" %}, or partials/_page_action.html of a PageAction.command',
        find=tagged("form", r"(?i)[\s}]method=[\"']?post\b"),
        refuses=('<FORM action="." method=post></FORM>',),
        keeps=('<form method="get"></form>',),
        drawn_by={
            "partials/_form.html": "the form part itself",
            "base.html": _FORMS_OF_THE_FRAME,
            "partials/_user_menu.html": _FORMS_OF_THE_FRAME,
            "core/_dashboard_glance.html": _SCRIPTED_FORM,
            "partials/_visit_refresh.html": _SCRIPTED_FORM,
        },
    ),
    Shape(
        name="filter-bar",
        said="A search field or select drawn by hand, where the filter bar and select parts draw it.",
        instead='{% filter_bar search="..." %}{% choices %}{% select ... %}{% endfilter_bar %}',
        find=lambda source: (
            start for find in (tagged("select"), tagged("input", r"[\s}]type=[\"']?search\b")) for start in find(source)
        ),
        refuses=("<select></select>", '<input type="search" name="q">'),
        keeps=('<input type="hidden" name="q">',),
        drawn_by={
            "partials/_filter_bar.html": "the filter bar part itself",
            "partials/_select.html": "the select part itself",
            "partials/_command_center.html": "the palette's combobox, which sends nothing",
            "partials/_topology_map.html": "the map's find, which sends nothing",
        },
    ),
    Shape(
        name="row-group",
        said="A group of rows headed by hand, where the row group tag heads it.",
        instead="{% row_group label count %}",
        find=tagged(r"[\w-]+", r"[\s}]scope=[\"']rowgroup[\"']"),
        refuses=('<th scope="rowgroup">A</th>',),
        keeps=('<th scope="row">A</th>',),
        drawn_by={"partials/_row_group.html": "the row group tag's own row"},
    ),
    Shape(
        name="list",
        said="A list drawn by hand, where the list part draws it.",
        instead='{% list kind="down|dots|prose|chips|codes|labeled" %}<li>...</li>{% endlist %}',
        find=lambda source: (
            start
            for find in (
                tagged("ul|ol", without=r"[\s}]class\b"),
                tagged(
                    "ul|ol",
                    r"[\s}]class=[\"'](?:[^\"']*\s)?(?:(?:stack|dot|prose|code|address)-list|link-chips)(?:[\s\"']|$)",
                ),
            )
            for start in find(source)
        ),
        refuses=("<ul><li>A</li></ul>", '<ol class="dot-list"><li>A</li></ol>'),
        keeps=('<ul class="nav-items"><li>A</li></ul>',),
        drawn_by={
            "partials/_list.html": "the list part itself",
            "partials/_switch.html": "the switch styles the list it draws",
            "partials/_relation_list.html": "the relation list styles the list it draws",
        },
    ),
    Shape(
        name="record-link",
        said="A link to a record's page drawn by hand, where the entity tag names the record by its model's page.",
        instead='{% entity row|record label=row.name %}, or row|record:"section" for a part of its page',
        find=tagged(
            "a",
            r"(?i)[\s}]href=[\"']?(?:[^\"'>]*\{%\s*url\s+[\"'][^\"']+[\"']\s+[^%\s]|\{\{\s*[\w.]+\.get_absolute_url\b)",
            without=r"(?i)[\s}]href=[\"']?[^\"'>]*\{%\s*url\s+[\"'][^\"']*(?:download|:file)[\"']",
        ),
        refuses=("<a href=\"{% url 'example_detail' row.pk %}\">A</a>", '<a href="{{ row.get_absolute_url }}">A</a>'),
        keeps=("<a href=\"{% url 'example:file' row.pk %}\">A</a>", "<a href=\"{% url 'example_list' %}\">A</a>"),
        drawn_by={
            "contacts/contact_list.html": "a contact message is a row of the site's store, with no model of HQ's"
        },
    ),
    Shape(
        name="row-actions",
        said="A row's actions drawn by hand, where the row actions part draws them.",
        instead="{% row_actions actions %} from hq_sdk.pages.PageAction, which draws the row's cell",
        find=within("td", r"\{%\s*button\b(?![^%]*\b(?:css=|look=\"(?:[^\"]*\s)?glyph[\s\"]))"),
        refuses=('<tr><td>{% button "A" offer=press look="glyphs" %}</td></tr>',),
        keeps=('<tr><td>{% button "Up" offer=press look="ghost glyph" %}</td></tr>',),
    ),
    Shape(
        name="run-state",
        said="How work stands is drawn by hand, where the ask or the status part draws it.",
        instead="an Ask (partials/_ask.html), or {% status run.word tone=run.tone data_ask_label=True %}",
        find=lambda source: (
            start
            for find in (
                matched(r"\b[Rr]efresh failed\b|\{% status \"(?:Running|Stopped early)\""),
                tagged(r"[\w-]+", r"[\s}]data-ask-label\b"),
            )
            for start in find(source)
        ),
        refuses=("<p>Refresh failed</p>", '<span data-ask-label="A">A</span>'),
        keeps=("{% status run.word tone=run.tone data_ask_label=True %}",),
    ),
    Shape(
        name="change-table",
        said="A before-and-after table drawn by hand, where the change table part draws it.",
        instead="{% change_table preview %} of an approval_review.ChangePreview",
        find=matched(r"\{%\s*table_region\b[^%]*\bcompare="),
        refuses=('{% table_region "A" compare=True %}{% endtable_region %}',),
        drawn_by={"partials/_change_table.html": "the change table part itself"},
    ),
    Shape(
        name="empty-said-quietly",
        said="An empty line drawn by hand, where the empty state part draws it.",
        instead='{% include "partials/_empty_state.html" with message="..." %}, or a {% section %}\'s {% empty %}',
        find=matched(r"\{%\s*muted\b[^%]*%\}\s*(?:No|Nothing|None|There (?:is|are) no)\b"),
        refuses=("{% muted %}No hosts{% endmuted %}",),
        keeps=("{% muted %}Nominal{% endmuted %}",),
    ),
    Shape(
        name="missing-value",
        said="A missing value's mark drawn by an if, where the value's own tag or filter draws it.",
        instead="{{ x|or_empty }}, a value filter alone for a figure or a moment, or {% present x %}",
        find=matched(
            r"\{%\s*else\s*%\}\s*\{%\s*empty_value\s*%\}\s*\{%\s*endif\s*%\}"
            r"|\{%\s*if\s[^%]*%\}\s*\{%\s*empty_value\s*%\}\s*\{%\s*(?:else|elif)\s"
        ),
        refuses=("{% if x %}{{ x }}{% else %}{% empty_value %}{% endif %}",),
        keeps=("{{ x|or_empty }}",),
        drawn_by={
            "control_plane/_service_rows.html": "a facet with nothing to show is No record where the DNS path has a gap",
            "control_plane/container_list.html": "the mark follows the image's standing, which no one value holds",
            "control_plane/tailnet.html": "a tag no device wears is said in words once the devices are read",
            "core/hq.html": "a kind never read counts 0 records, so whether it was read decides the mark",
        },
    ),
    Shape(
        name="query",
        said="A query added to an address by hand, where the with_query tag adds and encodes it.",
        instead="{% with_query url name=value as named_url %}, or {% with_query url name=value %} where it stands",
        find=matched(r"\|add:\s*[\"'][?&]|(?:\}\}|%\})(?:\?|&(?:amp;)?)[\w.-]+="),
        refuses=('<a href="{{ url }}?page=2">A</a>', '{{ url|add:"?page=2" }}'),
        keeps=('<a href="?all=1">A</a>',),
    ),
    Shape(
        name="split-count",
        said="A count split from its words, where the counted filter writes them as one.",
        instead='{{ n|counted:"thing" }}, the figure and its words as one, or {% fact "Label" value=n %}',
        find=matched(
            r"<strong>(?![^<]*\|counted\b)\{\{\s*(?:(?:[\w.]*[._])?(?:count|counts|total)\b[\w.]*|[\w.]*[a-z][^\Wsu]s"
            r"|[\w.]+\|length)(?:\|\w+(?::\S+)?)*\s*\}\}</strong>\s*[a-z]"
        ),
        refuses=("<p><strong>{{ rules|length }}</strong> grants</p>",),
        keeps=("<strong>{{ row.address }}</strong> answers",),
    ),
    Shape(
        name="split-figure",
        said="A figure apart from its word, where a value filter writes them as one.",
        instead='{{ n|counted:"thing" }} for a count, or {{ x|measured:"mi" }} for a unit, the figure and its word as one',
        find=_split_figures,
        refuses=("<p>{{ miles|floatformat:2 }} mi</p>", "<p>{{ record.ttl|breakable }}&nbsp;s</p>"),
        keeps=('<p>{{ miles|measured:"mi" }}</p>', "<p>{{ start|when }} to {{ end|when }}</p>"),
    ),
    Shape(
        name="stretched-shape",
        said="A round shape or text inside a stretched drawing, which distorts it.",
        instead="a dot as a zero-length line of class chart-dot, and a label in the unscaled svg.chart-labels layer",
        find=lambda source: (
            start
            for start in tagged("svg", r"[\s}]preserveAspectRatio=[\"']none[\"']")(source)
            if re.search(r"<(?:circle|ellipse|text)\b", source.masked[start : source.masked.find("</svg", start)])
        ),
        refuses=('<svg preserveAspectRatio="none"><circle r="1"></circle></svg>',),
        keeps=('<svg><circle r="1"></circle></svg>',),
    ),
    Shape(
        name="table",
        said="A table drawn outside its region.",
        instead='{% table_region "Label" %} around its <table class="data-table">',
        find=_outside_a_region,
        refuses=('{% table_region "A" %}<table></table>{% endtable_region %}<table></table>',),
        keeps=('{% table_region "A" %}<div><table></table></div>{% endtable_region %}',),
        drawn_by={
            "partials/_activity_calendar.html": "a month drawn as a grid, a chart part",
            "partials/_month_grid.html": "a month drawn as a grid of days, not a data table",
        },
    ),
    Shape(
        name="muted",
        said="Muted words drawn by hand, where the muted tag draws them.",
        instead='{% muted %}...{% endmuted %}, or {% muted element="p" css="..." %} for a paragraph',
        find=tagged(r"[\w-]+", r"[\s}]class=[\"'](?:[^\"']*[\s}])?muted(?:[^\w-]|$)"),
        refuses=('<span class="muted">A</span>',),
        keeps=('<span class="muted-ink">A</span>',),
        drawn_by={"partials/_list.html": "the list part says a quiet run in the muted voice"},
    ),
)


def hand_drawn(source: str, name: str) -> list[tuple[int, str]]:
    """Each shape the template ``name`` draws by hand, by line: what it is and
    what to write instead."""

    read = Source(source)
    return sorted(
        (read.line(at), f"{shape.said} Write {shape.instead}.")
        for shape in SHAPES
        if name not in shape.drawn_by
        for at in set(shape.find(read))
    )
