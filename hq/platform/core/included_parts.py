"""The shared parts no tag draws, which a template includes by path, each
declared once as a ``Part``. A tag's part is declared where its tag is
registered; the template contract reads both (``template_contract.parts``).
A ``public`` one is an extension's to include too."""

from hq.platform.core.template_parts import Part


def _part(partial: str, about: str, *classes: str, **declared) -> Part:
    return Part(name="", partial=f"partials/_{partial}.html", about=about, classes=classes, **declared)


FIELD = Part(
    name="",
    partial="django/forms/field.html",
    about="A form field as every form draws it: its label, its control, its help and its errors.",
    classes=("check-row", "command-confirm", "field", "field-effect", "has-errors", "is-pair-start", "is-wide"),
    example="{{ field.as_field_group }}",
)
CHART = _part(
    "chart",
    "A measure over time from `hq_sdk.ui.line_chart`, or quantities that add up from "
    "`hq_sdk.ui.stacked_bar_chart`, or a spatial path from `hq_sdk.ui.PathChart`.",
    *("bar-chart", "chart-axis", "chart-band-*", "chart-category", "chart-cats", "chart-dot", "chart-frame"),
    *("chart-frame-dense", "chart-grid", "chart-hit", "chart-labels", "chart-legend", "chart-line"),
    *("chart-mark", "chart-mark-label", "chart-path", "chart-path-frame", "chart-path-marker", "chart-path-scale"),
    *("chart-path-segment", "chart-scroll", "chart-tick", "chart-trend", "line-chart", "series-*"),
    pieces=("partials/_chart_legend.html", "partials/_chart_path.html"),
    public=True,
)
CADENCE = _part(
    "cadence",
    "Whether a thing happened, period by period, from `hq_sdk.ui.Cadence`.",
    *("cadence", "cadence-count", "cadence-head", "cadence-label", "cadence-mark", "cadence-note"),
    *("cadence-strip", "cadence-title", "cadence-week", "is-hit", "is-no-data"),
    public=True,
)
MONTH_GRID = _part(
    "month_grid",
    "A calendar month as a grid of weeks, each day its events, a span across the days it covers.",
    *("is-*", "is-all-day", "is-compact", "is-end", "is-gap", "is-outside", "is-past", "is-selected", "is-start"),
    *("is-today", "is-weekend", "month-date", "month-day", "month-day-head", "month-grid", "month-item"),
    *("month-item-dot", "month-mark", "month-more", "month-of", "month-span", "month-week", "month-weekday"),
    draws=(CHART,),
    example='{% section "Example month" %}{% include "partials/_month_grid.html" %}{% endsection %}',
)
WEEK_PLAN = _part(
    "week_plan",
    "Which days of the week a thing is scheduled for, from `hq_sdk.ui.WeekPlan`.",
    *("is-planned", "week-plan", "week-plan-day", "week-plan-label", "week-plan-mark", "week-plan-note"),
    "week-plan-notes",
    draws=(CHART,),
    public=True,
)
ASK = _part(
    "ask",
    "A do-this-now control and how its work stands, from `hq_sdk.outbound.ask`.",
    *("ask", "ask-elapsed", "ask-note", "ask-said", "ask-sentence", "ask-why", "ask-word"),
    public=True,
)
PAGE_HEAD = _part(
    "page_head",
    "A page's head: its trail, its title, its facts and its actions, the overflow of them in a menu.",
    *("is-column", "overflow-menu", "overflow-menu-copies", "overflow-menu-panel", "page-actions", "page-facts"),
    *("page-head", "page-head-copy", "page-title-row", "page-trail", "page-trail-row"),
)
MODAL = _part(
    "modal",
    "A dialog: its title, what it is for, the body a template names, and its close.",
    *("modal", "modal-*", "modal-close", "modal-foot", "modal-head", "modal-lede", "modal-panel"),
    example=(
        '<a href="/example/" data-modal-open="example">Open</a>'
        '{% include "partials/_modal.html" with id="example" title="Example dialog" '
        'body_template="partials/_empty_state.html" %}'
    ),
)
READING_AGE = _part(
    "reading_age",
    "When a reading was taken, `at` the moment, in the one wording every page uses; `unread` why there is none.",
    *("is-stale", "reading-age"),
    public=True,
)
PATH_HOPS = _part(
    "path_hops",
    "The hops a request takes to a service, each with the proof HQ holds of it.",
    *("hop-body", "hop-icon", "hop-label", "hop-proof-key", "is-attention", "is-proven", "is-unknown"),
    *("path-proof-note", "request-path", "route"),
)
RESOLUTION_WORKFLOW = _part(
    "resolution_workflow",
    "What a person does about a finding: a command to run, or what to do in words.",
    "resolution-workflow",
    example='{% include "partials/_resolution_workflow.html" with steps=steps only %}',
)
WORK_QUEUE = _part(
    "work_queue",
    "What waits on the operator, each row what wants doing and why, or that nothing does.",
    *("attention-clear", "attention-links", "attention-reason", "is-mark", "row-line"),
    pieces=("partials/_queue_entries.html", "partials/_queue_rows.html", "partials/_urgency_pill.html"),
)

