"""The shared parts no tag draws, which a template includes by path, each
declared once as a ``Part``. A tag's part is declared where its tag is
registered; the template contract reads both (``template_contract.parts``).
A ``public`` one is an extension's to include too."""

from hq.platform.core.included_actions import APPROVAL_CARD, ASK, PAGE_ACTION, WRITE_GAP
from hq.platform.core.part_examples import charts, connection, forms, graph, pages, records
from hq.platform.core.template_parts import Part, included as _part

FIELD = Part(
    name="",
    partial="django/forms/field.html",
    about="A form field as every form draws it: its label, its control, its help and its errors.",
    classes=("check-row", "command-confirm", "field", "field-effect", "has-errors", "is-pair-start", "is-wide"),
    example=forms.FIELD,
    rules="""
    A field is the view's Django form field, drawn by this template, so its label, help, errors and
    required mark come with it; one posted with a button outside the form names that form in its
    widget's `form` attribute.
    """,
)
CHART = _part(
    "chart",
    "A measure over time from `hq_sdk.ui.line_chart`, or quantities that add up from "
    "`hq_sdk.ui.stacked_bar_chart`, or a spatial path from `hq_sdk.ui.PathChart`.",
    *("bar-chart", "chart-axis", "chart-band-*", "chart-category", "chart-cats", "chart-dot", "chart-frame"),
    *("chart-frame-dense", "chart-grid", "chart-hit", "chart-labels", "chart-legend", "chart-line"),
    *("chart-mark", "chart-mark-label", "chart-path", "chart-path-frame", "chart-path-marker", "chart-path-scale"),
    *("chart-path-segment", "chart-scroll", "chart-tick", "chart-trend", "line-chart", "series-*", "stacked"),
    pieces=("partials/_chart_legend.html", "partials/_chart_path.html"),
    public=True,
    rules="""
    Every drawing's legend is `partials/_chart_legend.html`, given the drawing, which says its keys
    (`legend_keys`, each a `drawings.LegendKey`): a chart's series, a path's ordered bands, a
    calendar's kinds and its missed mark, a whole's parts with each figure, share and link.

    Spatial drawings use the same chart frame as time series: `PathChart` supplies paths, markers, an
    optional scale and its named bands, which its legend keys. The drawing makes no outside requests.
    """,
    example=charts.CHART,
)
CADENCE = _part(
    "cadence",
    "Whether a thing happened, period by period, from `hq_sdk.ui.Cadence`.",
    *("cadence", "cadence-count", "cadence-head", "cadence-label", "cadence-mark", "cadence-note"),
    *("cadence-strip", "cadence-title", "cadence-week", "is-hit", "is-no-data"),
    public=True,
    example=charts.CADENCE,
)
MONTH_GRID = _part(
    "month_grid",
    "A calendar month as a grid of weeks, each day its events, a span across the days it covers.",
    *("is-*", "is-all-day", "is-compact", "is-end", "is-gap", "is-outside", "is-past", "is-selected", "is-start"),
    *("is-today", "is-weekend", "month-date", "month-day", "month-day-head", "month-grid", "month-item"),
    *("month-item-dot", "month-mark", "month-more", "month-of", "month-span", "month-week", "month-weekday"),
    draws=(CHART,),
    example=records.MONTH_GRID,
    states=records.MONTH_GRID_STATES,
)
WEEK_PLAN = _part(
    "week_plan",
    "Which days of the week a thing is scheduled for, from `hq_sdk.ui.WeekPlan`.",
    *("is-planned", "week-plan", "week-plan-day", "week-plan-label", "week-plan-mark", "week-plan-note"),
    "week-plan-notes",
    draws=(CHART,),
    public=True,
    example=charts.WEEK_PLAN,
)
PAGE_HEAD = _part(
    "page_head",
    "A page's head: its trail, its title, its facts and its actions, the overflow of them in a menu.",
    *("is-column", "overflow-menu", "overflow-menu-copies", "overflow-menu-panel", "page-actions", "page-facts"),
    *("page-head", "page-head-copy", "page-title-row", "page-trail", "page-trail-row"),
    rules="""
    **One head.** Trail, title, controls, lede, laid out the same at every width. The title and its
    controls share a row (the title wraps before the controls move); the lede runs the full width
    under both. Two controls stand beside the title at every width, the lead and the next the page
    names, and the rest go behind the overflow menu. A head too narrow for three (a container query on
    the head) folds the second in too. The lead is chosen by what each control is for, never by where
    it stands: not a destructive one while another is offered, then one that can be pressed, then the
    page's primary (`head_actions`). A destructive verb (a stop) is a link to its confirmation, which
    names what it stops.

    **A lede earns its line.** Most pages have none. A lede says what the page cannot show: a fact
    that changes how you read it ("Lookups run from outside this network"), or the record's own
    details. Never a table of contents of the page, a pointer to another page (link to it instead), or
    how HQ works inside (a file path, a background mechanism). Never "What X may run: ...", which says
    the heading again.

    A thing's state sits on its title's line (`page_aside`), as words. A page about a day (the
    dashboard) names it with `page_dateline`, drawn over the title where a trail stands. A head with
    an aside and a line of facts (`page_facts`) stacks its words in a column of their own: the trail
    or day, the title, the facts. The aside stands beside the whole column, as the dashboard's
    readings stand beside its greeting and what needs you. The facts about it at a glance are a band
    under the head, as a service, a machine and HQ's own page open.
    """,
    example=pages.PAGE_HEAD,
    states=pages.PAGE_HEAD_STATES,
)
MODAL = _part(
    "modal",
    "A dialog: its title, what it is for, the body a template names, and its close.",
    *("modal", "modal-*", "modal-close", "modal-foot", "modal-head", "modal-lede", "modal-panel"),
    pieces=("partials/_about.html",),
    example=pages.MODAL,
    rules="""
    **About text opens in a dialog.** Long reference text a page explains itself with (a server's
    instructions to its clients, a reading's method) sits behind `partials/_about.html`. That is an
    info button that opens it in the shared dialog through the platform's `command="show-modal"`
    invoker, with no script. It is never content the page's job depends on, which stays on the page.
    It goes in a section's body or a head's aside, never in a fold's summary: a button inside the
    summary is a control nested in the toggle, whose role a screen reader loses. Host-only, as the
    dialog it opens is. A panel a status opens in place (an urgency's origin) is this part with
    `popover=True`: it opens as a popover, and its close button hides it. The dialog is a form's
    width. `size="wide"` makes it a page in a dialog (the connection panel): two panels side by side,
    the window showing around it by a gap that grows with the window. It scrolls under a title that
    stays put, and takes the whole screen on a phone.
    """,
    states=pages.MODAL_STATES,
)
READING_AGE = _part(
    "reading_age",
    "When a reading was taken, `at` the moment, in the one wording every page uses; `unread` why there is none.",
    *("is-stale", "reading-age"),
    public=True,
    rules="""
    **A reading says how old it is.** Wherever a page shows what was read, its age is this part, over
    the reading's `Freshness` where the page has one. It says "Last read 3 hours ago", with "out of
    date" beside it past the line `application.freshness` draws for its kind.
    """,
    states=pages.READING_AGE_STATES,
)
WORK_QUEUE = _part(
    "work_queue",
    "What waits on the operator, each row what wants doing and why, or that nothing does.",
    *("attention-clear", "attention-links", "attention-reason", "is-mark", "row-line"),
    pieces=("partials/_queue_entries.html", "partials/_queue_rows.html", "partials/_urgency_pill.html"),
    public=True,
    rules="""
    **A queue row says what is wrong and holds what fixes it.** The queue is named "Needs you"
    wherever it appears. Every list of problems is drawn by it, so every row's urgency, words and
    buttons share one edge. The queue page, the findings page, the dashboard's short queue, a thing's
    problems, the checks a subject does not meet, the steps a request contradicts and a domain's own
    attention list all use one projection (`hq/platform/application/decisions.py`). They draw it as
    one table (its rows `partials/_queue_rows.html`). A page never draws a list of its own (the
    `hand-drawn-findings` rule). A row is a table row as any other: its words in the cell that names
    it, what it offers in `{% row_actions %}`, so it stands at the table's own density and gives way
    as every table does. It reads, in order:

    - its first line (`row-line`): the title and, only when it is urgent, the word "Urgent" after it
      in its tone (the words are `decisions.URGENCY`; a row that needs attention carries no mark, its
      word kept for a screen reader). The title states what is wrong and links to the thing it is
      about. It is what a reader presses, so it leads; a domain's heading, a family's name and their
      counts read below it;
    - one line of what it means;
    - quiet under it, since when, where its owner knows (`Insight(since=...)`), and never a guess, and
      how old the reading it is drawn from is (`Insight(observed_at=...)`). An out-of-date reading
      never asserts a live condition: its owner says what was read, in the past tense, as a notice.
      These are words under the title, not a column: most rows have neither;
    - buttons: the owner's actions, the first step that can be taken now when the owner offered none,
      and the check that reads again. At most one is primary. Where the reader sorts their own queue,
      Dismiss or Restore is the last of them (`row_actions put_away=`).

    Closed, a row is those one or two lines. Everything taller is the row's `row-below`, opened by its
    toggle into the line beneath, from the title's edge to the table's. A row toggles only where
    opening it shows something more, and with no script every row shows all of it. The line beneath
    holds, in order:

    - what it means in full;
    - what was read that shows it (`Insight(facts=...)`), each label once with its values beside it;
    - what to do, where a command or an instruction is a step, one standing alone and several
      numbered. A command is `{% command text=... %}`;
    - the reason it cannot be done from here, as a sentence and never as a step;
    - the pages that help look into it (`Insight(links=...)`), the map's as icons with HQ's
      tooltip;

    The survey holds "words swapped for a glyph".

    The word is `partials/_urgency_pill.html`. A Cmd- or Ctrl-click on it opens what raised it, read
    from the code rather than written down. That is the finding rule, its severity and the line its
    detector is defined at, or the domain and key of anything else.

    A next step an owner names as a short label with a `url` is a link to that page; one it writes as
    a sentence is shown as a sentence. Each action renders once, by method and URL, through the
    page-action renderer and the shared POST form, and the transport keeps the owner's full workflow.
    A dismissed row is its title and the way back. A link to a row (`/action-items/#…`) lands on it
    open.

    Several items of one family (`Insight(family=...)`) fold under a row saying how bad, what kind
    and how many, which opens to their rows: on the queue page (`partials/_queue_entries.html`) and
    wherever a thing's problems are drawn (`fold`). The findings page keeps each row its own, its
    filter being by rule.

    A queue of three or fewer is read on the dashboard, under the greeting, as the queue's own rows
    (`glance`), under no heading of their own. A longer one is a count and a link to its page, except
    the held requests, which are read there first at any length. There a row is its title, what it
    means, its age and its buttons, holding nothing beneath; its title opens the row on the page that
    shows the rest, steps included, or a held request's own page.

    **An item is open until its owner stops raising it.** Nobody closes one by looking at it. What a
    reader can do with one they are not acting on is dismiss it. It leaves their queue and their
    counts, stays reachable in a fold below while its owner still raises it, and comes back on its own
    when it gets worse, grows, or is raised again after clearing (its `since` after the dismissal).
    The header and the dashboard count what waits; a dashboard with only dismissed items says so and
    links to them, and never claims all is clear. A held request (`Insight(held=True)`) is decided by
    its Approve or Reject, never dismissed: its row has no Dismiss and no mark hides it
    (`action_items.sortable`). It is composed with no decision on it: each reader is offered only the
    decisions the rule that refuses them lets that reader take (`approvals.offers`), and a decision
    withheld says why among the row's facts.

    **A family folds.** An owner names what kind of matter an item is (`Insight(family="Image
    advisories")`), in the words it would head a list with. Several open items of one family from one
    domain are one line in the queue (how bad, what kind, how many), a row that opens to theirs, and
    can be dismissed whole. A family of one is the item alone.

    **Dismissing a row costs a write, not a queue.** A row's button names the row itself (its revision
    and key), so dismissing or restoring one composes nothing. With script it happens in place, the
    row becoming one quiet line with the way back and the counts over it following. Only "all of
    these" asks the server what "these" are.

    **A notice is not something to do.** An owner marks an item a notice (`Insight(notice=True)`) when
    it reports something that happened or was measured and there is nothing to resolve. The queue
    keeps notices in their own section, out of the "needs you" count, and a dismissed notice is done
    with: it returns only when what it reports changes. The line is drawn once
    (`action_items.split_waiting`) from what each item says of itself.

    The queue page is sections by the domain that raised each item, derived from what the items carry
    (`hq/platform/application/action_items.py`), so a domain that starts raising items has its section
    without being listed anywhere. Filters that match nothing offer a way back; they never imply a
    healthy estate.
    """,
    example=records.WORK_QUEUE,
)

