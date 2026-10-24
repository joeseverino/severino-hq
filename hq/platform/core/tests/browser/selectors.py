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
    # A family's toggle, the row toggle, and a row's own dismiss.
    "decision_family": "[data-queue-family] [data-fold-toggle]",
    "row_toggle": "[data-row-toggle]",
    # A row opened to show all it holds, with every cut in it lifted.
    "open_row": 'tr:has(> * > [data-row-toggle][aria-expanded="true"])',
    "decision_dismiss": "[data-triage] button",
    "decision_title": ".attention-title",
    "decision_steps": ".resolution-workflow",
    "decision_detail": ".attention-detail",
    "decision_actions": ".queue-act .attention-actions",
    # A list page's controls: its search, a filter's menu and a box in one, a
    # toggle beside them, a heading that sorts on the server, and the results
    # they change: the rows, or the empty state that says nothing matches.
    "table_search": "[data-table-toolbar] input[type=search]",
    "table_filter": "[data-table-toolbar] details[data-menu]",
    "table_filter_box": "input[type=checkbox][name]",
    "table_toggle": "[data-table-toolbar] .table-toggle input",
    "table_rows": "main .data-table > tbody > tr",
    "table_empty": "main [data-empty-state]",
    "sort_link": "main a.table-sort-link",
    # The queue's own filter: a choice of urgency, applied by its button.
    "queue_urgency": "form.filter-bar select[name=status]",
    "queue_apply": "form.filter-bar button[type=submit]",
    # The header's menus, and on a phone the drawer that holds them.
    "nav_menu": ".primary-nav details[data-menu]",
    "nav_open": ".nav-toggle-open",
    "highlights": ".dashboard-highlights > .highlight-card",
    # Each reading in the dashboard's row: a mark, then its lines.
    "readings": ".dash-readings .glance-panel-head",
    "patterns": ".dashboard-patterns > .card",
    # What reads as one unit and so never breaks across lines: a state, a
    # time, one part of a value told in several ("Up 1 week", "healthy"), a
    # control's label in a row's controls, a page's head or a list's filters.
    "one_line": (
        ".pill, td time, td .cell-part,"
        ' :is(td[data-kind="control"], .page-head, .filter-bar, .table-toolbar) :is(.btn, button)'
    ),
    # A control one line tall, drawn on its own rather than inside a label
    # that is the control a reader sees.
    "line_control": (
        "input:not([type=checkbox], [type=radio], [type=hidden], [type=file], [type=range], [type=color],"
        " [type=submit], [type=button]):not(label *), select:not(label *)"
    ),
    # A form's field: one label, its control and its help.
    "field": ".field",
    # The grid every form's fields are laid out in.
    "form": ".form",
    # A control a reader fills in or picks from, whose edge is what shows it.
    "fillable": "input:not([type=checkbox], [type=radio], [type=hidden], [type=range], [type=color], [type=submit], [type=button], [type=reset], [type=image]), select, textarea",
    # A row of controls that wraps when its screen is narrow.
    "wrapping_bar": ".filter-bar, .table-toolbar, .table-filters",
    # Everything pressed: a button, a link drawn as one, a disclosure's handle.
    "control": "button, .btn, .icon-link, summary",
    # Everything a pointer presses: a link, a form's control, a disclosure,
    # and whatever else takes focus.
    "target": "a[href], button, summary, select, textarea, input:not([type=hidden]), [tabindex]:not([tabindex='-1'])",
    # Anything in a table cell: its words stay inside its own box, or they are
    # drawn across the next column while every box measures as in place.
    "cell_content": "main td *, main th *",
    # What a control says about the work it asked for, drawn under it.
    "spoken": ".ask-said",
    # An identifier: written through `|breakable` (an entity's name is), or in
    # code. A code block is text laid out by its own rules, and a table lays
    # out its own identifiers.
    "token": ":is(code, kbd, samp, .token, [data-entity]):not(pre *, .data-table *)",
    # A name in a table cell that is not the row's own: cut, never broken
    # inside a hyphenated part. Code in a cell is cut to one line on its own,
    # and a label of several words wraps as prose.
    "cell_name": ".data-table td [data-entity]:not(:has(code), strong > *, .row-gutter > *):has(> .token[data-sole])",
    # A hyphenated part of a token, which a cell cuts and never breaks.
    "token_part": ".whole",
    # Where `|breakable` lets a token's line break (value_tags._SEAM).
    "seam": ".seam",
    # A part of a phrase: a box the cell cuts on its own (figures.css).
    "phrase_part": ".token:not([data-sole]) :is(.whole, .part)",
    # A box a cell cuts a value in, holding out for a few characters of it
    # (`--cut-floor`): a name, a line, an identifier's code, a service's name.
    "cut_box": ".data-table td :is([data-entity], .cell-line, code, .service-name strong)",
    # Prose, whose words wrap as a sentence's do: a paragraph, the blocks the
    # stylesheet holds to `--measure` (Design.md: "prose keeps `--measure`"),
    # and a cell's free text (`cell-data`). A code block lays out by its own
    # rules. Muted text is not prose: `.muted` is a tone, and the line it
    # dresses is as often a name ("on example-host") as a sentence.
    "prose": (
        "p, .page-lede, .empty, .attention-body, .queue-meaning, .resolution-workflow li,"
        " .cell-data, pre, script, style, template"
    ),
    # A row's own name, which wraps at its hyphens by design rather than being
    # cut (figures.css: "it wraps at its hyphens, and the names beside it give
    # way instead"). A hyphen is no seam `|breakable` can mark, so the token
    # rule leaves it to that design: bare, as the row's link, or alone in the
    # row's gutter (figures.css exempts `.row-gutter > *` from the cut), or a
    # queue row's title.
    "row_name": "td > strong:first-child, td > a:first-child > strong, td.row-gutter > [data-entity], td .attention-title",
    # Any token, in a table or not: none leaves a scrap of itself on a line.
    "any_token": ".token",
    # A cell name's own token, the box that cuts it. A name in a muted line
    # is context, cut at the cell's edge by design.
    "sole_token": ":scope > .token[data-sole]",
    "context_line": ".muted",
    # A figure in a band or card: its label, and the value it answers.
    "figure": ".kpi",
    "figure_label": ".label",
    "figure_value": ".value",
    # The second of a label and its value: a fact's value after its term, a
    # node key after its machine, a piece of evidence before its kind.
    "labeled": ":is(.kv, .conn-keys) > dt + dd, .conn-row > code + .conn-kind",
    # What a heading says beside its title: a count, an age, where it is read.
    "heading_aside": ".heading-aside",
    "external_link": ".external-link",
    # The same, in a heading or beside it in a section's head.
    "any_aside": ".heading-aside, .section-head .count",
    # A page's title, and the controls that share its head.
    "page_head": ".page-head",
    "page_title": ".page-head h1",
    "page_actions": ".page-actions",
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
    # A box that holds content: a card, a table's frame.
    "frame": ".card, .table-scroll",
    # What reaches across a frame besides its text.
    "frame_content": "img, svg, canvas, button, .btn, input, select, pre",
    # Frames by design: a control, a chip, a diagram's nodes.
    "frame_exempt": ".btn, .pill, .topology-node, label",
    # Everything inside a menu or a dialog, which float over the page.
    "frame_exempt_within": "dialog, [popover], [data-menu]",
    # What an open menu or a dialog draws over the page, its toggle aside.
    "floating": "dialog, [popover], [data-menu] > :not(summary)",
    # The lead control renders first, so it is the one a narrow head keeps.
    "head_lead": ".page-head .page-actions > :first-child",
    "head_title": ".page-head .page-title-row",
    # Where the contrast check samples text: the page and the header over it.
    "contrast_scope": "main, .site-header",
    # Filled boxes that are not tiles: controls, tables, code, charts, and
    # anything floating over the page.
    "tile_exempt": (".btn, .pill, button, input, select, textarea, table, pre, code, svg, canvas, dialog, [data-menu]"),
    # A control inside a tile is measured by its own box, not its text.
    "tile_control": ".btn, button",
    # A menu and the topology map lay out away from the box they sit in, so
    # what they hold is not measured against it.
    "tile_detached": "dialog, [data-menu], .topology-map",
    # A disclosure in the page, which the tile check opens to lay out.
    "disclosure": "main details:not([data-menu])",
    # Where the API reference mounts its viewer.
    "reference": "#api-reference-root",
    # Where a vendored script draws markup HQ does not write: the API
    # reference's viewer, which ReferenceBrowserTests holds to its own rules.
    "vendored": "#api-reference-root",
    # A band, and what in it is not a cell: a dialog a cell opens, its note.
    "band": (
        ":is(.control-summary, .service-band, .fact-band, .insight-grid, .sweep-grid, .machine-telemetry-metrics,"
        " .kpi-grid:not(.highlight-card > *))"
    ),
    "band_aside": "dialog, .band-meta",
    # A route's hops, and the map's lanes and the box that holds them.
    "route": ".request-path",
    # A dashboard queue card, and what can be pressed at its end.
    "queue_card": ".attention-row",
    "queue_card_end": ".attention-end",
    "route_icon": ".hop-icon",
    # Every route that draws a line through its stops, and the mark on the
    # line at each stop: the route strip and the connection panel's path.
    "route_line": ":is(.request-path, .conn-path > .conn-layers)",
    "route_mark": ":is(.hop-icon, .conn-tick)",
    # A check in the connection panel: what it says, then what it was read from.
    "check_statement": ".conn-layer-label",
    "check_evidence": ".conn-layer-evidence",
    # A value a disclosure's handle names.
    "summary_value": "main summary code",
    # Sections paired two to a row, at one height.
    "paired": ".section-flow",
    "map": ".topology-map",
    "map_lane": ".topology-lane",
    "map_node": ".topology-node",
    # A band's cells. Stats inside a card are the one band laid out with real
    # gaps instead of padded cells, so the KPI band is not listed.
    "band_cell": (
        ":is(.control-summary, .service-band, .fact-band, .insight-grid,"
        " .connection-control-grid, .sweep-grid,"
        " .machine-telemetry-metrics) > *"
    ),
}

_TOKEN_SELECTORS = [
    SELECTORS["token"],
    SELECTORS["cell_name"],
    SELECTORS["token_part"],
    f"{SELECTORS['prose']}, {SELECTORS['row_name']}",
    SELECTORS["open_row"],
]
