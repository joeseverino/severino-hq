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
| `partials/_reading_age.html` | nothing | `at`, `reading`, `unread` | When a reading was taken, `at` the moment, in the one wording every page uses; `unread` why there is none. |
| `partials/_record_list.html` | `rows` | `empty`, `label`, `whole_rows` | A compact list of records, from `hq_sdk.ui.ListRow`. |
| `partials/_row_field.html` | `field` | nothing | One field of a form whose fields stand one to a row of a table, in its row. |
| `partials/_timeline.html` | nothing | `timeline` | Chronological linked events, from `hq_sdk.ui.Timeline`. |
| `partials/_week_plan.html` | `plan` | nothing | Which days of the week a thing is scheduled for, from `hq_sdk.ui.WeekPlan`. |

| Tag | Later parts | What it is | Written |
| --- | --- | --- | --- |
| `{% arrow %}` | nothing | The arrow that ends a link onward, written straight after its last word. | `<a href="/example/">All of them{% arrow %}</a>` |
| `{% button %}` | nothing | Every control that looks like a button: a link, a post through the page's one shared form, its form's own, or one a script acts on. | `{% button "Restart" url post=True look="primary" %}` |
| `{% button_row %}` | nothing | A row of buttons that stands on its own: a form's closing row, a card's decisions, a page's way on. | `{% button_row %}{% button "Save" submit=True look="primary" %}{% endbutton_row %}` |
| `{% card %}` | nothing | The frame a page's content is grouped in; a titled part of a page is a `{% section %}`. | `{% card label="Hosts" %}<p>Body</p>{% endcard %}` |
| `{% dot %}` | nothing | The middle dot between two parts of one line, written straight after the first. | `{{ count }}{% dot %}{{ age }}` |
| `{% dot_list %}` | nothing | A run of whole parts on one line, each an `<li>`, a dot between each that no line opens on: a fact's links, a row's context line, a band's details. | `{% dot_list %}<li><a href="/example/">example-host</a></li><li>2 days ago</li>{% enddot_list %}` |
| `{% empty_value %}` | nothing | The mark for a value that is not there, where there is no value to test; `\|or_empty` tests one. | `{% empty_value %}` |
| `{% entity %}` | nothing | One thing named, as the link builder answered for it: a link to its page, or its name. | `{% entity "machine"\|entity_of:name %}` |
| `{% fact %}` | `detail`, `heading`, `panel`, `foot` | One fact of a `{% facts %}`: its label, its value and the detail under it. Given `value=` it encloses nothing; a value in markup is its block, its detail after `{% detail %}`, and a panel its value opens in place after `{% heading %}`, `{% panel %}` and `{% foot %}`. | `{% facts %}{% fact "Version" icon="layers" %}<code>1.4.2</code>{% detail %}Tag only{% endfact %}{% endfacts %}` |
| `{% facts %}` | nothing | Every label and its value a page shows, each a `{% fact %}`; given `items=` it draws each item as a fact and encloses nothing. | `{% facts layout="band" %}{% fact "Running" value="3" url="/example/" %}{% endfacts %}` |
| `{% filter_bar %}` | `choices`, `summary` | Every query a page asks of what it lists, in one bar: its search and any controls of its own, its `choices` part, the one button that sends them, then its `summary` part. A list's table toolbar is the same bar. | `{% filter_bar search="Filter by name" query=q filtered=narrowed clear=every_url %}{% choices %}{% select "kind" kinds label="Kind" every="kind" %}{% endfilter_bar %}` |
| `{% muted %}` | nothing | Words said quietly beside what they are about: a context line, a sentence under a value. A cell or a list item holds it; a run of parts said quietly is `{% dot_list muted=True %}`. | `{% muted element="p" %}A sentence under it.{% endmuted %}` |
| `{% notice %}` | `title` | Every banner and message a page shows, in one of its tones; a message among the page's messages is the same part in a toast's placement. A title the page composes is its `{% title %}` part, after the body. | `{% notice tone="warning" title="2 jobs failed" %}<p>What to do.</p>{% endnotice %}` |
| `{% outside_link %}` | nothing | A link to a page outside HQ around markup of the page's own (a chip, an icon, a card); a name is `{% entity url\|outside label=name %}`. | `{% outside_link url="https://example.com/docs/" %}<span class="tag">Docs</span>{% endoutside_link %}` |
| `{% present %}` | nothing | What it encloses, drawn only when the value is there, else the mark for a missing value. | `{% present repo.release %}<code>{{ repo.release }}</code>{% endpresent %}` |
| `{% prose_list %}` | nothing | A list of sentences, each an `<li>` marked as the browser marks it. | `{% prose_list %}<li>A sentence.</li>{% endprose_list %}` |
| `{% reference %}` | nothing | The link for the reference a record holds, for `{% entity %}`, or none the viewer may not see. | `{% reference expense "paid_from" as account %}{% entity account %}` |
| `{% referenced_by %}` | nothing | Everything that names a thing, for that thing's page, a card per kind of reference. | `{% referenced_by "machine" machine.name %}` |
| `{% row_actions %}` | nothing | The cell that ends a row with what can be done to its record, each a compact control. | `{% table_region "Example actions" %}<table class="data-table"><tbody><tr><td>{{ row.name }}</td>{% row_actions row.actions %}</tr></tbody></table>{% endtable_region %}` |
| `{% row_editor %}` | nothing | The way into a row's editor, and the editor itself under the row where the page's address opens it. | `{% row_editor form row.pk label="Edit" action=row.edit_url verb="Save" %}` |
| `{% row_group %}` | nothing | The heading row of one group of a table's rows, first in its `tbody`: its name and how many rows. | `{% table_region "Example groups" %}<table class="data-table"><tbody>{% row_group "Example group" 3 %}<tr><td>example-host</td></tr></tbody></table>{% endtable_region %}` |
| `{% row_toggle %}` | nothing | The toggle that opens a row to all it holds, last in the cell that names it, in a table with no headings. | `{% table_region "Example toggles" %}<table class="data-table"><tbody><tr><td>{{ row.name }}{% row_toggle %}</td></tr></tbody></table>{% endtable_region %}` |
| `{% section %}` | `aside`, `empty`, `foot` | A titled part of a page in its frame, its head on the page and its body in the frame. | `{% section "Agents" count=n %}<p>Body</p>{% aside %}A line.{% empty %}None yet.{% endsection %}` |
| `{% status %}` | nothing | One short state, drawn in its tone and said in sentence case, the one way a page draws a state. | `{% status "Blocked" tone="attention" %}` |
| `{% table_heading %}` | nothing | One column heading of a table: its kind, the rank it leaves by and its sort control. | `{% table_region "Example updates" %}<table class="data-table"><thead><tr>{% table_heading "Name" %}{% table_heading "Updated" "status" drop=2 %}</tr></thead><tbody><tr><td>example-host</td><td>An hour ago</td></tr></tbody></table>{% endtable_region %}` |
| `{% table_region %}` | nothing | The box a table fits, which draws what follows from its headings, and, with no rows, the one empty state in its place. | `{% table_region "Hosts" empty="No hosts yet." %}<table class="data-table"><thead><tr>{% table_heading "Name" %}</tr></thead><tbody><tr><td>example-host</td></tr></tbody></table>{% endtable_region %}` |
| `{% words %}` | nothing | What a button for a command says, from its declaration, for prose that names the one to press. | `Press {% words "agent.access.set" %}.` |
| `{% write_token %}` | nothing | What every write form carries in place of `{% csrf_token %}`, so the form sent twice acts once. | `<form method="post">{% write_token %}...</form>` |