PARTS = (
    FIELD,
    Part(
        name="",
        partial="django/forms/widgets/choice_cards.html",
        about="A choice of one, each option a card saying what it does.",
        classes=("choice-card", "choice-card-what", "choice-cards"),
        example="{{ field.as_field_group }}",
    ),
    Part(
        name="",
        partial="django/forms/widgets/suffixed.html",
        about="A text input with its unit after it.",
        classes=("input-suffix",),
        example="{{ field.as_field_group }}",
    ),
    CHART,
    CADENCE,
    MONTH_GRID,
    WEEK_PLAN,
    ASK,
    PAGE_HEAD,
    MODAL,
    READING_AGE,
    PATH_HOPS,
    RESOLUTION_WORKFLOW,
    WORK_QUEUE,
    _part(
        "activity_calendar",
        "One month of plan against execution, from `hq_sdk.ui.ActivityCalendar`.",
        *("activity-calendar", "calendar-controls", "calendar-date", "calendar-day", "calendar-dot"),
        *("calendar-legend", "calendar-marks", "calendar-period", "calendar-weekday"),
        *("is-filler", "is-missed"),
        draws=(CHART, MONTH_GRID, WEEK_PLAN),
        public=True,
    ),
    _part(
        "allocation",
        "Parts of one whole as one bar, from `hq_sdk.ui.AllocationBar`.",
        public_classes=("allocation", "allocation-legend", "stacked"),
        draws=(CHART,),
        public=True,
    ),
    _part(
        "attention_list",
        "Decisions waiting on the operator, from the entries `hq_sdk.plugin.plugin_attention_items` gathers.",
        public=True,
    ),
    _part(
        "cadence_matrix",
        "Several cadences over the same periods, from `hq_sdk.ui.CadenceMatrix`.",
        *("cadence-cell", "cadence-detail", "cadence-period", "cadence-total"),
        draws=(CADENCE,),
        public=True,
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
    ),
    _part(
        "certificate_mark",
        "A certificate's mark: whether it is in force or waits on its issue.",
        "cert-mark",
        "is-pending",
    ),
    _part("change", "One value changed: what it was, an arrow, what it is.", "change", "change-arrow", "change-to"),
    _part(
        "chart_tabs",
        "Up to four charts of one thing in the space of one.",
        *("chart-panel", "chart-panel-*", "chart-tab", "chart-tab-input", "chart-tablist", "chart-tabs"),
        public=True,
    ),
    _part("command", "A command to copy and run, with its copy control.", "command", "operator-command"),
    _part(
        "command_center",
        "Search across HQ, its results grouped, each a place to go or a command to run.",
        *("command-center-arrow", "command-center-copy", "command-center-destination", "command-center-empty"),
        *("command-center-enter-hint", "command-center-evidence", "command-center-footer"),
        *("command-center-group-label", "command-center-loading", "command-center-more", "command-center-option"),
        *("command-center-options", "command-center-palette", "command-center-query", "command-center-results"),
        "command-center-title",
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
    ),
    _part(
        "demo_about",
        "The About dialog's body on a saved copy of HQ: what it is, the pages to try, what built it.",
        draws=(MODAL,),
    ),
    _part(
        "demo_foot", "A saved copy's foot, which opens its About dialog.", "demo-about", "demo-about-dot", "demo-foot"
    ),
    _part("document_picture", "A receipt's scan, held in its shape before it arrives.", "document-picture"),
    _part(
        "figure_drawing",
        "What a figure looks like beside its value: dots for a part of a whole, a line for a direction.",
        *("is-latest", "figure-trend", "figure-trend-dot", "figure-trend-hit"),
        draws=(CHART,),
    ),
    _part("dots", "A count as a row of dots, those it reached filled.", "is-filled", "figure-dot", "figure-dots"),
    _part(
        "dropzone",
        "A file field that also accepts a drop.",
        *("dropzone", "dropzone-files", "dropzone-hint", "dropzone-title"),
        draws=(FIELD,),
        public=True,
    ),
    _part(
        "empty_state",
        "The one empty state: what is missing, then what would fill it.",
        "empty",
        "empty-cta",
        public=True,
    ),
    _part(
        "error_page",
        "An error page's body: its code, what went wrong and the way on.",
        *("error-page", "error-page-body", "error-page-code"),
        draws=(PAGE_HEAD,),
    ),
    _part("example_connection", "The mark of a saved copy's example connection.", "example-connection"),
    _part(
        "form",
        "A form that writes: its token, its errors, its fields or its table, and its closing row.",
        "is-question",
        public_classes=("form",),
        public=True,
    ),
    _part(
        "form_actions",
        "A form's closing row: its one verb, Cancel, and what saving does.",
        "form-actions-note",
        public=True,
    ),
    _part("form_errors", "What was wrong with a submitted form as a whole, above its fields.", public=True),
    _part("icon", "One of the host's icons, by `name`, the height of the text beside it.", "icon", public=True),
    _part(
        "identity",
        "An account's picture, name and handle.",
        *("identity", "identity-name", "identity-picture", "identity-words"),
    ),
    _part(
        "insight_grid",
        "Readings that need interpreting, each with its next step.",
        *("insight-action", "insight-body", "insight-card", "insight-grid", "insight-head", "insight-title"),
        "insight-value",
        example=(
            '{% section "Example insights" %}'
            '{% include "partials/_insight_grid.html" with label="Example insights" %}{% endsection %}'
        ),
        public=True,
    ),
    _part(
        "job_progress",
        "A running job and what it is doing.",
        *("job-bar", "job-note", "job-progress", "job-progress-head"),
        public=True,
    ),
    _part("linked_text", "Free text with the names HQ knows in it as links.", "linked-text"),
    _part("messages", "What the last action said, each message in its tone.", "messages"),
    _part("page_navigation", "A page's map of its sections.", "page-nav", "page-nav-list"),
    _part("pagination", "A long list's way to its other pages.", "pagination"),
    _part(
        "problems",
        "The open problems about what a page shows, the urgent ones framed above it.",
        "problems-here",
        "tone-serious",
    ),
    _part(
        "record_list",
        "A compact list of records, from `hq_sdk.ui.ListRow`.",
        *("cat-*", "cat-dot", "list-rows", "row-chips", "row-snippet", "whole-rows"),
        public_classes=("row-empty", "row-main", "row-meta"),
        public=True,
    ),
    _part("relationships", "What a thing is related to, a group per kind of relation.", "relation-source"),
    _part(
        "row_field",
        "One field of a form whose fields stand one to a row of a table, in its row.",
        draws=(FIELD,),
        public=True,
    ),
    _part("row_fold", "The fold that opens a row to its whole record.", "row-fold"),
    _part(
        "share_list",
        "Ranked shares, each a bar against the largest.",
        "bar-label",
        "bar-list",
        "bar-row",
        "bar-track",
        "bar-value",
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
    ),
    _part(
        "sweep_freshness",
        "How fresh each reading is, the stale and the unreachable marked.",
        "sweep",
    ),
    _part(
        "table_toolbar",
        "A list's toolbar: its filters, its sort, its columns and what is selected.",
        *("filter-count", "table-filter-menu", "table-filter-options", "table-selection", "table-sort", "table-toggle"),
    ),
    _part(
        "timeline",
        "Chronological linked events, from `hq_sdk.ui.Timeline`.",
        *("timeline-*", "timeline-copy", "timeline-item", "timeline-list"),
        public=True,
    ),
    _part("tip", "The tip a chart's point shows when pointed at.", "chart-tip"),
    _part("toggle_set", "A set of toggles that filter what a view shows.", "toggle-set"),
    _part(
        "topology_map",
        "The map of how things connect, laid out and filtered by its own script, each node's edges stated from it.",
        *("topology-detail", "topology-kind-*", "topology-kind-mark", "topology-lane", "topology-map"),
        *("topology-node", "topology-node-*", "topology-node-degree", "topology-node-link", "topology-node-list"),
        *("topology-node-select", "topology-node-title", "topology-workspace", "relation-arrow", "relation-kind"),
        *("relation-link", "relation-list", "relation-other", "relation-reading", "relation-values", "relation-verb"),
        pieces=("partials/_relation_list.html",),
    ),
    _part(
        "visit_refresh",
        "Whether what a page shows is fresh, and the control that reads it again.",
        *("ask-quiet", "strut", "visit-slot", "visit-status"),
        draws=(ASK,),
    ),
    Part(
        name="",
        partial="projects/_repository_postures.html",
        about="Each repository's checks as a meter of those met.",
        classes=(
            *("is-met", "is-unmet", "posture-access", "posture-meter", "posture-meter-break", "posture-repo-name"),
            *("posture-repos", "posture-score"),
        ),
        draws=(MONTH_GRID,),
    ),
)
