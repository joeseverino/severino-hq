"""Every CSS selector the browser checks query, and the one way a check hands
one to the page (``css``).

A selector that matches nothing makes a check wait for an element that never
comes, or measure an empty list and pass. So a check asks for each selector
through ``css``, which counts it in the page and raises where it matches
nothing and the page is expected to hold it, naming the fix. The page is
expected to hold it wherever a check asks for it, but where its declaration
is ``optional``: one a check reads on pages that may draw none of it (every
page a sweep reads, or a state a test waits for) is held only on its part's
catalog entry, which draws it.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache
from typing import Any, override
from urllib.parse import urlsplit

from hq.platform.core.template_parts import Part
lazy from hq.platform.core.template_contract import parts

_HERE = "hq/platform/core/tests/browser/selectors.py"


@dataclass(frozen=True)
class Selector:
    """A CSS selector a check queries: the shared part whose markup it names
    (its tag, or the partial a page includes; none for HTML's own elements
    and what a page writes itself), and whether a page may hold none of it.
    It reaches the page only through ``css``, so it never formats as text.
    A call site narrows it (``"main " + selector``, ``selector + ":hover"``)
    and fills its ``{id}`` (``format``); what is held is the declaration."""

    declared: str
    part: str = ""
    optional: bool = False
    key: str = ""
    before: str = ""
    after: str = ""

    @property
    def query(self) -> str:
        return self.before + self.declared + self.after

    def format(self, **values: str) -> Selector:
        return replace(self, declared=self.declared.format(**values))

    def __add__(self, more: str) -> Selector:
        return replace(self, after=self.after + more)

    def __radd__(self, more: str) -> Selector:
        return replace(self, before=more + self.before)

    @override
    def __str__(self) -> str:
        raise TypeError(f"SELECTORS[{self.key!r}] reaches the page through css(), never as text")

    @override
    def __format__(self, spec: str) -> str:
        return str(self)


@cache
def _parts() -> dict[str, Part]:
    return {part.name or part.partial: part for part in parts()}


class Unmatched(AssertionError):
    """A selector a check asked for matches nothing on a page expected to hold it."""


def _unmatched(selector: Selector, where: str) -> str:
    named = f"SELECTORS[{selector.key!r}], {selector.declared!r},"
    if not selector.part:
        return (
            f"{named} matches nothing on {where}, which a check asks it of. Point it at what that page draws now "
            f"in {_HERE}, or declare it optional=True there if a page may draw none of it."
        )
    part = _parts()[selector.part]
    drawn = f"{{% {part.name} %}}" if part.name else part.partial
    files = ", ".join(f"templates/{name}" for name in part.files())
    return (
        f"{named} matches nothing on {where}, which a check asks it of. It names what {drawn} draws"
        f"{f' ({files})' if files else ''}: point it at what the part draws now in {_HERE}"
        + ("." if selector.optional else ", or declare it optional=True there if a page may draw none of it.")
    )


# Each selector's count in the page, in one trip.
_COUNTED = "(queries) => queries.map((query) => document.querySelectorAll(query).length)"


def css(page, value: Any, *, drawn: Callable[[], Part | None] = lambda: None, seen: set[str] | None = None) -> Any:
    """``value`` as the page reads it: each ``Selector`` in it (alone, or in
    a list, tuple or mapping) its CSS, once the page holds each one it is
    expected to (``Unmatched`` names the rest). ``drawn`` says the part the
    page is the catalog entry of; ``seen`` keeps the selectors already held,
    so each is counted once, and a state a page later leaves (a prompt
    answered) is the test's to judge."""

    asked: list[Selector] = []

    def plain(given):
        if isinstance(given, Selector):
            asked.append(given)
            return given.query
        if isinstance(given, list | tuple):
            return [plain(each) for each in given]
        if isinstance(given, dict):
            return {key: plain(each) for key, each in given.items()}
        return given

    read = plain(value)
    if not asked:
        return read
    path = urlsplit(page.url).path
    seen = set() if seen is None else seen
    entry = drawn() if any(selector.optional and selector.part for selector in asked) else None
    held = [
        selector
        for selector in {selector.declared: selector for selector in asked}.values()
        if selector.declared not in seen
        and (not selector.optional or (entry is not None and selector.part == (entry.name or entry.partial)))
    ]
    if held:
        counts = page.evaluate(_COUNTED, [selector.declared for selector in held])
        if unmatched := [selector for selector, count in zip(held, counts, strict=True) if not count]:
            raise Unmatched("\n".join(_unmatched(selector, path) for selector in unmatched))
        seen.update(selector.declared for selector in held)
    return read


