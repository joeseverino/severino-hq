"""Every CSS selector the browser gates query (test_browser_selectors holds them to rendered markup)."""

# Every class, id and attribute the gate queries, held by
# test_browser_selectors to markup a template or tag library writes: CSS
# selectors only (no `xpath=`), and never an attribute only a script sets.
SELECTORS = {
    # The fragment primitive: a placeholder's failure line, the disclosure
    # holding one, and the calendar it pages in place.
    "fragment_failure": "main > [data-fragment-load] > .notice",
    "fragment_fold": "details:has(> [data-fragment-load]) > summary",
    "calendar_period": "#calendar-period",
    "calendar_next": '#calendar a[rel="next"]',
    "decisions": "[data-attention-item]",
    # What a page says of the readings it asked for on opening.
    "ask_note": "[data-ask-note]",
    # Every note a page says beside something: its live regions.
    "note": "[role=status], [aria-live]",
    # A family's toggle, the row toggle, and a row's own dismiss.
    "decision_family": "[data-queue-family] [data-fold-toggle]",
    "row_toggle": "[data-row-toggle]",
    "decision_dismiss": "[data-triage] button",
    "decision_title": ".attention-title",
    "decision_steps": ".resolution-workflow",
    "decision_detail": ".attention-detail",
    # What a card's own buttons decide (an approval's change): never cut.
    "decision_whole": ".attention-detail[data-whole]",
    "decision_actions": ".queue-act .attention-actions",
    # A list page's controls: its search, a filter's menu and a box in one, a
    # toggle beside them, a heading that sorts on the server, and the results
    # they change: the rows, never the line held beneath one, or the empty
    # state that says nothing matches.
    "table_search": "[data-table-toolbar] input[type=search]",
    "table_filter": "[data-table-toolbar] details[data-menu]",
    "table_filter_box": "input[type=checkbox][name]",
    "table_toggle": "[data-table-toolbar] .table-toggle input",
    "table_rows": "main .data-table > tbody > tr:not(.row-detail)",
    "table_empty": "main [data-empty-state]",
    "sort_link": "main a.table-sort-link",
    # The queue's own filter: a choice of urgency, applied by its button.
    "queue_urgency": "form.filter-bar select[name=status]",
    "queue_apply": "form.filter-bar button[type=submit]",
    # The header's menus, and on a phone the drawer that holds them.
    "nav_menu": ".primary-nav details[data-menu]",
    "nav_open": ".nav-toggle-open",
    "nav_close": ".nav-toggle-close",
    "highlights": ".dashboard-highlights > .highlight-card",
    # Each reading in the dashboard's row: a mark, then its lines.
    "readings": ".dash-readings .glance-panel-head",
    "patterns": ".dashboard-patterns > .card",
    # A control a reader fills in or picks from, whose edge is what shows it.
    "fillable": "input:not([type=checkbox], [type=radio], [type=hidden], [type=range], [type=color], [type=submit], [type=button], [type=reset], [type=image]), select, textarea",
    # A control pressed or picked from rather than typed into: a button, a
    # disclosure's handle, a label drawn around a select or a checkbox.
    "pressed": (
        "button, .btn, summary, input:is([type=submit], [type=button], [type=reset]),"
        " label:has(select, input:is([type=checkbox], [type=radio]))"
    ),
    # Everything pressed: a button, a link drawn as one, a disclosure's handle.
    "control": "button, .btn, .icon-link, summary",
    # Everything a pointer presses: a link, a form's control, a disclosure,
    # and whatever else takes focus.
    "target": "a[href], button, summary, select, textarea, input:not([type=hidden]), [tabindex]:not([tabindex='-1'])",
    # Anything in a table cell: its words stay inside its own box, or they are
    # drawn across the next column while every box measures as in place.
    "cell_content": "main td *, main th *",
    # A page's title.
    "page_title": ".page-head h1",
    # What holds the content and so stays inside its gutter: a card, a block
    # of code.
    "gutter_bound": "pre, .card, .boxed-facts",
    # The box built to scroll a table sideways.
    "table_scroll": ".table-scroll",
    # The boxes allowed to scroll sideways: a table's, and a block of code,
    # whose lines are as written.
    "sideways": ".table-scroll, pre",
    # A table whose rows can be chosen, which the table script wires up last.
    "selectable_table": "[data-selectable-table]",
    # What a parser leaves where a <p> was closed early by a block inside it.
    "broken_paragraph": "main p:empty:not([hidden])",
    # What an open menu or a dialog draws over the page, its toggle aside.
    "floating": "dialog, [popover], [data-menu] > :not(summary)",
    # Where the contrast check samples text: the page and the header over it.
    "contrast_scope": "main, .site-header",
    # A disclosure in the page, rather than a menu's.
    "disclosure": "main details:not([data-menu])",
    # Where the API reference mounts its viewer.
    "reference": "#api-reference-root",
    # Where a vendored script draws markup HQ does not write: the API
    # reference's viewer, which ReferenceBrowserTests holds to its own rules.
    "vendored": "#api-reference-root",
    # A check in the connection panel: what it says, then what it was read from.
    "check_statement": ".conn-layer-label",
    "check_evidence": ".conn-layer-evidence",
    # A value a disclosure's handle names.
    "summary_value": "main summary code",
    # The topology map, its lanes and the nodes in them.
    "map": ".topology-map",
    "map_lane": ".topology-lane",
    "map_node": ".topology-node",
    # A value and the one it changes into, its arrow, and what the arrow points at.
    "change": "main .change",
    # A chip set in a line: an identifier, wherever it is not a block of code, or a tag.
    "chip": "main :is(code:not(pre *), .tag)",
    "change_arrow": ".change-arrow",
    "change_to": ".change-to",
    # A page head's controls, its menu, and the box of what the menu holds.
    "head_actions": ".page-head > .page-actions",
    "head_menu": ".overflow-menu",
    "head_menu_panel": ".overflow-menu-panel",
    # The steps of a page's trail, and a figure's value.
    "trail_steps": "main .page-trail a",
    "figure_value": ".kpi .value",
    # A "do this now" control, whose words are its button's.
    "ask": ".ask",
    # One standing in a table's row, rather than in the line a row opens.
    "row_ask": ".data-table > tbody > tr:not(.row-detail) > td [data-ask]",
    # What an ask says on the line its row opens to.
    "ask_why": "[data-ask-why]",
    # An identifier in a table's cell, and what in a row wraps by design,
    # where one may break (figures.css): the line a row opens, the row's own
    # name, free text, a preview, a context line, a chip that wraps, a
    # control, and a phrase's word whose hyphenated parts are each cut at the
    # cell's edge.
    "cell_token": "main .data-table > tbody > tr > td .token",
    "wraps_in_a_cell": (
        "tr.row-detail, .row-gutter, td > strong:first-child, td > a:first-child > strong,"
        " .cell-data, .muted, .attention-title, .attention-detail, .table-clamp, code.cell-whole, .btn,"
        " .token:not([data-sole]):has(> .whole)"
    ),
    # Words said to a reader and never drawn.
    "unseen": ".visually-hidden",
    # A dialog a page declares (partials/_modal.html), and the controls that
    # open one: a link the page's wiring intercepts, or a control that says
    # it opens a dialog and names it.
    # A fact whose value is a list of names (partials' .code-list, .address-list).
    "fact_list": "main .kv > dd:has(> :is(.code-list, .address-list) > :nth-child(2))",
    # What keeps its line wherever it is drawn: the count of planned days kept
    # beside a calendar's legend.
    "one_line": ".calendar-legend > .count",
    # One fact of a band, and the label that names its value.
    "band_fact": ".fact-band > *",
    "band_label": ".band-label",
    "dialog": 'dialog.modal[id^="modal-"]',
    "dialog_opener": '[data-modal-open], [aria-haspopup="dialog"][aria-controls]',
}
