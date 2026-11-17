# Shared UI

What an extension's templates may use of the host's: the frames and their
blocks, the public partials and what each reads, the host tags and filters,
and the classes and `data-` hooks. Written by `manage.py shared_ui_reference`
from the host's templates (`hq.platform.core.template_contract`); a test
fails when this file falls behind. The rules around it are in
[Plugins](Plugins.md#shared-ui-contract).

| Frame | Blocks a page fills | What it is |
| --- | --- | --- |
| `confirm_page.html` | `consequences` | A destructive action's confirmation: `consequences` says what will happen. |
| `form_page.html` | nothing | A page that is one form, a record's: its card and `partials/_form.html`, closing with `form_verb` and `cancel_url`; `form_fields` names a template for fields beyond the form's own loop and `form_note` says what saving does, each from the view, so the page fills no block. |
| `list_page.html` | `foot`, `row` (given `forloop`, `row`), `summary` | A `TableListMixin` list: toolbar, table, empty state and pagination. `row` is one row's cells, `summary` sits above the table and is redrawn with it when a search or filter changes, `foot` is its `<tfoot>`. |
| `page.html` | `page`, `print_footer` | Any page: the head, actions, dialogs and section navigation, drawn from `page`. |

| Partial | Required | Optional | What it is |
| --- | --- | --- | --- |
| `partials/_activity_calendar.html` | `calendar` | `calendar_id` | One month of plan against execution, from `hq_sdk.ui.ActivityCalendar`. |
| `partials/_allocation.html` | `allocation` | `legend`, `stacked` | Parts of one whole as one bar, from `hq_sdk.ui.AllocationBar`. |
| `partials/_ask.html` | `ask` | `extra`, `heard` | A do-this-now control and how its work stands, from `hq_sdk.outbound.ask`. |
| `partials/_attention_list.html` | `empty`, `entries` | nothing | Decisions waiting on the operator, from the entries `hq_sdk.plugin.plugin_attention_items` gathers. |
| `partials/_cadence.html` | `cadence` | nothing | Whether a thing happened, period by period, from `hq_sdk.ui.Cadence`. |
| `partials/_cadence_matrix.html` | `matrix` | `compact`, `label` | Several cadences over the same periods, from `hq_sdk.ui.CadenceMatrix`. |
| `partials/_chart.html` | `chart` | nothing | A measure over time from `hq_sdk.ui.line_chart`, or quantities that add up from `hq_sdk.ui.stacked_bar_chart`, or a spatial path from `hq_sdk.ui.PathChart`. |
| `partials/_chart_tabs.html` | `charts`, `group` | nothing | Up to four charts of one thing in the space of one. |
| `partials/_dropzone.html` | `field` | `hint`, `title` | A file field that also accepts a drop. |
| `partials/_empty_state.html` | `message` | `action_label`, `action_url`, `inline`, `remedy`, `why` | The one empty state: what is missing, then what would fill it. |
| `partials/_form.html` | `bar`, `cancel_url`, `choice_field`, `choices`, `form`, `note`, `verb` | `action`, `fields`, `gap`, `hooks`, `in_place`, `method`, `next`, `table_body` | A form that writes: its token, its errors, its fields or its table, and its closing row. |
| `partials/_form_actions.html` | `choice_field`, `verb` | `bar`, `cancel_url`, `choices`, `gap`, `in_place`, `live`, `note` | A form's closing row: its one verb, Cancel, and what saving does. |
| `partials/_form_errors.html` | nothing | `form` | What was wrong with a submitted form as a whole, above its fields. |
| `partials/_icon.html` | `name` | nothing | One of the host's icons, by `name`, the height of the text beside it. |
| `partials/_insight_grid.html` | `items` | `label`, `level` | Readings that need interpreting, each with its next step. |
| `partials/_job_progress.html` | `job` | nothing | A running job and what it is doing. |
| `partials/_kpi_grid.html` | `items` | `featured`, `label` | Responsive linked or static figures. |
| `partials/_reading_age.html` | nothing | `at`, `reading`, `unread` | When a reading was taken, `at` the moment, in the one wording every page uses; `unread` why there is none. |
| `partials/_record_list.html` | `rows` | `empty`, `label`, `whole_rows` | A compact list of records, from `hq_sdk.ui.ListRow`. |
| `partials/_row_field.html` | `field` | nothing | One field of a form whose fields stand one to a row of a table, in its row. |
| `partials/_timeline.html` | nothing | `timeline` | Chronological linked events, from `hq_sdk.ui.Timeline`. |
| `partials/_week_plan.html` | `plan` | nothing | Which days of the week a thing is scheduled for, from `hq_sdk.ui.WeekPlan`. |

Host tags: `{% arrow %}`, `{% button %}`, `{% button_row %}`, `{% card %}`, `{% dot %}`, `{% dot_list %}`, `{% empty_value %}`, `{% entity %}`, `{% fact %}`, `{% facts %}`, `{% filter_bar %}`, `{% muted %}`, `{% notice %}`, `{% outside_link %}`, `{% present %}`, `{% prose_list %}`, `{% reference %}`, `{% referenced_by %}`, `{% row_actions %}`, `{% row_editor %}`, `{% row_group %}`, `{% row_toggle %}`, `{% section %}`, `{% status %}`, `{% table_heading %}`, `{% table_region %}`, `{% words %}`, `{% write_token %}`. Host filters: `|breakable[:arg]`, `|counted:arg`, `|measured:arg`, `|money[:arg]`, `|or_empty`, `|outside`, `|readout`, `|record[:arg]`, `|span`, `|web_url`, `|when[:arg]`, `|when_text[:arg]`. Django's own tags and filters, and `{% load %}` of `static` or the extension's own libraries.

Classes: `actions-col`, `address-list`, `allocation`, `allocation-legend`, `cell-data`, `cell-part`, `cell-whole`, `code-list`, `compact`, `compact-table`, `count`, `danger`, `data-table`, `date-col`, `domain-charts`, `errors`, `eyebrow`, `finding`, `form`, `ghost`, `help`, `icon-link`, `inline`, `key-col`, `keyboard-only`, `name-line`, `narrow-more`, `narrow-only`, `nowrap`, `one-line`, `optional-col`, `page-lede`, `primary`, `prose`, `row-actions`, `row-below`, `row-empty`, `row-gutter`, `row-less`, `row-main`, `row-meta`, `row-more`, `save-bar`, `section-flow`, `section-foot`, `split`, `stacked`, `subtle-link`, `table-clamp`, `table-scroll`, `tight-col`, `two-col`, `visually-hidden`, `whole`.

Script hooks: `data-sort`, `data-submit-busy`, `data-submit-label`, `data-submit-status`, `data-tip`. Styled attributes: `data-chart`.

| Rule | Drawn by hand | Write instead |
| --- | --- | --- |
| `hand-drawn-change-table` | A before-and-after table drawn by hand, where the change table part draws it. | Build an approval_review.ChangePreview and write `{% change_table preview %}`. |
| `hand-drawn-empty-said-quietly` | An empty line drawn by hand, where the empty state part draws it. | Write `{% include "partials/_empty_state.html" with message="..." %}`, or a `{% section %}`'s `{% empty %}`. |
| `hand-drawn-filter-bar` | A search field or select drawn by hand, where the filter bar and select parts draw it. | Write `{% filter_bar search="..." query=q filtered=... clear=... %}``{% choices %}``{% select ... %}``{% summary %}`count`{% endfilter_bar %}`. |
| `hand-drawn-fold` | A fold drawn by hand, where the section part draws it. | Write `{% section "Heading" fold=True %}`body`{% endsection %}`. |
| `hand-drawn-form` | A form that writes drawn by hand, where the form part draws it. | Write `{% include "partials/_form.html" %}`, or `{% button ... post=True %}` for a single verb. |
| `hand-drawn-heading` | A heading drawn by hand, where the section part or the page frame draws it. | Write the heading of `{% section "Heading" %}`, a sub-head a `{% section %}` inside it (it heads itself a level down); the page's own title is the frame's, from page. |
| `hand-drawn-icon` | An icon drawn by hand, where the icon part draws it. | Write `{% include "partials/_icon.html" with name="..." %}`; a drawing that says something is role="img". |
| `hand-drawn-missing-value` | A missing value's mark drawn by an if, where the value's own tag or filter draws it. | Write {{ x\|or_empty }} for a plain value, {{ x\|money }} (or measured, seconds, bytes, when) alone for a figure or a moment, and `{% present x %}`...`{% endpresent %}` for a value shown in markup. |
| `hand-drawn-prose-list` | A list of sentences drawn by hand, where the prose list part draws it. | Write `{% prose_list %}``<li>`...`</li>``{% endprose_list %}`, with numbered=True for items read in order. |
| `hand-drawn-record-link` | A link to a record's page drawn by hand, where the entity tag names the record by the page its model declares. | Write `{% entity row\|record label=row.name %}`, or row\|record:"section" for a part of its page. |
| `hand-drawn-row-actions` | A row's actions drawn by hand, where the row actions part draws them. | Write `{% row_actions actions %}` from hq_sdk.pages.PageAction, which draws the row's cell. |
| `hand-drawn-row-group` | A group of rows headed by hand, where the row group tag heads it. | Write `{% row_group label count %}`. |
| `hand-drawn-run-state` | How work stands is drawn by hand, where the ask or the status part draws it. | Draw the work as an Ask (`partials/_ask.html`), or its state with `{% status run.word tone=run.tone data_ask_label=True %}` from job\|job_run. |
| `hand-drawn-section` | A `<section>` element drawn by hand, where the section part draws it. | Write `{% section "Heading" %}`body`{% endsection %}`, or a `<div>` for one that only groups a layout. |
| `hand-drawn-split-count` | A count split from its words, where the counted filter writes them as one. | Write {{ n\|counted:"thing" }}, the figure and its words as one, or `{% fact "Label" value=n %}`. |
| `hand-drawn-split-figure` | A figure apart from its word, where a value filter writes them as one. | Write {{ n\|counted:"thing" }} for a count, or {{ x\|measured:"mi" }} for a unit or a word the count leaves alone ("more"), the figure and its word as one value. |
| `hand-drawn-stretched-shape` | A round shape or text inside a stretched drawing, which distorts it. | Draw a dot as a zero-length line of class chart-dot, and a label in the unscaled svg.chart-labels layer. |
| `hand-drawn-table` | A table drawn outside its region. | Write `{% table_region "Label" %}` around its `<table class="data-table">`. |