TOPOLOGY_MAP = _part(
    "topology_map",
    "The map of how things connect, laid out and filtered by its own script, each node's edges stated from it.",
    *("topology-detail", "topology-kind-*", "topology-kind-mark", "topology-lane", "topology-map"),
    *("topology-node", "topology-node-*", "topology-node-degree", "topology-node-link", "topology-node-list"),
    *("topology-node-select", "topology-node-title", "topology-workspace", "relation-arrow", "relation-kind"),
    *("relation-link", "relation-list", "relation-other", "relation-reading", "relation-values", "relation-verb"),
    pieces=("partials/_relation_list.html",),
    rules="""
    **One map.** A graph is drawn by this part from what the map's view (`drawn`) hands it: the filter bar
    that narrows it, a lane of cards for each kind, and the one panel beside the lanes that holds the
    selected node, never a card. The edges a node stands at the end of are
    `partials/_relation_list.html`, grouped by direction under a kicker each, every row the link to
    the other end. A page's own list of what a record relates to, grouped by what each says, is
    `partials/_relationships.html` instead. The `graph/` examples draw both from one example graph.
    """,
    example=graph.TOPOLOGY_MAP,
)

PARTS = (
    FIELD,
    Part(
        name="",
        partial="django/forms/widgets/choice_cards.html",
        about="A choice of one, each option a card saying what it does.",
        classes=("choice-card", "choice-card-what", "choice-cards"),
        example=forms.CHOICE_CARDS,
        rules="""
        **A choice that has a consequence says it.** A field whose few answers each change something
        different declares `ChoiceCards(described=...)` (`hq/platform/application/forms.py`). Each answer
        is a card of its radio, with its few words as the radio's name and what choosing it does as its
        description. The whole card is pressable. It draws through the form's own loop, so no page writes
        its markup. *Catalog:* `form/choice-cards`.
        """,
    ),
    Part(
        name="",
        partial="django/forms/widgets/suffixed.html",
        about="A text input with its unit after it.",
        classes=("input-suffix",),
        example=forms.SUFFIXED,
        rules="""
        A number with a unit declares `SuffixedInput(suffix=...)`, the unit beside the box. It draws
        through the form's own loop, so no page writes its markup.
        """,
    ),
    CHART,
    CADENCE,
    MONTH_GRID,
    WEEK_PLAN,
    ASK,
    PAGE_ACTION,
    WRITE_GAP,
    APPROVAL_CARD,
    PAGE_HEAD,
    MODAL,
    READING_AGE,
    WORK_QUEUE,
    _part(
        "activity_calendar",
        "One month of plan against execution, from `hq_sdk.ui.ActivityCalendar`.",
        *("activity-calendar", "calendar-controls", "calendar-date", "calendar-day", "calendar-dot"),
        *("calendar-legend", "calendar-marks", "calendar-period", "calendar-weekday"),
        *("is-filler", "is-missed"),
        draws=(CHART, MONTH_GRID, WEEK_PLAN),
        public=True,
        example=charts.ACTIVITY_CALENDAR,
    ),
    _part(
        "allocation",
        "Parts of one whole as one bar, from `hq_sdk.ui.AllocationBar`.",
        public_classes=("allocation",),
        draws=(CHART,),
        public=True,
        example=records.ALLOCATION,
        states=records.ALLOCATION_STATES,
    ),
    _part(
        "cadence_matrix",
        "Several cadences over the same periods, from `hq_sdk.ui.CadenceMatrix`.",
        *("cadence-cell", "cadence-detail", "cadence-period", "cadence-total"),
        draws=(CADENCE,),
        public=True,
        example=charts.CADENCE_MATRIX,
    ),
    _part(
        "calendar_nav",
        "A calendar's way to the month before and after, and back to today.",
        "calendar-nav",
        "calendar-now",
    ),
    _part(
        "calendar_sources",
        "The calendar's sources, each a toggle with its color swatch.",
        *("calendar-source", "calendar-source-failure", "calendar-source-note", "calendar-sources"),
        draws=(CHART, MONTH_GRID),
        rules="""
        Each source is a `{% button %}` with `post` and `pressed`, in its series color.
        """,
        example=records.CALENDAR_SOURCES,
    ),
    _part(
        "certificate_mark",
        "A certificate's mark: whether it is in force or waits on its issue.",
        "cert-mark",
        "is-pending",
        example=records.CERTIFICATE_MARK,
    ),
    _part(
        "change",
        "One value changed: what it was, an arrow, what it is.",
        "change",
        "change-arrow",
        "change-to",
        rules="""
        **A change is one primitive.** A value and the one it changes into (a running version and the
        newer release, an installed package and the version that fixes it) is `{% include
        "partials/_change.html" with from=… to=… %}`, with `code=True` for identifiers and `url` to link
        the new value. Old value, arrow and new value share one baseline. A narrow line breaks only between
        the two values, with the arrow kept beside the new one, and a reader hears "1.2.3 to 1.3.0" rather
        than an arrow.
        """,
        states=records.CHANGE_STATES,
    ),
    _part(
        "chart_tabs",
        "Up to four charts of one thing in the space of one.",
        *("chart-panel", "chart-panel-*", "chart-tab", "chart-tab-input", "chart-tablist", "chart-tabs"),
        public=True,
        example=charts.CHART_TABS,
    ),
    _part(
        "command_center",
        "Search across HQ, its results grouped, each a place to go or a command to run.",
        *("command-center-arrow", "command-center-copy", "command-center-destination", "command-center-empty"),
        *("command-center-enter-hint", "command-center-evidence", "command-center-footer"),
        *("command-center-group-label", "command-center-loading", "command-center-more", "command-center-option"),
        *("command-center-options", "command-center-palette", "command-center-query", "command-center-results"),
        "command-center-title",
        states=pages.COMMAND_CENTER_STATES,
    ),
    _part(
        "connection_loading",
        "The connection dialog while its checks run.",
        *("conn-live", "conn-pending", "conn-pulse", "conn-samples"),
    ),
    _part(
        "connection_peering",
        "The connection page's peering of you, the link and HQ.",
        *("conn-carriage", "conn-carriage-*", "conn-headline", "conn-link", "conn-link-*", "conn-link-line"),
        *("conn-peer", "conn-peer-name", "conn-peering", "conn-peering-frame", "conn-row-note"),
        "conn-sub",
        rules="""
        **A connection is its two ends and the link between them.** It draws You, the link and HQ side by
        side, the link in its network's color and the verdict's. It stacks the three in one column when
        its own box is too narrow for them, wherever it is drawn. Its `#carriage` is a fact's value: which
        network the link rides over, with a lookup of the address a link across the internet comes from.
        The dialog's shell before the panel arrives (`partials/_connection_loading.html`) is the panel's
        own box.
        """,
        example=connection.CONNECTION_PEERING,
        states=connection.CONNECTION_PEERING_STATES,
    ),
    _part(
        "demo_about",
        "The About dialog's body on a saved copy of HQ: what it is, the pages to try, what built it.",
        draws=(MODAL,),
        example=pages.DEMO_ABOUT,
    ),
    _part(
        "demo_foot",
        "A saved copy's foot, which opens its About dialog.",
        "demo-about",
        "demo-about-dot",
        "demo-foot",
        example=pages.DEMO_FOOT,
    ),
    _part(
        "document_picture",
        "A receipt's scan, held in its shape before it arrives.",
        "document-picture",
        example=records.DOCUMENT_PICTURE,
        states=records.DOCUMENT_PICTURE_STATES,
    ),
    _part(
        "figure_drawing",
        "What a figure looks like beside its value: dots for a part of a whole, a line for a direction.",
        *("is-latest", "figure-trend", "figure-trend-dot", "figure-trend-hit"),
        draws=(CHART,),
    ),
    _part(
        "dots",
        "A count as a row of dots, those it reached filled.",
        "is-filled",
        "figure-dot",
        "figure-dots",
        example=records.DOTS,
        states=records.DOTS_STATES,
    ),
    _part(
        "dropzone",
        "A file field that also accepts a drop.",
        *("dropzone", "dropzone-files", "dropzone-hint", "dropzone-title"),
        draws=(FIELD,),
        public=True,
        example=forms.DROPZONE,
    ),
    _part(
        "empty_state",
        "The one empty state: what is missing, then what would fill it.",
        "empty",
        "empty-cta",
        public=True,
        rules="""
        **Nothing empty takes space.** A panel with nothing to show is not drawn: the page closes up rather
        than carrying a card that says "nothing here". An empty list says whether its own search or
        filters emptied it, and offers the way back. A page with nothing yet says what is missing and,
        where it can, what would fill it and where (`message`, `why`, an action); controls that only make
        sense over data wait for the data. An absence where a value would stand (a fact, a cell, a row) is
        the same part with `inline=True`: its words, quiet, in the line that holds them, with no frame.

        The message is the fact alone, one sentence ("No playbooks adopted yet."), with no how-to after
        it: the action and the page's controls say what to do.
        """,
        example=records.EMPTY_STATE,
        states=records.EMPTY_STATE_STATES,
    ),
    _part(
        "error_page",
        "An error page's body: its code, what went wrong and the way on.",
        *("error-page", "error-page-body", "error-page-code"),
        draws=(PAGE_HEAD,),
        rules="""
        `{% include "partials/_error_page.html" with code="404" title="Page not found" body="..."
        search=user.is_authenticated only %}` is the whole of what 403, 404 and 500 show. It stands
        outside the page frame and so titles itself: the status over the title, what happened and what to
        do, and the one way back to the dashboard. `request_id` names the id the log line for the failure
        carries, and `search` offers the search field.
        """,
        example=pages.ERROR_PAGE,
        states=pages.ERROR_PAGE_STATES,
    ),
    _part(
        "example_connection",
        "The mark of a saved copy's example connection.",
        "example-connection",
        rules="""
        **A saved copy never says "you" about someone else.** Anything that describes the reader's own
        connection includes this part. On a live HQ it renders nothing; on a saved copy
        (`SEVERINO_RENDERED_COPY`, set only in code by whatever renders the copy, as the demo does) it
        marks the line as an example connection, not the reader's. *Gate:*
        `hq/platform/core/tests/test_example_connection.py`, and `demo/check.py` on the built demo.
        """,
        example=connection.EXAMPLE_CONNECTION,
        states=connection.EXAMPLE_CONNECTION_STATES,
    ),
    _part(
        "form",
        "A form that writes: its token, its errors, its fields or its table, and its closing row.",
        "is-question",
        public_classes=("form",),
        public=True,
        rules="""
        **A form's fields take as many columns as fit.** Fields flow in reading order across as many
        columns as hold a field of 18rem, so a wide frame gains columns rather than stretching each input,
        and a phone's is one. A textarea, a group of choices, a field marked wide, errors, a command's
        confirmation and the actions take the row. A group of checkboxes reads across. A field marked
        `starts_pair` (a range's start) begins a row and the field after it ends the pair there, so the
        two read across together and a third column never splits them. A command's form is this grid too:
        `.form` is the one form layout. *Catalog:* `form/page-pairs`.

        **Every form a page is about is drawn one way.** A page that is a record's form or a command's is
        the frame `form_page.html`: one card spanning its column holding this one partial. It draws the
        form's token, then its errors above the fields (`partials/_form_errors.html`), then each field
        with its help under it (`templates/django/forms/field.html`). It ends with one closing row
        (`partials/_form_actions.html`): the primary verb, Cancel, and a note on what saving does where
        the page has one. Its options: `fields` names a template for fields beyond the form's own loop,
        and `method="get"` makes it a query with no write token. `bar=True` pins the closing row to the
        screen's foot once the form has changed, drawn in its place from the first draw so nothing moves.
        A field's help is its command's meaning (`records.described`), so the form and the command page
        say the same; a form writes its own only for what it alone asks. The view supplies the verb and
        the way back, never the template, and a record's form page is the frame with nothing of its own
        (`form_fields`, `form_note` from the view). A form whose verb can't be pressed because its
        connection can't make the change passes `gap`; the closing row draws the verb unusable and what it
        needs beside it. A form a script or test finds names its `data-` attributes as `hooks`. A form
        answered in another region's place gives that region's id as `in_place`. *Catalog:*
        `form/write-gap`. *Gate:* `lint:ast-grep` (`hand-drawn-form.yml`), which holds every form that
        writes to the form part, and `test_form_page.py`, which holds every routed create and edit view to
        the frame and the card to it alone.

        A form that asks one question is one visible field of a line (words, a number, a date, one
        choice) closed by its verb alone. It draws its button on the field's line where the field keeps
        18rem beside it, and under it where it cannot, as a search or lookup form does. The partial
        derives it (`asks_one_question`, then `.form.is-question`), so no page flags it. *Catalog:*
        `form/lookup`, and `form/lookup-help` with its help and an error.

        A form whose fields stand one to a row of a table (a group chosen for each record, a verdict for
        each request) is the same partial with `table_body` naming the template that draws the table, in
        its `table_region`. The form lays out no fields of its own, so it adds no box: the table reaches
        its section's sides as a table section's does, and the closing row stands under it. The form
        declares one field for each row, named by the row (`group-<id>`), so each control's name and id
        are its own. A row moved between two drawings still posts its own value, where a formset's names
        follow each row's place. Each row's cell draws its field with `partials/_row_field.html`: the
        control, its label for a screen reader alone (the column's heading names it on the page), and its
        errors under it. A row's own buttons are posting `{% button %}`s, which post the page's own form,
        so they sit in this one.
        """,
        example=forms.FORM,
    ),
    _part(
        "form_actions",
        "A form's closing row: its one verb, Cancel, and what saving does.",
        "form-actions-note",
        public=True,
        example=forms.FORM_ACTIONS,
    ),
    _part(
        "form_errors",
        "What was wrong with a submitted form as a whole, above its fields.",
        public=True,
        example=forms.FORM_ERRORS,
    ),
    _part(
        "icon",
        "One of the host's icons, by `name`, the height of the text beside it.",
        "icon",
        public=True,
        example=records.ICON,
    ),
    _part(
        "identity",
        "An account's picture, name and handle.",
        *("identity", "identity-name", "identity-picture", "identity-words"),
        rules="""
        **An account is named by this part.** It draws its picture (a `data:` URI or an HQ path, never
        another host's address) and its name, with a state beside it drawn by `{% status %}`. Its handle
        leads to it, then a line in its own words. In a card followed by a band of counts (`{% facts
        layout="band" %}`), the two share one line once the card holds the identity beside four counts
        across, as the Starred page heads with your GitHub profile, and the band takes the line below when
        narrower.
        """,
        example=connection.IDENTITY,
        states=connection.IDENTITY_STATES,
    ),
    _part(
        "insight_grid",
        "Readings that need interpreting, each with its next step.",
        *("insight-action", "insight-body", "insight-card", "insight-grid", "insight-head", "insight-title"),
        "insight-value",
        example=records.INSIGHT_GRID,
        public=True,
        states=records.INSIGHT_GRID_STATES,
    ),
    _part(
        "job_progress",
        "A running job and what it is doing.",
        *("job-bar", "job-note", "job-progress", "job-progress-head"),
        public=True,
        example=pages.JOB_PROGRESS,
        states=pages.JOB_PROGRESS_STATES,
    ),
    _part(
        "linked_text",
        "Free text with the names HQ knows in it as links.",
        "linked-text",
        rules="""
        A person's free text with the names HQ knows in it (a project's deployment notes) is this part,
        over the text's runs that `application.where_it_runs.named_links` finds: each name a link as every
        mention is, and its line breaks kept.
        """,
        example=records.LINKED_TEXT,
    ),
    _part(
        "messages",
        "What the last action said, each message in its tone.",
        "messages",
        example=pages.MESSAGES,
    ),
    _part(
        "page_navigation",
        "A page's map of its sections.",
        "page-nav",
        "page-nav-list",
        example=pages.PAGE_NAVIGATION,
        states=pages.PAGE_NAVIGATION_STATES,
    ),
    _part(
        "pagination",
        "A long list's way to its other pages.",
        "pagination",
        example=records.PAGINATION,
        states=records.PAGINATION_STATES,
    ),
    _part(
        "problems",
        "The open problems about what a page shows, the urgent ones framed above it.",
        "problems-here",
        "tone-serious",
        rules="""
        **Only what is urgent is framed.** A thing's page draws the problems about it from this part. An
        urgent one is the queue's row in a box above the page. Anything less is one unframed line (how
        urgent, what is wrong, what it means, its button). On a page made of parts it sits in the card of
        the part it is about (`partials/_problems.html#lines`); one whose part the page does not draw
        stays in the problems' place above the page. The status at the top counts them by urgency ("1
        urgent · 1 needs attention") and links to the first, and a figure in the summary band takes the
        tone of its part's problems.

        Problems drawn inside a section of a page about something else (a machine's containers, a
        container's checks, the request's path) are the queue row's dense form (`dense`): what it means
        comes first in the line beneath, so the row is its title, its marks and its buttons, and the
        section's own content stays what it is about.
        """,
        example=records.PROBLEMS,
        states=records.PROBLEMS_STATES,
    ),
    _part(
        "record_list",
        "A compact list of records, from `hq_sdk.ui.ListRow`.",
        *("cat-*", "cat-dot", "list-rows", "row-chips", "row-snippet", "whole-rows"),
        public_classes=("row-empty", "row-main", "row-meta"),
        public=True,
        rules="""
        A row of a record list (`.list-rows`) reads as one line: its meta follows its name a gap after,
        never at the row's far side across an empty middle. What can be done to the record ends its line:
        `ListRow.actions`, each a `PageAction`, drawn as compact controls, a POST through `{% button %}`,
        a glyph named aloud by its `title`, and one that cannot be pressed as `disabled`. A row that names
        a thing HQ links (a related record, a project) is given its `link`, an `EntityLink`, in place of a
        `title` and a `url`, and draws it as every mention of a thing is drawn. A project's row gives its
        `category`, drawn as that category's color (`--cat-*`) before the title, its name in `detail`. A
        search hit gives its `snippet`, where the record matched, as runs of the record's own text each
        with whether it is the match: a line of its own under the row, two lines at most, the match
        marked. It is quoted as the record holds it, so it is the one field not held to words. Search
        draws every group of results this way, the count in its filter bar, a way to each group in the
        page navigation over them, and the hint before a search as a notice. A row's `chips` are the words
        a place is known by besides its name (a machine's state, the abilities a search matched), each a
        chip (`{% status chip=True %}`) after its line. A list of places to go, as search draws the
        machines, pages and commands it found, is included `with whole_rows=True`. Each row is its title's
        link, stretched over the row, so the row is one target and its markup stays a link around a name.
        """,
        example=records.RECORD_LIST,
        states=records.RECORD_LIST_STATES,
    ),
    _part(
        "relationships",
        "What a thing is related to, a group per kind of relation.",
        "relation-source",
        example=records.RELATIONSHIPS,
    ),
    _part(
        "row_field",
        "One field of a form whose fields stand one to a row of a table, in its row.",
        draws=(FIELD,),
        public=True,
        example=forms.ROW_FIELD,
    ),
    _part(
        "page_sections",
        "The bands a detail page grows from what HQ knows, each a section of a table, a history or readouts.",
        rules="""
        A band on a detail page (`ServiceSection`) is drawn by this part, never by a page's own markup, so a
        page grows a band because a resolver produced one: a function and a registry entry, not an edit to
        a template. Its table is `{% data_table %}`; a band of audit events (`history`) is
        `{% history_table %}`; each readout a fold under it. A band whose rows are the latest of more links
        to the rest from its heading (`more_url`), and a folded one's heading is its toggle, its count
        saying what is behind it. A page draws the bands that take a flow's whole row (`spans_row=True`)
        ahead of its short ones (`spans_row=False`), so its flow paints in the order it reads.
        """,
        example=pages.PAGE_SECTIONS,
    ),
    _part(
        "row_fold",
        "The fold that opens a row to its whole record.",
        "row-fold",
        example=records.ROW_FOLD,
    ),
    _part(
        "share_list",
        "Ranked shares, each a bar against the largest.",
        "bar-label",
        "bar-list",
        "bar-row",
        "bar-track",
        "bar-value",
        rules="""
        A ranked list of shares, as the pages and sources of a site's traffic are: each row its label and
        figure on one line over a bar of its size against the largest row's, so a long tail still reads
        apart. The bar is a `<meter>`, and a label past its line is cut, whole on hover.
        """,
        example=charts.SHARE_LIST,
    ),
    _part(
        "site_header",
        "The header every page opens with: the brand, the places, the search and the user's menu.",
        *("action-badge", "brand", "demo-flag", "global-search", "nav-group", "nav-group-button", "nav-group-label"),
        *("nav-group-panel", "nav-toggle", "primary-nav", "search-key", "search-label", "site-header", "skip-link"),
        *("menu-identity", "menu-identity-picture", "menu-identity-when", "menu-identity-who", "menu-item"),
        *("menu-item-note", "menu-item-split", "menu-item-status", "menu-places", "menu-setting", "menu-settings"),
        *("menu-theme", "menu-theme-label", "menu-theme-option", "menu-theme-options", "role-mark", "switch-knob"),
        *("switch-track", "user-avatar", "user-menu", "user-menu-panel", "user-name"),
        pieces=("partials/_user_menu.html",),
        rules="""
        **The header is one row.** When room runs out, the search field collapses to a button that opens
        over the navigation, then the sections move behind the toggle. The navigation never shrinks below
        its own content, and the bar's controls all take their size from `--control-size`.

        **The user menu says whose session it is first.** It shows the person's picture, kept by HQ from
        sign-on and never loaded from the provider by a page. Then come their name, what the account may
        do, and how long ago they signed in, in hours or days. Then what can be set: the two switches
        share a line, names over tracks, and the theme has its row. Then where the menu leads.
        """,
        example=pages.SITE_HEADER,
    ),
    _part(
        "sweep_freshness",
        "How fresh each reading is, the stale and the unreachable marked.",
        "sweep",
        example=records.SWEEP_FRESHNESS,
        states=records.SWEEP_FRESHNESS_STATES,
    ),
    _part(
        "table_toolbar",
        "A list's toolbar: its filters, its sort, its columns and what is selected.",
        *("filter-count", "table-filter-menu", "table-filter-options", "table-selection", "table-sort", "table-toggle"),
        rules="""
        **A table's filter is a popover.** Its button names its panel with `popovertarget`. The panel
        (`popover`, its id `table-filter-<name>`) opens in the top layer, placed against the button by
        anchor positioning. It sits below the button from its left edge, or from its right edge when that
        would cross the screen (`position-try-fallbacks: flip-inline`, which the browser decides against
        the viewport). The browser dismisses it on an outside press or Escape, and the fragment primitive
        shows it again after a swap that replaces it, found by its id, so a ticked box leaves its menu
        open. No script places or closes it.
        """,
        states=records.TABLE_TOOLBAR_STATES,
    ),
    _part(
        "timeline",
        "Chronological linked events, from `hq_sdk.ui.Timeline`.",
        *("timeline-*", "timeline-copy", "timeline-item", "timeline-list"),
        public=True,
        example=records.TIMELINE,
    ),
    _part("tip", "The tip a chart's point shows when pointed at.", "chart-tip"),
    _part(
        "toggle_set",
        "A set of toggles that filter what a view shows.",
        "toggle-set",
        rules="""
        No chip walls: a set of on-or-off choices is this part, checkboxes in a row of their own words
        under one hidden legend, named (`name=`) when Filter sends them and unnamed when the page's script
        narrows what it already holds.
        """,
        example=forms.TOGGLE_SET,
    ),
    TOPOLOGY_MAP,
    _part(
        "visit_refresh",
        "Whether what a page shows is fresh, and the control that reads it again.",
        *("ask-quiet", "strut", "visit-slot", "visit-status"),
        draws=(ASK,),
        rules="""
        **A page about one thing asks for that thing's readings.** A machine's page, a service's page and
        a record's page name which page they are as `page_visit` (`visit_refresh.page_visit`). They do so
        only where a controller will read what it asks for (`cadence.WorkerStanding.takes_reads`): one
        whose heartbeat says it reports to this HQ, and is not silent. Where none will, the page draws no
        ask and no note, so it never promises a refresh that cannot come. `page.html` draws the ask, and
        its note says what it waits for at the end of the head's first line, in room of its own; a line
        whose longest word or date leaves it none hides it. An unseen strut of each sentence HQ writes for
        it word for word sizes its slot (`asks.REFRESHING`, `REFRESHED`, `NEWER_READING` and
        `SHOW_NEWER`, which the script writes from the page). So those sentences are always whole, and the
        trail beside it never moves whatever the note says. Only a failure's sentence is cut, whole in its
        title. The dashboard names itself only when a reading its queue is drawn from is due
        (`visit_refresh.PAGES`), and the connection panel only when the tailnet, container or posture
        readings it shows are due. The panel asks once its dialog is open, and names itself as the part
        to refresh. A region that carries its own address (`data-fragment`) is fetched from there, so the
        panel fills in place on whatever page the dialog opened over.

        Until the reading lands, a value from an older reading is said as last seen, as of when it was
        read, never as how things stand now: its age is no fault. Only a read that fails says so, in the
        ask's note, with why.

        Once the page is showing, it posts that. HQ works out which kinds the page is assembled from and
        asks the controller for those whose last attempt is older than their cadence
        (`hq/platform/application/freshness.py`).

        - Opening a page never logs in to a machine: a kind whose reader opens a shell is left to the
          scheduled read.
        - A page in a background tab asks for nothing, a GET never does, and the request cannot name a
          connection or a kind.
        - When the reading lands, the page's content is fetched again and swapped in place, the ask's own
          line kept. Only if that fails does the page load again, once; if somebody has touched it or it
          reloaded a moment ago, it says a newer reading is in and leaves showing it to them.
        """,
        states=pages.VISIT_REFRESH_STATES,
    ),
    Part(
        name="",
        partial="projects/_repository_postures.html",
        about="Each repository's checks as a meter of those met.",
        classes=(
            *("posture-access", "posture-meter", "posture-meter-break", "posture-repo-name"),
            *("posture-repos", "posture-score"),
        ),
        draws=(MONTH_GRID,),
        rules="""
        Repositories against their standard: a row each of its name, a meter with a segment per check, its
        score and who can reach it. The meter follows the name column, so one check sits in the same place
        on every row.
        """,
        example=records.REPOSITORY_POSTURES,
    ),
)