| Tag | Option | Default | Choices | What it does |
| --- | --- | --- | --- | --- |
| `button` | `label`, first and unnamed | required |  | Its words. |
| `button` | `arrow` | `False` |  | Ends with the arrow of a link onward. |
| `button` | `count` | `None` |  | A count of what it holds, after its words. |
| `button` | `css` |  |  | Classes in place of its look, for a control not drawn as a button. |
| `button` | `current` | `False` |  | It names the page being drawn. |
| `button` | `disabled` | `False` |  | Neither a link nor a form, kept in the tab order. |
| `button` | `fields` | `None` |  | What a posting one sends besides its own field, as a form of its own. |
| `button` | `hit` | `False` |  | Drawn smaller than a target, so it asks for its hit area. |
| `button` | `icon` |  |  | The icon its words open with. |
| `button` | `look` |  |  | How it looks, its words from `ui.BUTTON_LOOKS`. |
| `button` | `modal` |  |  | The dialog a link opens where script runs. |
| `button` | `name` |  |  | The field it sends. |
| `button` | `post` | `False` |  | Posts to its url. |
| `button` | `pressed` | `None` |  | A toggle, and which way it stands. |
| `button` | `submit` | `False` |  | Sends its own form. |
| `button` | `title` |  |  | Its `title`, read where it stands. |
| `button` | `url` |  |  | Where it leads, or with `post` where it posts. |
| `button` | `value` |  |  | That field's value. |
| `button` | `**attributes` |  |  | Each `data_*` hook, and the popover, dialog command or `hidden` a script acts on. |
| `button_row` | `bar` |  |  | Pinned to the bottom of the screen once its form has changed. |
| `card` | `alone` |  |  | The whole of a page that stands outside the page frame (sign-in). |
| `card` | `busy` |  |  | Its content is still coming. |
| `card` | `css` |  |  | The classes of where it stands. |
| `card` | `element` | `"section"` | `section`, `article`, `figure`, `details`, `div` | The element it is. |
| `card` | `heading` |  |  | The id of the heading that names it. |
| `card` | `id` |  |  | Its `id`. |
| `card` | `label` |  |  | What it holds, for a card with no heading. |
| `card` | `open` |  |  | A `details` card drawn open. |
| `card` | `data_*` | | | Each a `data-` attribute. |
| `dot_list` | `muted` | `False` | `False`, `True` | Said quietly. |
| `dot_list` | `data_*` | | | Each a `data-` attribute. |
| `entity` | `link`, first and unnamed | required |  | The thing's link, as the link builder answered for it. |
| `entity` | `code` | `False` |  | Its name drawn as code. |
| `entity` | `label` |  |  | The words it is named by, in place of its own. |
| `entity` | `mark` |  |  | The punctuation after the name, held to it, or a list's `forloop`. |
| `fact` | `label`, first and unnamed | required |  | Its label. |
| `fact` | `detail` |  |  | The words under the value, or a list of them, a line each. |
| `fact` | `detail_tone` |  |  | A status tone the words under the value are colored by. |
| `fact` | `detail_url` |  |  | The page the words under the value lead to. |
| `fact` | `drawing` |  |  | What its value looks like, beside it: `Dots` for a part of a whole, `Trend` for a direction, or under it an `AllocationBar` with its legend. |
| `fact` | `icon` |  |  | The icon beside it. |
| `fact` | `id` |  |  | The id of the panel its value opens in place, given a `panel` part. |
| `fact` | `modal` |  |  | The dialog its value's link opens where script runs. |
| `fact` | `title` |  |  | What pointing at it says. |
| `fact` | `tone` |  |  | A status tone the fact is colored by, for what it means. |
| `fact` | `url` |  |  | The page in HQ its value links to; an outside one is the block form's `{% entity %}`. |
| `fact` | `value` |  |  | Its value, which makes it one tag with no end. |
| `fact` | `zero` |  |  | Its value is nothing counted, drawn quiet. |
| `fact` | `data_*` | | | Each a `data-` attribute. |
| `facts` | `items` |  |  | Figures (`Kpi`) to draw as its facts, which makes it one tag with no end. |
| `facts` | `label` |  |  | What the whole is, for a reader, when nothing on the page heads it. |
| `facts` | `layout` | `"list"` | `list`, `band`, `inline` | A list, each label beside its value; a band, the few facts that sum up a page, each a cell; a line (`inline`), a card's summary, each fact its value then its words. |
| `facts` | `size` | `"regular"` | `small`, `regular`, `large`, `lead` | How large a band sets its values: small figures read across, status facts, the headline figures a page is read for, or headline figures led by the first. |
| `facts` | `data_*` | | | Each a `data-` attribute. |
| `filter_bar` | `action` |  |  | The page it asks, when that is another. |
| `filter_bar` | `autofocus` |  |  | The search takes focus. |
| `filter_bar` | `clear` |  |  | Where Clear filters leads: the page with nothing narrowed. |
| `filter_bar` | `css` |  |  | The classes of where it stands. |
| `filter_bar` | `filtered` |  |  | Something narrows the list, so Clear filters shows. |
| `filter_bar` | `held` |  |  | What the page holds back, each counted. |
| `filter_bar` | `id` |  |  | Its name, where a page draws two whose choices send the same names. |
| `filter_bar` | `keep` |  |  | The query's other fields, each sent again as it stands. |
| `filter_bar` | `more` |  |  | The page that shows what it holds back. |
| `filter_bar` | `query` |  |  | The search's value. |
| `filter_bar` | `search` |  |  | The words the search is named by and shows empty; it is sent as `q`. |
| `filter_bar` | `shown` | `None` |  | How many it shows, of `total`. |
| `filter_bar` | `total` | `None` |  | How many there are. |
| `filter_bar` | `verb` |  |  | The words of the button that sends it, Filter unless told. |
| `filter_bar` | `data_*` | | | Each a `data-` attribute. |
| `muted` | `css` |  |  | The classes of where it stands (`narrow-more`, `row-more`, `cell-part`). |
| `muted` | `element` | `"span"` | `span`, `p`, `div` | A line among other words, a paragraph or a block. |
| `muted` | `hidden` |  |  | Drawn hidden, for a line a script shows. |
| `muted` | `id` |  |  | Its `id`. |
| `muted` | `role` |  |  | Its `role`, for a line a script shows and a reader hears. |
| `muted` | `tip` |  |  | What a press or a hover shows, as a status's tip is. |
| `muted` | `title` |  |  | Its `title`. |
| `muted` | `data_*` | | | Each a `data-` attribute. |
| `notice` | `css` |  |  | The classes of where it stands. |
| `notice` | `title` |  |  | Its first line, in bold. |
| `notice` | `tone` |  | `info`, `success`, `warning`, `error` | Its tone, none for a neutral notice; an error is an alert. |
| `notice` | `data_*` | | | Each a `data-` attribute. |
| `outside_link` | `css` |  |  | The classes of where it stands. |
| `outside_link` | `entity` |  |  | The kind of thing it names, as `data-entity`. |
| `outside_link` | `label` |  |  | The name a reader hears, for a link whose markup says none. |
| `outside_link` | `modal` |  |  | The dialog it opens where script runs, the page being its fallback. |
| `outside_link` | `tip` |  |  | What a press or a hover shows, in place of the page it opens. |
| `outside_link` | `title` |  |  | Its `title`, which otherwise names the page it opens. |
| `outside_link` | `tone` |  |  | A status tone its words are drawn in. |
| `outside_link` | `url` |  |  | Where it leads; anything but a web address draws what it holds, unlinked. |
| `present` | `value`, first and unnamed | required |  | The value whose markup it holds. |
| `prose_list` | `numbered` |  |  | Each numbered, for items read in order. |
| `reference` | `row`, first and unnamed | required |  | The record that holds the reference. |
| `reference` | `field`, second and unnamed | required |  | The field it is held in. |
| `referenced_by` | `thing`, first and unnamed | required |  | A record, or the kind of thing it is. |
| `referenced_by` | `identity` |  |  | Given a kind, what its page is addressed by. |
| `row_actions` | `actions` |  |  | What can be done to the row's record, each a `PageAction`. |
| `row_actions` | `gap` | `None` |  | A `WriteGap` standing in for every action the connection cannot make. |
| `row_actions` | `mark` | `None` |  | A `RowMark` the reader keeps on the row's record, first of its controls. |
| `row_actions` | `order` | `None` |  | A `RowOrder` that moves the row in an order the reader keeps. |
| `row_actions` | `put_away` | `None` |  | The reader's own way to set the row aside, last of its buttons. |
| `row_editor` | `form`, first and unnamed | required |  | The one form the view declares for the table's editors. |
| `row_editor` | `key`, second and unnamed | required |  | The row's key, which its ids and the address that opens it carry. |
| `row_editor` | `action` | required |  | Where the editor posts. |
| `row_editor` | `label` | required |  | The words of the way in. |
| `row_editor` | `verb` | required |  | Its button's words. |
| `row_editor` | `**initial` |  |  | The row's values, each by its field's name. |
| `row_group` | `label`, first and unnamed | required |  | The group's name. |
| `row_group` | `attention` | `False` |  | A group a reader should weigh before the rest. |
| `row_group` | `count` | `None` |  | How many rows it holds. |
| `row_group` | `folds` |  |  | The row's id, for a group that starts folded away. |
| `row_group` | `href` |  |  | The page its rows are dealt with on together, which its name links to. |
| `section` | `heading`, first and unnamed |  |  | Plain words; with none, the moment it names is the heading. |
| `section` | `count` | `None` |  | How many things it holds, beside the heading. |
| `section` | `css` |  |  | The classes of where it stands. |
| `section` | `edge` |  | `serious`, `attention`, `neutral`, `good` | Its frame's starting edge in a status tone, while it stands out from those beside it. |
| `section` | `figure` | `False` |  | Drawn as a figure, its head the caption. |
| `section` | `fold` | `False` |  | Drawn closed, its heading the toggle. |
| `section` | `head` |  | `above`, `inside` | Where its head stands; left empty, the stylesheet decides. |
| `section` | `id` |  |  | Its own `id`, for a link to it or a region refreshed in place. |
| `section` | `lede` |  |  | A line under the heading. |
| `section` | `moment` | `None` |  | A moment the heading names, drawn after its words. |
| `section` | `more_label` |  |  | The label of the aside's link to the whole of what it shows part of. |
| `section` | `more_url` |  |  | Where that link leads, drawn only when there is somewhere to go. |
| `section` | `open` | `False` |  | A folded section drawn open. |
| `section` | `remedy_label` |  |  | The label of the link the empty line offers. |
| `section` | `remedy_url` |  |  | Where the empty line's link leads. |
| `section` | `when` |  |  | The `when` form the moment is drawn in. |
| `section` | `data_*` | | | Each a `data-` attribute. |
| `status` | `*parts`, first and unnamed |  |  | Its words, several being one label, only the first sentence-cased. |
| `status` | `chip` | `False` |  | A neutral chip for a name, never a state. |
| `status` | `css` |  |  | The classes of where it stands. |
| `status` | `dot` | `False` |  | Its words after a steady dot in its tone. |
| `status` | `href` |  |  | Where it leads. |
| `status` | `icon` |  |  | Its words after an icon in its tone, beside `pill=False`. |
| `status` | `ink` | `"tone"` | `tone`, `context` | Whose ink its words take beside a dot or an icon: its tone's or the text's. |
| `status` | `pill` | `True` |  | A pill; with `False`, its words in its tone. |
| `status` | `tip` |  |  | What a press or a hover shows. |
| `status` | `title` |  |  | Its `title`. |
| `status` | `tone` |  |  | Its tone, or a subject's state in its own words and tone. |
| `status` | `**data` |  |  | Each a `data-` attribute. |
| `table_heading` | `label`, first and unnamed | required |  | The column's name. |
| `table_heading` | `css` |  |  | The column's classes. |
| `table_heading` | `descending` |  |  | Its reverse, when that is not `-field`. |
| `table_heading` | `drop` | `0` |  | The rank it leaves by when the table is too wide, 1 first. |
| `table_heading` | `gutter` | `False` |  | Its cells stand in the gutter and hold each row's toggle. |
| `table_heading` | `hidden` | `False` |  | The name for assistive technology only. |
| `table_heading` | `kind` |  |  | What the column holds, one of `COLUMN_KINDS`; left out, its classes say. |
| `table_heading` | `local_sort` | `None` |  | Sorts the rows on the page; left out, it follows from what the column holds. |
| `table_heading` | `select_all` | `False` |  | The tick-box that selects every row. |
| `table_heading` | `sort` |  |  | The field a server-sorted list orders this column by. |
| `table_heading` | `title` |  |  | Its `title`. |
| `table_region` | `label`, first and unnamed | required |  | What the table holds, the name its box is a stop by. |
| `table_region` | `action_label` |  |  | That link's label. |
| `table_region` | `action_url` |  |  | With no rows, where the link that fills it leads. |
| `table_region` | `chart` | `False` |  | A chart's table, read whole in the chart's order and scrolled. |
| `table_region` | `clear` |  |  | The way back from that search. |
| `table_region` | `compare` | `False` |  | Two values side by side in each row, after the row's name. |
| `table_region` | `css` |  |  | The classes of where it stands. |
| `table_region` | `empty` |  |  | With no rows, what is missing. |
| `table_region` | `filtered` | `False` |  | Its own search narrows it, so no rows says nothing matches. |
| `table_region` | `pane` | `False` |  | A box of its own height, which scrolls. |
| `table_region` | `remedy` | `None` |  | With no rows, what would fill it: a `label` and a `url`. |
| `words` | `capability`, first and unnamed | required |  | The command, by name. |
| `words` | `variant` |  |  | Which of its words, where it has more than one. |

Host filters: `|breakable[:arg]`, `|counted:arg`, `|measured:arg`, `|money[:arg]`, `|or_empty`, `|outside`, `|readout`, `|record[:arg]`, `|span`, `|web_url`, `|when[:arg]`, `|when_text[:arg]`. Django's own tags and filters, and `{% load %}` of `static` or the extension's own libraries.

Classes: `actions-col`, `address-list`, `allocation`, `allocation-legend`, `cell-data`, `cell-part`, `cell-whole`, `code-list`, `compact`, `compact-table`, `count`, `danger`, `data-table`, `date-col`, `domain-charts`, `errors`, `eyebrow`, `finding`, `form`, `ghost`, `help`, `icon-link`, `inline`, `key-col`, `keyboard-only`, `narrow-more`, `narrow-only`, `nowrap`, `one-line`, `optional-col`, `page-lede`, `primary`, `prose`, `row-actions`, `row-below`, `row-empty`, `row-gutter`, `row-less`, `row-main`, `row-meta`, `row-more`, `save-bar`, `section-flow`, `section-foot`, `split`, `stacked`, `subtle-link`, `table-clamp`, `table-scroll`, `tight-col`, `two-col`, `visually-hidden`, `whole`.

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
