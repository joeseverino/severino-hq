"""Every CSS selector the browser gates query (test_browser_selectors holds them to rendered markup)."""

# Every class, id and attribute the gate queries, held by
# test_browser_selectors to markup a template or tag library writes: CSS
# selectors only (no `xpath=`), and never an attribute only a script sets.
SELECTORS = {
    # A row's actions cell ({% row_actions %}) and any cell of a column of
    # controls, whose controls sit on the row's first line.
    "row_actions_cell": 'main td.row-actions, main td[data-kind="control"]',
    # The queue's cell of what a row offers and its dismiss
    # (`partials/_queue_rows.html`), a table with no headings to name its kind.
    "queue_actions_cell": "main td.queue-act",
    # A device on the Agents page waiting for the reader's approval.
    "device_prompt": "[data-prompt]",
    # An agent's update streams: a full set warned of in its row's cell, and
    # the streams it holds open under the cap said in the row's detail.
    "agent_streams": "tr[data-agent] > td [data-streams]",
    "agent_streams_detail": "tr[data-agent] > th .row-below [data-streams]",
    # The fragment primitive: a placeholder's failure line, the disclosure
    # holding one, and the calendar it pages in place.
    "fragment_failure": "main > [data-fragment-load] > .notice",
    "fragment_fold": "details:has(> [data-fragment-load]) > summary",
    # A folded section, and a part of one drawn only once it opens.
    "section_fold": "details.section-fold",
    "drawn_when_opened": "[data-fragment-load]",
    # A kicker over a group, the one label drawn in capitals, and a quiet
    # link ("Change", "All projects").
    "kicker": ".eyebrow",
    # A form's closing row (a long form's save bar is one), the section frame
    # it ends, and a control a row of the form chooses with.
    "save_bar": ".form-actions",
    # A form that asks one question (partials/_form.html), its one control
    # and its button.
    "one_question": ".form.is-question",
    "question_control": ".field > :is(input, select)",
    "question_button": ".form-actions > button",
    "section_frame": ".section",
    "choice": "select",
    "quiet_link": ".subtle-link",
    "calendar_period": "#calendar-month > .section-head h2",
    "calendar_next": '#calendar a[rel="next"]',
    "decisions": "[data-attention-item]",
    # What a page says of the readings it asked for on opening.
    "ask_note": "[data-ask-note]",
    # A page's note on its readings, at the end of the head's first line, and
    # the page it heads.
    "page_note": '.page-trail-row > [data-ask-note][form="page-visit"]',
    "content": "#content",
    # A family's toggle, the row toggle, and a row's own dismiss.
    "decision_family": "[data-queue-family] [data-fold-toggle]",
    "row_toggle": "[data-row-toggle]",
    # A fold of rows' toggle, and a row's own, as a reader presses them.
    "row_fold": "main [data-fold-toggle]",
    "row_opener": "main [data-row-toggle]:not([data-fold-toggle])",
    # A table's own rows of records, the lines beneath them left out.
    "own_records": ":scope > tbody > tr:not(.row-detail)",
    "decision_dismiss": "[data-triage] button",
    "decision_title": ".attention-title",
    "decision_steps": ".resolution-workflow",
    # What an item means: never cut.
    "decision_detail": ".attention-detail",
    # What an item says, and where its buttons are: the words run to the buttons.
    "decision_words": "[data-attention-item] :is(.attention-head, .attention-detail)",
    "decision_buttons": ".queue-act",
    # Running text, and what an opened row shows beneath it: code in it is
    # never cut, since only a cell's own identifier is.
    "running_text": ":is(p, li, .row-detail)",
    "code": "code",
    # The boxes that cut a cell's identifier: the token, the box it draws
    # the name in (a chip in code), a part inside it.
    "cut_box": ":is(.token[data-sole], .token[data-sole] > .cut, .token[data-sole] .whole, code)",
    # The box a name in a cell is held by.
    "name_box": "[data-entity], code, .token[data-sole]",
    # The box a cell cuts a one-token name in, and the parts it lays out on
    # its line.
    "name_cut": "main td :is(.token[data-sole], .cell-line):not(code *) > .cut:has(.whole)",
    "part": ".whole",
    "decision_actions": ".queue-act .attention-actions",
    # The queue's table, and its cells of what is wrong and of its buttons.
    "queue_table": ".queue-table",
    "queue_what": "td.queue-what",
    "queue_act": "td.queue-act",
    # A list page's controls: its search, the button that opens a filter's
    # panel, the panel and a box in one, a toggle beside them, a heading that
    # sorts on the server, and the results they change: the rows, never the
    # line held beneath one, or the empty state that says nothing matches.
    "table_search": "[data-table-toolbar] input[type=search]",
    "table_filter": "[data-table-toolbar] [popovertarget]",
    "table_filter_panel": "[data-table-toolbar] [popover]",
    "table_filter_box": "input[type=checkbox][name]",
    "table_toggle": "[data-table-toolbar] .table-toggle input",
    "table_rows": "main .data-table > tbody > tr:not(.row-detail)",
    # The line beneath a row, with what the row holds back.
    "row_detail": "main .data-table > tbody > tr.row-detail",
    "table_empty": "main [data-empty-state]",
    "sort_link": "main a.table-sort-link",
    # The queue's own filter: a choice of urgency, applied by its button.
    "queue_urgency": "form.filter-bar select[name=status]",
    "queue_apply": "form.filter-bar button[type=submit]",
    # The header's menus, a group's button and the links it opens, and on a
    # phone the drawer that holds them.
    "nav_menu": ".primary-nav [data-menu] > [popovertarget]",
    "nav_menu_panel": ".primary-nav [data-menu] > [popover]",
    "nav_drawer": ".primary-nav",
    "nav_toggle": "[popovertarget=primary-navigation]",
    "highlights": ".dashboard-highlights > section",
    # Each reading in the dashboard's row: a mark, then its lines.
    "readings": ".dash-readings .glance-panel-head",
    "patterns": ".dashboard-patterns > .section",
    # A control a reader fills in or picks from, whose edge is what shows it.
    "fillable": "input:not([type=checkbox], [type=radio], [type=hidden], [type=range], [type=color], [type=submit], [type=button], [type=reset], [type=image]), select, textarea",
    # A link that goes somewhere.
    "link": "a[href]",
    # A status tag (`{% status %}`), which says a state beside a name.
    "tag": ".pill",
    # The mark a sentence puts after a name (`breakable`'s).
    "mark": ".mark",
    # A select; the probes that judge one that opens to its choices leave out a
    # list box (multiple or sized), which Django's widgets mark, not a template.
    "dropdown": "select, .table-filter-menu > button",
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
    "gutter_bound": "pre, .card",
    # The box built to scroll a table sideways.
    "table_scroll": ".table-scroll",
    # The boxes allowed to scroll sideways: a table's, and a block of code,
    # whose lines are as written.
    "sideways": ".table-scroll, pre",
    # A table whose rows can be chosen, which the table script wires up last.
    "selectable_table": "[data-selectable-table]",
    # What a parser leaves where a <p> was closed early by a block inside it.
    "broken_paragraph": "main p:empty:not([hidden])",
    # A share's bar, a <meter> behind its row's words (analytics).
    "share_bar": "main .bar-track",
    # What an open menu or a dialog draws over the page.
    "floating": "dialog, [popover]",
    # Where the contrast check samples text: the page and the header over it.
    "contrast_scope": "main, .site-header",
    # A disclosure in the page.
    "disclosure": "main details",
    # Where the API reference mounts its viewer.
    "reference": "#api-reference-root",
    # A block of code the reference's viewer draws: a request or answer.
    "reference_sample": "#api-reference-root pre",
    # Where a vendored script draws markup HQ does not write, which HQ's
    # stylesheet leaves to the vendor's own rules (tokens.css): the API
    # reference's viewer, which ReferenceBrowserTests holds to its own rules.
    "vendored": "[data-vendored]",
    # A check in the connection panel: what it says, then what it was read from.
    "check_statement": ".conn-layer-label",
    "check_evidence": ".conn-layer-evidence",
    # A value a disclosure's handle names.
    "summary_value": "main summary code",
    # The topology map, its lanes and the nodes in them.
    "map": ".topology-map",
    "map_lane": ".topology-lane",
    "map_node": ".topology-node",
    # A node's select link, the details beside the lanes, a trace's groups of
    # chips, and a chip.
    "map_select": "[data-topology-select]",
    "map_detail": ".topology-detail",
    "trace_controls": ".topology-trace-controls",
    "trace_chip": ".scope-chip",
    # The command center: its field, its options, and the hint Enter's choice hides.
    "palette_input": "[data-command-center-input]",
    "palette_option": "[data-command-center-option]",
    "palette_hint": ".command-center-enter-hint",
    # The header's sections, an entry or a group's handle.
    "nav_entry": ".primary-nav > a, .nav-group > button",
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
    # where one may break (figures.css): the line a row opens, each part a
    # cell marks with its role (the row's own name, what a row means, free
    # text, a context line), a preview, a chip that wraps, a control, a
    # phrase's word whose hyphenated parts are each cut at the cell's edge,
    # and a panel the cell shows, which is laid out as a page.
    "cell_token": "main .data-table > tbody > tr > td .token",
    # An identifier as `breakable` draws it, one run however its seams split it.
    "identifier": ".token",
    "wraps_in_a_cell": (
        "tr.row-detail, .row-gutter, [data-cell], .table-clamp, code.cell-whole, .btn,"
        " .token:not([data-sole]):has(> .whole), [popover]"
    ),
    # Words said to a reader and never drawn.
    "unseen": ".visually-hidden",
    # A dialog a page declares (partials/_modal.html), and the controls that
    # open one: a link the page's wiring intercepts, or a control that says
    # it opens a dialog and names it.
    # The flow of sibling sections or cards (layout.css).
    "flow": ".section-flow",
    # A section's heading (partials/_section_head.html).
    "section_head": ".section-head",
    # A section's body, the frame what it holds is drawn in (partials/_part_section.html).
    "section_body": ".section-body",
    # A fact whose value is a list of names (partials' .code-list, .address-list).
    "fact_list": "main .facts-list .fact-value:has(> :is(.code-list, .address-list) > :nth-child(2))",
    # A list of facts, label beside value (partials/_facts.html), and the
    # frames one is drawn in: a card or a section.
    "facts_list": ".facts-list",
    "list_frame": ".card, .section",
    # What keeps its line wherever it is drawn: the count of planned days kept
    # beside a calendar's legend.
    "one_line": ".calendar-legend > .count",
    # One fact of a band, and the label that names its value.
    "band_fact": ".facts-band > .fact",
    "band_label": ".fact-label",
    "fact_band": ".facts-band",
    "band_fact_cell": ".fact",
    "fact_value": ".fact-value",
    # A tab of a tabbed chart, the radio inside it, and the panels they choose.
    "chart_tab": ".chart-tablist > .chart-tab",
    "chart_tab_input": ".chart-tab-input",
    "chart_panel": ".chart-tabs .chart-panel",
    # A drawing: every chart, trend, bar and calendar a page draws, an icon
    # aside.
    "drawing": "main svg:not(.icon), table.activity-calendar",
    "dialog": 'dialog.modal[id^="modal-"]',
    # The controls that open the dialog `modal-{id}`, once `{id}` is filled
    # in (partials/_modal.html states the contract): a link the page's wiring
    # intercepts, a control that says it opens a dialog and names it, and the
    # platform's own invoker.
    "dialog_opener": (
        '[data-modal-open="{id}"], [aria-haspopup="dialog"][aria-controls="modal-{id}"],'
        ' [command="show-modal"][commandfor="modal-{id}"]'
    ),
}