def written(selector: Selector) -> str:
    """``selector``'s CSS written into a probe's source, for one no page is
    held to: optional, of no part."""

    if not selector.optional or selector.part:
        raise TypeError(f"SELECTORS[{selector.key!r}] is held on a page, so it reaches one through css()")
    return selector.query


def _named(declared: dict[str, Selector]) -> dict[str, Selector]:
    if unknown := sorted({selector.part for selector in declared.values() if selector.part} - _parts().keys()):
        raise KeyError(f"No shared part is named {', '.join(unknown)}: a selector names a part by its tag or partial")
    return {key: replace(selector, key=key) for key, selector in declared.items()}


_S = Selector

# Every class, id and attribute a check queries, CSS only (no `xpath=`).
_DECLARED = {
    # A row's actions cell (partials/_row_actions.html) and any cell of a column of
    # controls, whose controls sit on the row's first line.
    "row_actions_cell": _S(
        'main td.row-actions, main td[data-kind="control"]', "partials/_row_actions.html", optional=True
    ),
    # A device on the Agents page waiting for the reader's approval.
    "device_prompt": _S("[data-prompt]"),
    # An agent's update streams: a full set warned of in its row's cell, and
    # the streams it holds open under the cap said in the row's detail.
    "agent_streams": _S("tr[data-agent] > td [data-streams]"),
    "agent_streams_detail": _S("tr[data-agent] + tr.row-detail .row-below [data-streams]"),
    # The fragment primitive: a placeholder's failure line, which comes only
    # once a load fails, the disclosure holding one, and the calendar it
    # pages in place.
    "fragment_failure": _S("main > [data-fragment-load] > .notice", optional=True),
    "fragment_fold": _S("details:has(> [data-fragment-load]) > summary"),
    # A form's closing row (a long form's save bar is one), the section frame
    # it ends, and a control a row of the form chooses with.
    "save_bar": _S(".form-actions", "button_row"),
    # A form that asks one question (partials/_form.html), its one control
    # and its button.
    "one_question": _S(".form.is-question", "partials/_form.html"),
    "question_control": _S(".field > :is(input, select)", "django/forms/field.html"),
    "question_button": _S(".form-actions > button", "button_row"),
    "section_frame": _S(".section", "section"),
    "choice": _S("select", optional=True),
    "calendar_period": _S("#calendar-month > .section-head h2", "section"),
    "calendar_next": _S('#calendar a[rel="next"]'),
    "decisions": _S("[data-attention-item]", "partials/_work_queue.html"),
    # What a page says of the readings it asked for on opening.
    "ask_note": _S("[data-ask-note]", "partials/_ask.html"),
    # A page's note on its readings, at the end of the head's first line, and
    # the page it heads.
    "page_note": _S('.page-trail-row [data-ask-note][form="page-visit"]', optional=True),
    "page_trail_row": _S(".page-trail-row", "partials/_page_head.html", optional=True),
    "content": _S("#content", "page_main", optional=True),
    # A family's toggle, and the row toggle, which a list may draw none of.
    "decision_family": _S("[data-queue-family] [data-fold-toggle]", "partials/_work_queue.html"),
    "row_toggle": _S("[data-row-toggle]", "partials/_row_toggle.html", optional=True),
    # A fold of rows' toggle, and a row's own, as a reader presses them.
    "row_fold": _S("main [data-fold-toggle]", optional=True),
    "row_opener": _S("main [data-row-toggle]:not([data-fold-toggle])", "partials/_row_toggle.html", optional=True),
    # A table's own rows of records, the lines beneath them left out.
    "own_records": _S(":scope > tbody > tr:not(.row-detail)", optional=True),
    # Read inside one row of the queue, so it names nothing above the row's own cell.
    "decision_controls": _S("td.row-actions :is(a, button):not([hidden])", "partials/_row_actions.html"),
    "decision_title": _S("[data-attention-title]", "partials/_work_queue.html"),
    "decision_steps": _S(".step-list", "steps"),
    # What an item means, after its title.
    "decision_detail": _S(".row-gist", "partials/_work_queue.html"),
    # Running text, and what an opened row shows beneath it: code in it is
    # never cut, since only a cell's own identifier is. The rest of a
    # `line_list` is no text but the parts its line has no room for, each
    # an item of its own.
    "running_text": _S(":is(p, li:not(.line-rest), .row-detail)", optional=True),
    "code": _S("code", optional=True),
    # The box of a one-token name in a cell, and the parts it lays out on its
    # line.
    "name_box": _S("main td :is(.token[data-sole], .cell-line):not(code *):has(.whole)", "cell_line"),
    "part": _S(".whole"),
    "decision_actions": _S("[data-attention-item] > td.row-actions", "partials/_row_actions.html"),
    # The queue's table, and its cells of what is wrong and of its buttons.
    "queue_table": _S(".data-table:has(> tbody > tr[data-attention-item])", "partials/_work_queue.html"),
    "queue_what": _S("[data-attention-item] > .row-gutter", "partials/_work_queue.html"),
    "queue_act": _S("[data-attention-item] > td.row-actions", "partials/_row_actions.html"),
    # A list page's controls: its search, the button that opens a filter's
    # panel, the panel and a box in one, a toggle beside them, a heading that
    # sorts on the server, and the results they change: the rows, never the
    # line held beneath one, or the empty state that says nothing matches.
    # Never the copy a filter bar's sizer holds, which no one reaches.
    "table_search": _S("[data-table-toolbar] input[type=search]:not([inert] *)", "partials/_table_toolbar.html"),
    "table_filter": _S("[data-table-toolbar] .table-filter-menu > [popovertarget]", "partials/_table_toolbar.html"),
    "table_filter_panel": _S(
        "[data-table-toolbar] .table-filter-menu > [popover]:not([inert] *)", "partials/_table_toolbar.html"
    ),
    "table_filter_box": _S("input[type=checkbox][name]"),
    "table_toggle": _S("[data-table-toolbar] .table-toggle input:not([inert] *)", "partials/_table_toolbar.html"),
    "table_rows": _S("main .data-table > tbody > tr:not(.row-detail)", "data_table"),
    "table_empty": _S("main [data-empty-state]", "partials/_empty_state.html"),
    "sort_link": _S("main a.table-sort-link", "partials/_table_heading.html"),
    # The Filters control a filter bar folds its choices behind, where its
    # row has no room for them.
    "filter_fold": _S("form.filter-bar [data-filter-bar-fold]", "filter_bar", optional=True),
    # The queue's own filter: a choice of urgency, applied by its button.
    "queue_urgency": _S("form.filter-bar select[name=status]", "filter_bar"),
    "queue_apply": _S("form.filter-bar button[type=submit]:not([inert] *)", "filter_bar"),
    # The header's menus, a group's button and the links it opens, and on a
    # phone the drawer that holds them.
    "nav_menu": _S(".primary-nav [data-menu] > [popovertarget]", "partials/_site_header.html"),
    "nav_menu_panel": _S(".primary-nav [data-menu] > [popover]", "partials/_site_header.html"),
    "nav_drawer": _S(".primary-nav", "partials/_site_header.html"),
    "nav_toggle": _S("[popovertarget=primary-navigation]", "partials/_site_header.html"),
    # The dashboard's domain overviews, and the row its calendar leads.
    "highlights": _S(".section-flow > .section:has(> .section-body > .facts-band.is-lead)", "layout"),
    "patterns": _S(".section-flow:has(> #dashboard-calendar) > .section", "layout"),
    "glance": _S(".page-head [data-glance] > .facts-band > .fact", "fact"),
    # A control a reader fills in or picks from, whose edge is what shows it.
    "fillable": _S(
        "input:not([type=checkbox], [type=radio], [type=hidden], [type=range], [type=color], [type=submit],"
        " [type=button], [type=reset], [type=image]), select, textarea",
        optional=True,
    ),
    # A link that goes somewhere.
    "link": _S("a[href]", optional=True),
    # A control pressed or picked from rather than typed into: a button, a
    # disclosure's handle, a label drawn around a select or a checkbox.
    "pressed": _S(
        "button, .btn, summary, input:is([type=submit], [type=button], [type=reset]),"
        " label:has(select, input:is([type=checkbox], [type=radio]))",
        optional=True,
    ),
    # Everything pressed: a button, a link drawn as one, a disclosure's handle.
    "control": _S("button, .btn, summary", optional=True),
    # Everything a pointer presses: a link, a form's control, a disclosure,
    # and whatever else takes focus.
    "target": _S(
        "a[href], button, summary, select, textarea, input:not([type=hidden]), [tabindex]:not([tabindex='-1'])",
        optional=True,
    ),
    # Anything in a table cell: its words stay inside its own box, or they are
    # drawn across the next column while every box measures as in place.
    "cell_content": _S("main td *, main th *", optional=True),
    # A page's title.
    "page_title": _S(".page-head h1", "partials/_page_head.html"),
    # A head's line of facts under its title (`page_facts`).
    "head_facts": _S(".page-head .page-facts", "partials/_page_head.html"),
    # A head's controls, and what it says beside its title, its lede and its facts.
    "head_actions": _S(".page-head > .page-actions", optional=True),
    "head_said": _S(".page-head-copy > :not(.page-trail-row, .page-name), .page-name > :not(h1, .pill)", optional=True),
    # The box built to scroll a table sideways.
    "table_scroll": _S(".table-scroll", "data_table", optional=True),
    # The boxes allowed to scroll sideways: a table's, a block of code, whose
    # lines are as written, and a page's map of its sections, one row of
    # links at every width as a row of tabs is, so a section added never
    # makes the page taller before its content begins.
    "sideways": _S(".table-scroll, pre, .page-nav-list", optional=True),
    # A page's map of its sections: the row that scrolls, a link in it, and
    # the link of the section being read.
    "page_nav_list": _S("[data-page-navigation] .page-nav-list", "partials/_page_navigation.html"),
    "page_nav_link": _S("[data-page-nav-link]", "partials/_page_navigation.html"),
    "page_nav_current": _S('[data-page-nav-link][aria-current="location"]', "partials/_page_navigation.html"),
    # A table whose rows can be chosen, which the table script wires up last.
    "selectable_table": _S("[data-selectable-table]", "data_table"),
    # What a parser leaves where a <p> was closed early by a block inside it.
    "broken_paragraph": _S("main p:empty:not([hidden])", optional=True),
    # A share's bar, a <meter> behind its row's words (analytics).
    "share_bar": _S("main .bar-row > .meter", "partials/_share_list.html"),
    # What an open menu or a dialog draws over the page.
    "floating": _S("dialog, [popover]", optional=True),
    # Where the contrast check samples text: the page and the header over it.
    "contrast_scope": _S("main, .site-header", optional=True),
    # A disclosure in the page.
    "disclosure": _S("main details", optional=True),
    # Where the API reference mounts its viewer, on the pages that carry it.
    "reference": _S("#api-reference-root", optional=True),
    # Where a vendored script draws markup HQ does not write, which HQ's
    # stylesheet leaves to the vendor's own rules (tokens.css): the API
    # reference's viewer.
    "vendored": _S("[data-vendored]", optional=True),
    # A check of a check ladder: what it says, then what it was read from.
    "check_statement": _S(".ladder-statement", "check_rung", optional=True),
    "check_evidence": _S(".ladder-evidence", "check_rung", optional=True),
    # A value a disclosure's handle names.
    "summary_value": _S("main summary code", optional=True),
    # The topology map, its lanes and the nodes in them.
    "map": _S(".topology-map", "partials/_topology_map.html", optional=True),
    "map_lane": _S(".topology-lane", "partials/_topology_map.html", optional=True),
    "map_node": _S(".topology-node", "partials/_topology_map.html", optional=True),
    # A node's select link, the details beside the lanes, a trace's switches
    # and a choice in one.
    "map_select": _S("[data-topology-select]", "partials/_topology_map.html"),
    "map_detail": _S(".topology-detail", "partials/_topology_map.html"),
    "trace_controls": _S("#trace .switch", "switch"),
    "trace_chip": _S(".switch li > a", "switch"),
    # The command center: its field, its options, and the hint Enter's choice hides.
    "palette_input": _S("[data-command-center-input]", "partials/_command_center.html"),
    "palette_option": _S("[data-command-center-option]", "partials/_command_center.html"),
    "palette_hint": _S(".command-center-enter-hint", "partials/_command_center.html"),
    # The header's sections, an entry or a group's handle.
    "nav_entry": _S(".primary-nav > a, .nav-group > button", "partials/_site_header.html"),
    # A chip set in a line: an identifier, wherever it is not a block of code, or a tag.
    "chip": _S("main :is(code:not(pre *), .tag)", optional=True),
    # A page head's menu, and the box of what the menu holds.
    "head_menu": _S(".overflow-menu", "partials/_page_head.html"),
    "head_menu_panel": _S(".overflow-menu-panel", "partials/_page_head.html"),
    # A "do this now" control, whose words are its button's.
    "ask": _S(".ask", "partials/_ask.html", optional=True),
    # An ask's button.
    "ask_button": _S("[data-ask] [data-ask-button]", "partials/_ask.html"),
    # The button of an ask beside a page's title, which a menu holds a copy
    # of where the head has no room for it.
    "head_ask_button": _S(".page-head > .page-actions > .ask [data-ask-button]", optional=True),
    # What a page's head offers when it could not draw a reading in place,
    # which a page that draws its readings never shows.
    "head_offer": _S(".page-head .ask-said a", optional=True),
    # One standing in a table's row, rather than in the line a row opens.
    "row_ask": _S(".data-table > tbody > tr:not(.row-detail) > td [data-ask]", "partials/_ask.html"),
    # What an ask says on the line its row opens to.
    "ask_why": _S("[data-ask-why]", "partials/_ask.html"),
    # An identifier in a table's cell, and what in a row wraps by design,
    # where one may break (figures.css): the line a row opens, each part a
    # cell marks with its role (the row's own name, what a row means, free
    # text, a context line), a preview, a chip that wraps, a control, a
    # value among the words of a phrase, which wraps at its seams as they
    # do, a name outside a short value's box (`cell-short`), a value set after
    # words on its line, and a panel the cell shows,
    # which is laid out as a page.
    "cell_token": _S("main .data-table > tbody > tr > td .token", optional=True),
    # An identifier as `breakable` draws it, one run however its seams split it.
    "identifier": _S(".token", optional=True),
    # What in a table's cell breaks anywhere its column lacks room, as an
    # identifier does (cuts.css): a row's own name.
    "breaks_anywhere": _S('[data-cell~="name"]', optional=True),
    "wraps_in_a_cell": _S(
        "tr.row-detail, .row-gutter, [data-cell], code.cell-whole, .btn, .token:not(.cell-short *), [popover]",
        optional=True,
    ),
    # Words said to a reader and never drawn.
    "unseen": _S(".visually-hidden", optional=True),
    # The flow of sibling sections or cards, and of charts (layout.css).
    "flow": _S(".section-flow, .domain-charts", "layout", optional=True),
    # A band of readings beside a page's title.
    "head_band": _S(".page-head .facts-band", "facts", optional=True),
    # A list of facts, each label beside its value.
    "facts_list": _S(".facts-list", "facts", optional=True),
    # One fact of a band, and the label that names its value.
    "band_fact": _S(".facts-band > .fact", "fact"),
    "band_label": _S(".fact-label", "fact"),
    "fact_value": _S(".fact-value", "fact"),
    # What keeps its line wherever it is drawn: the count of planned days kept
    # beside a calendar's legend.
    "one_line": _S(".calendar-legend > .count", "partials/_activity_calendar.html"),
    # A tab of a tabbed chart, the radio inside it, and the panels they choose.
    "chart_tab": _S(".chart-tablist > .chart-tab", "partials/_chart_tabs.html"),
    "chart_panel": _S(".chart-tabs .chart-panel", "partials/_chart_tabs.html"),
    # A drawing: every chart, trend, bar and calendar a page draws, an icon
    # aside.
    # A month that ends its section's body, the box it fills (layout.css).
    "month": _S(".section-body > .month-grid:last-child", "partials/_month_grid.html", optional=True),
    "drawing": _S("main svg:not(.icon), table.activity-calendar", "partials/_chart.html", optional=True),
    # A dialog a page declares (partials/_modal.html), and the controls that
    # open the dialog `modal-{id}`, once `{id}` is filled in (the partial
    # states the contract): a link the page's wiring intercepts, a control
    # that says it opens a dialog and names it, the platform's own invoker,
    # and a status naming the popover panel it opens (an urgency's origin,
    # static/js/app.js).
    "dialog": _S('dialog.modal[id^="modal-"]', "partials/_modal.html", optional=True),
    "dialog_opener": _S(
        '[data-modal-open="{id}"], [aria-haspopup="dialog"][aria-controls="modal-{id}"],'
        ' [command="show-modal"][commandfor="modal-{id}"], [data-origin="modal-{id}"]',
        optional=True,
    ),
}

SELECTORS = _named(_DECLARED)
