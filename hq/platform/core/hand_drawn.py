"""The shapes a shared part draws, which no other template writes by hand.

Each ``Shape`` names what it finds in a template's source, what a page writes
instead, and the templates that draw it themselves with why. HQ's template
loaders (``template_engine``) refuse a template of HQ or an extension that
draws one anywhere else as it loads, and the extension check reads every
template of an extension through ``hand_drawn``. Each shape carries the
sources it refuses and keeps, which one test walks.
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from django.template.base import Lexer, TokenType

from .template_source import OPEN_COMMENT, Find, Source, blank, matched, tagged, within

_DOTS, _ARROWS = "·•", "→←↗↑↓›‹»«"


def _typed_marks(source: Source) -> Iterator[int]:
    """Each dot, and each arrow a reader hears, typed as text."""

    def visit(around: list[tuple[str, dict]], data: str) -> Iterator[int]:
        tags = {tag for tag, _ in around}
        if tags & {"title", "script", "style"}:
            return iter(())
        # A name given to a control replaces its text; a landmark's does not.
        unheard = "kbd" in tags or any(
            named.get("aria-hidden") == "true" or (tag in {"a", "button"} and named.get("aria-label"))
            for tag, named in around
        )
        return (at for at, mark in enumerate(data) if mark in _DOTS or (mark in _ARROWS and not unheard))

    return source.walked(visit)


# What prints a figure: a filter that writes one, a count's or a total's name.
_FIGURE_FILTERS = frozenset(("money", "money_whole", "counted", "length", "floatformat", "bytes"))
_COUNT_NAME = re.compile(r"(?:^|[._])(?:count|total)$")


def _figures_in_chips(source: Source) -> Iterator[int]:
    """Each figure printed inside a chip, which is a word."""

    values = source.marked[1]

    def figure(value: str, percent: str) -> bool:
        name, *filters = (part.strip().partition(":")[0] for part in value.split("|"))
        return bool(percent) or bool(_FIGURE_FILTERS.intersection(filters)) or bool(_COUNT_NAME.search(name))

    def visit(around: list[tuple[str, dict]], data: str) -> Iterator[int]:
        chip = any(
            word in {"chip", "pill", "tag"} or word.endswith("-chip")
            for _, named in around
            for word in (named.get("class") or "").split()
        )
        printed = re.finditer(r"\0(\d+)\0(%?)", data) if chip else iter(())
        return (match.start() for match in printed if figure(values[int(match.group(1))], match.group(2)))

    return source.walked(visit)


# What a row holds back until it opens, and a tag that decides by a count.
_HELD_BACK = re.compile(r"""class=["'][^"']*\brow-more\b""")
_BY_A_COUNT = re.compile(r"""forloop\.counter|\|length\b|\|slice:["']-?\d*:?\d|\|add:["']-\d|[<>]=?\s*\d""")


def _counted_folds(source: Source) -> Iterator[int]:
    """Each part a row holds back inside a tag that decides by a count."""

    opened: list[bool] = []
    for token in Lexer(source.text).tokenize():
        if token.token_type is TokenType.BLOCK:
            name, counted = token.contents.partition(" ")[0], bool(_BY_A_COUNT.search(token.contents))
            if name in ("if", "for", "with"):
                opened.append(counted)
            elif name in ("elif", "else", "empty") and opened:
                opened[-1] = opened[-1] or counted
            elif name in ("endif", "endfor", "endwith") and opened:
                opened.pop()
        elif token.token_type is TokenType.TEXT and any(opened):
            for match in _HELD_BACK.finditer(token.contents):
                yield source.at(token.lineno + token.contents.count("\n", 0, match.start()))


_COUNT_IN_PARENTHESES = re.compile(r"\(\{\{[^}]*\|length\s*\}\}\)")


def _cut_counts(source: Source) -> Iterator[int]:
    """Each bare count in parentheses, apart from the words it counts."""

    text = re.sub(r"<option\b.*?</option>", lambda match: blank(match.group()), source.text, flags=re.DOTALL)
    return (match.start() for match in _COUNT_IN_PARENTHESES.finditer(text))


def _nested_forms(source: Source) -> Iterator[int]:
    """Each form opened while another is open."""

    depth = 0
    for match in re.finditer(r"<(/?)form\b", source.masked, re.IGNORECASE):
        if match.group(1):
            depth = max(depth - 1, 0)
            continue
        depth += 1
        if depth > 1:
            yield match.start()


# The value tags that write a figure together with its own words, each joined
# where a line could break inside it (value_tags, through line_breaks.held).
WORDED_FIGURES = {
    "bytes": "a byte count",
    "counted": "a count and the words for it",
    "expiry": "a date and a count of days",
    "measured": "a figure and its unit",
    "money": "an amount",
    "money_whole": "an amount in whole dollars",
    "seconds": "a run time",
    "signed_in": "an age in words",
    "span": "a length of time",
    "when_text": "a moment in words",
}

# A value is held as it is printed (held_values), a box as wide as its line at
# most; a mark written after it has no room there, so it starts the next line
# alone. The engine takes a mark written straight after a value into its hold,
# so a mark is stranded only past a closing tag or a branch's end, or after a
# tag's value, which the engine does not hold. A value whose last filter is
# UNBOXED is no box: a `<time>` (`|when`), or a figure in its words or a
# count, held as text, so the engine leaves its mark as text too.
UNBOXED = frozenset(("when", "length", *WORDED_FIGURES))
_VALUE = r"\{\{(?![^{}]*\|(?:" + "|".join(UNBOXED) + r")\b[^{}|]*\}\})[^{}]*\}\}"
_TAG_VALUE = r"\{%\s*(?:entity|cell_line)\b[^%]*%\}"
_SILENT = (
    r"(?:</[a-z]+>|\{%\s*(?:endif|endfor|endwith)\s*%\}|\{%\s*if\b[^%]*%\}"
    r"|\{%\s*if not forloop\.last\s*%\}[^{]*\{%\s*endif\s*%\})"
)
_STRANDED = re.compile(rf"(?:{_VALUE}{_SILENT}+|{_TAG_VALUE}{_SILENT}*)[.,;:!?)\]]")
# Text only a screen reader hears has no line for a mark to start.
_UNSEEN = re.compile(r'<span class="visually-hidden">.*?</span>', re.DOTALL)


def _stranded_marks(source: Source) -> Iterator[int]:
    """Each mark written past a value's closing tag or branch, or after a tag's value, out of its hold's reach."""

    seen = _UNSEEN.sub(lambda match: blank(match.group()), source.text)
    return (match.start() for match in _STRANDED.finditer(seen))


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


_CHOICE = r"[\s}]type=[\"']?(?:checkbox|radio)\b"
_CHIP_CLASS = r"[\s}]class=[\"'](?:[^\"']*[\s}])?(?:[\w-]*(?:chips?|badge|pill|tag)|[\w-]+-(?:count|age))(?:[\s\"'{]|$)"


def _form_controls(source: Source) -> Iterator[int]:
    """Each form, field and label outside a choice: a choice's box and its
    label are the filter-choice shape's."""

    choices = set(within("label", _CHOICE)(source))
    yield from tagged("form|textarea|fieldset")(source)
    yield from tagged("input", without=r"[\s}]type=[\"']?(?:checkbox|radio|search)\b")(source)
    yield from (at for at in tagged("label")(source) if at not in choices)


_FORMS_OF_THE_FRAME = "the frame's menu verbs, each one button, and the one form every post button names"
_SCRIPTED_FORM = "a hidden form the page's script submits, with no fields"
_FORMS = {
    "partials/_form.html": "the form part itself",
    "base.html": _FORMS_OF_THE_FRAME,
    "partials/_user_menu.html": _FORMS_OF_THE_FRAME,
    "core/_dashboard_glance.html": _SCRIPTED_FORM,
    "partials/_visit_refresh.html": _SCRIPTED_FORM,
}
_FILTER_BARS = {
    "partials/_filter_bar.html": "the filter bar part itself",
    "partials/_select.html": "the select part itself",
    "partials/_command_center.html": "the palette's combobox, which sends nothing",
    "partials/_topology_map.html": "the map's find, which sends nothing",
}
# The table part's own toolbar: each filter's menu of boxes, a toggle apiece
# under its own name, and the sort its label boxes.
_TABLE_TOOLBAR = "the table part's own filter bar, as partials/_filter_bar.html is the page's"
# The frame's bar, drawn once, whose composite controls no other control has.
_THE_BAR = {
    "partials/_site_header.html": "the frame's bar: each section group's label and caret, one box, and the queue's bell around its live count",
    "partials/_user_menu.html": "the frame's bar: the person's opener, a picture and a name that folds away",
}

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
        name="live-region",
        said="A live region's hooks written by hand, where the live region part draws them from a declared read set.",
        instead='<div{% live_region "estate" drawn at=url %}>, or a part\'s data_fragment_live= option',
        find=matched(r"data-fragment-live"),
        refuses=('<div data-fragment="" data-fragment-live="estate">',),
        keeps=('<div{% live_region "estate" drawn %}>', '{% card data_fragment_live="run" %}A{% endcard %}'),
    ),
    Shape(
        name="form",
        said="A form that writes drawn by hand, where the form part draws it.",
        instead='{% include "partials/_form.html" %}, or partials/_page_action.html of a PageAction.command',
        find=tagged("form", r"(?i)[\s}]method=[\"']?post\b"),
        refuses=('<FORM action="." method=post></FORM>',),
        keeps=('<form method="get"></form>',),
        drawn_by=_FORMS,
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
        drawn_by=_FILTER_BARS,
    ),
    Shape(
        name="button",
        said="A button drawn by hand, where the button tag draws every control that looks like one.",
        instead="{% button %}, which takes a popover, a dialog command and each data_* hook",
        find=tagged("button"),
        refuses=('<button type="button" popovertarget="menu">Menu</button>',),
        keeps=('{% button "Menu" popovertarget="menu" %}',),
        drawn_by={
            "partials/_table_heading.html": "the heading's sort, the twin of its server sort link, words and mark apart",
            **_THE_BAR,
        },
    ),
    Shape(
        name="chip",
        said="A chip, a badge, a count or an age drawn by hand, where the status part or a value filter draws it.",
        instead='{% status word chip=True %}, a count by {{ n|counted:"thing" }}, an age by {{ at|ago }}',
        find=tagged(r"[\w-]+", _CHIP_CLASS),
        refuses=('<span class="scope-chip">A</span>', '<span class="sweep-age">2h</span>'),
        keeps=('<span class="counter">2</span>', '{% status "Online" chip=True %}'),
        drawn_by={
            "partials/_list.html": "the list part, whose chips kind draws a link as a chip",
            "partials/_reading_age.html": "the reading age part itself",
            "partials/_cadence.html": "the cadence part's tally, a line of text",
            "partials/_record_list.html": "the record list lays out the chips the status part draws",
            "partials/_switch.html": "the switch part, each choice a chip of its row",
            "partials/_site_header.html": _THE_BAR["partials/_site_header.html"],
        },
    ),
    Shape(
        name="count",
        said="A count or a line beside a heading drawn by hand, where the section part draws its voice.",
        instead='{% section "Words" counts=rows %}, or the words alone in its {% aside %}',
        find=tagged(r"[\w-]+", r"[\s}]class=[\"'](?:[^\"']*[\s}])?count(?:[\s\"'{]|$)"),
        refuses=('{% aside %}<span class="count">{{ rows|length|counted:"row" }}</span>', '<b class="x count">2</b>'),
        keeps=('{% section "A" counts=rows %}{% aside %}{{ rows|length|counted:"row" }}{% endsection %}',),
        drawn_by={
            "partials/_section_head.html": "the section part itself, its heading's count",
            "partials/_row_group.html": "a declared group's count, after its name",
            "partials/_switch.html": "the switch part, each choice's count beside its chip",
            "partials/_activity_calendar.html": "the calendar part's tally of days kept, beside its legend",
        },
    ),
    Shape(
        name="filter-choice",
        said="A choice of several drawn as boxes by hand, where the toggle set part draws it.",
        instead='{% include "partials/_toggle_set.html" %}, or {% select %} for one choice',
        find=tagged("input", _CHOICE),
        refuses=('<label><input type="checkbox" name="kind" value="a"> A</label>',),
        keeps=('<input type="hidden" name="kind" value="a">',),
        drawn_by={
            "partials/_toggle_set.html": "the toggle set part itself",
            "partials/_row_select.html": "the table's box that selects its row",
            "partials/_table_heading.html": "the table's box that selects every row",
            "partials/_table_toolbar.html": _TABLE_TOOLBAR,
            "partials/_chart_tabs.html": "the chart tabs' own switch, which turns with no script",
        },
    ),
    Shape(
        name="form-control",
        said="A form, a field or a label drawn by hand, where the form, field and filter bar parts draw it.",
        instead='{% include "partials/_form.html" %} of a Django form, or {% filter_bar %} for what a list keeps',
        find=_form_controls,
        refuses=('<form method="get"><label>Kind <input name="kind"></label></form>', '<input type="hidden" name="a">'),
        keeps=('<label><input type="checkbox" name="a"> A</label>', '<input type="search" name="q">'),
        drawn_by={
            "django/forms/field.html": "the field part itself",
            "django/forms/widgets/choice_cards.html": "the field part's widget",
            "django/forms/widgets/multiple_input.html": "the field part's widget",
            "partials/_control_label.html": "the control label part itself",
            "partials/_toggle_set.html": "the toggle set part itself",
            "partials/_dropzone.html": "the dropzone part, a file field",
            "partials/_row_field.html": "the row field part, a field in a table's row",
            "partials/_table_toolbar.html": _TABLE_TOOLBAR,
            **_FORMS,
            **_FILTER_BARS,
        },
    ),
    Shape(
        name="list",
        said="A list or a band of facts drawn by hand, where the list or the facts part draws it.",
        instead='{% list kind="down|dots|prose|chips|codes|labeled" %}<li>...</li>{% endlist %}, or {% facts %}',
        find=lambda source: (
            start
            for find in (
                tagged("ul|ol|dl"),
                tagged(
                    r"[\w-]+",
                    r"[\s}]class=[\"'](?:[^\"']*\s)?(?:(?:stack|dot|prose|code|address)-list|link-chips"
                    r"|facts-(?:band|list|inline))(?:[\s\"']|$)",
                ),
            )
            for start in find(source)
        ),
        refuses=(
            "<ul><li>A</li></ul>",
            '<ul class="example-links"><li>A</li></ul>',
            '<span class="dot-list"><span>A</span></span>',
        ),
        keeps=('{% list kind="dots" %}<li>A</li>{% endlist %}', '<div class="stack">A</div>'),
        drawn_by={
            "partials/_list.html": "the list part itself",
            "partials/_facts.html": "the facts part itself",
            "django/forms/errors/list/ul.html": "Django's error list, which every form's and field's errors draw",
            "partials/_switch.html": "the switch styles the list it draws",
            "partials/_relation_list.html": "the relation list styles the list it draws",
            "partials/_messages.html": "the messages part, each message a notice",
            "partials/_record_list.html": "the record list part, each row a record",
            "partials/_steps.html": "the steps part, a list of its own in order",
            "partials/_timeline.html": "the timeline part, its events in order",
            "partials/_share_list.html": "the share list part, each row a bar",
            "partials/_check_ladder.html": "the check ladder part, its rungs in order",
            "partials/_check_rung.html": "the rung's statement, a dot run in its summary, where no list may stand",
            "partials/_cadence.html": "the cadence part, its periods in order",
            "partials/_week_plan.html": "the week plan part, its days in order",
            "partials/_dropzone.html": "the dropzone part, the files its script lists as they are chosen",
            "partials/_page_navigation.html": "the page's section navigation",
            "partials/_data_table.html": "a row's open line, its entries under their columns' names",
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
            r"|[\w.]+\|length)(?:\|\w+(?::[^\s|]+)?)*\s*\}\}</strong>\s*[a-z]"
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
        name="cell",
        said="A table's cell drawn by hand, where a declared table draws each cell and every mark it reads.",
        instead="{% data_table table %} of a DataTable its view declares",
        find=tagged("td|th"),
        refuses=("<table><tr><td>A</td></tr></table>", "<tr><th>Name</th></tr>"),
        keeps=("<table><caption>A</caption></table>", "<thead></thead>"),
        drawn_by={
            "partials/_data_table.html": "the declared table itself",
            "partials/_table_heading.html": "a declared table's heading",
            "partials/_row_select.html": "a declared row's tick-box",
            "partials/_row_group.html": "a declared group's heading row",
            "partials/_row_field.html": "a declared row's field",
            "partials/_row_actions.html": "a declared row's actions",
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
    Shape(
        name="typed-mark",
        said="A dot or an arrow typed as text, which a reader hears and a line may strand.",
        instead="{% dot %} between two parts, a .dot-list for a run of parts, {% arrow %} after a link",
        find=_typed_marks,
        refuses=("<p>{{ a }} &middot; {{ b }}</p>", '<a href="{{ url }}">All &rarr;</a>'),
        keeps=(
            '<span aria-hidden="true">→</span><a aria-label="Next" href="{{ url }}">&rsaquo;</a><kbd>↑</kbd>',
            '<span data-tip="{{ a }} · {{ b }}">{{ a }}</span><title>{{ a }} · HQ</title>',
        ),
    ),
    Shape(
        name="figure-in-chip",
        said="A figure inside a chip, which is a word.",
        instead="the figure in a band of facts, a numeric column or a sentence",
        find=_figures_in_chips,
        refuses=('<span class="{% if a %}pill{% endif %}">{{ alerts|length }}</span>', '<b class="tag">{{ n }}%</b>'),
        keeps=('<span class="pill">{{ state|breakable }}</span>', '<span class="pill-row">{{ n|length }}</span>'),
    ),
    Shape(
        name="counted-fold",
        said="A list folded at a count, which hides parts its line has room for.",
        instead="a Cell(fit=True) of the parts, which shows as many as the line holds",
        find=_counted_folds,
        refuses=(
            '{% for x in xs %}{% if forloop.counter > 3 %}<span class="row-more">{{ x }}</span>{% endif %}{% endfor %}',
        ),
        keeps=('{% if x.hidden %}<span class="row-more">{{ x }}</span>{% endif %}',),
    ),
    Shape(
        name="cut-count",
        said="A count cut off from the words it counts.",
        instead='{% section "Words" counts=rows %}, or {{ n|counted:"thing" }}',
        find=_cut_counts,
        refuses=("<summary>Other settings ({{ rows|length }})</summary>",),
        keeps=("<option>Machines ({{ rows|length }})</option>",),
    ),
    Shape(
        name="since",
        said='A "since" written before a moment, which reads "since 3 days ago" beside an age.',
        instead='{{ at|when:"since" }}, which says its own "since"',
        find=matched(r"\bsince\s+\{\{[^}]*\|when\b"),
        refuses=("Online since {{ at|when }}",),
        keeps=('Online {{ at|when:"since" }}',),
    ),
    Shape(
        name="plural",
        said="A word chosen by a count of one, which the count's own filter agrees.",
        instead='{{ n|counted:"one,many" }}, or {{ n|agreed:"one,many" }} for the words alone',
        find=matched(r"\{%-?\s*if\s[^%]*[!=]=\s*1\s*-?%\}[^{}]*\{%-?\s*else\s*-?%\}[^{}]*\{%-?\s*endif\s*-?%\}"),
        refuses=("Grant {% if names|length == 1 %}it{% else %}them{% endif %}",),
        keeps=(
            'Grant {{ names|length|agreed:"it,them" }}',
            "{% if rows|length == 1 %}{{ rows.0 }}{% else %}{% for row in rows %}{{ row }}{% endfor %}{% endif %}",
        ),
    ),
    Shape(
        name="code-block",
        said="A code block a keyboard cannot reach, though it scrolls in its own box.",
        instead='<pre tabindex="0">',
        find=tagged("pre", without=r"[\s}]tabindex=\"0\""),
        refuses=("<pre>{{ log }}</pre>",),
        keeps=('<pre tabindex="0">{{ log }}</pre>',),
    ),
    Shape(
        name="control-in-summary",
        said="A control inside a summary, which is the control that opens its disclosure.",
        instead="the link or button in the disclosure's body",
        find=within(
            "summary",
            r"<(?:a|button|input|select|textarea)\b|\btabindex=|\{%\s*(?:entity|button|cell_line|page_action)\b",
        ),
        refuses=('<summary>More <a href="{{ url }}">all</a></summary>',),
        keeps=("<summary>More</summary>",),
    ),
    Shape(
        name="nested-form",
        said="A form opened inside a form, whose opening tag the parser drops, so the inner form's end closes the outer one.",
        instead="one form, or the second after the first closes",
        find=_nested_forms,
        refuses=('<form method="post"><form method="post"></form></form>',),
        keeps=('<form method="post"></form><form method="post"></form>',),
    ),
    Shape(
        name="open-comment",
        said="A comment left open on its line, which Django prints on the page.",
        instead="{# #} closed on its line, or {% comment %} for a longer note",
        find=lambda source: (match.start() for match in OPEN_COMMENT.finditer(source.text)),
        refuses=("{# A note\nthat runs on #}",),
        keeps=("{# A note #}\n{% comment %}\nA longer note\n{% endcomment %}",),
    ),
    Shape(
        name="page-title",
        said="A page title that is empty, or names the site, which the layout appends after it.",
        instead="{% block title %} naming the page alone",
        find=matched(
            r"\{%\s*block title\s*%\}(?:\s*\{%\s*endblock|(?:(?!\{%\s*endblock).)*?(?:SITE_NAME|Severino HQ))",
            re.DOTALL,
        ),
        refuses=("{% block title %}{% endblock %}", "{% block title %}Machines · {{ SITE_NAME }}{% endblock %}"),
        keeps=("{% block title %}Machines{% endblock %}",),
        drawn_by={"base.html": "the layout, which appends the site name to each page's title"},
    ),
    Shape(
        name="dollar-amount",
        said='A dollar sign before a value, which writes a negative amount as "$-5.00".',
        instead="{{ amount|money }}",
        find=matched(r"\$\{\{"),
        refuses=("<td>${{ total|floatformat:2 }}</td>",),
        keeps=("<td>{{ total|money }}</td>",),
    ),
    Shape(
        name="stranded-mark",
        said="A mark written after a value's tag or branch, which has no room left on its line, so it starts the next alone.",
        instead="the mark straight after the value ({{ host }}.), or through its hold: |breakable:forloop for a "
        'list\'s commas, {% entity link mark="," %}; a run of code chips takes no commas',
        find=_stranded_marks,
        refuses=(
            "<p>At <code>{{ host }}</code>, which</p>",
            "<p>{% entity link %}.</p>",
            "<p>{% entity link %}{% if x %}, {{ y }}{% endif %}</p>",
            "<p>{% for h in hosts %}{{ h }}{% if not forloop.last %} {% endif %}{% endfor %}.</p>",
        ),
        keeps=(
            (
                "<p>Runs on {{ host }}. {% entity link mark=forloop %}"
                '{% with mark=x|then:"," %}{{ y|breakable:mark }}{% endwith %} {{ z }} or</p>'
            ),
            '<a><span class="visually-hidden">{{ x }}: {{ y }}</span></a>',
            "{% if x %}<code>{{ x }}.</code>{% else %}.{% endif %}",
            "<p>Read {{ at|when }}</time>.</p>",
        ),
    ),
    Shape(
        name="command-url",
        said="A command's address built by hand, which encodes its target as the form does not read it.",
        instead="the link command_url builds (action_links)",
        find=matched(r"""\{%\s*url\s+["']command["']"""),
        refuses=("""<a href="{% url 'command' name %}?target={{ key }}">Run</a>""",),
        keeps=('<a href="{{ action.url }}">Run</a>',),
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
