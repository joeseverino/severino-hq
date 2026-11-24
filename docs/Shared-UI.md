# Shared UI

What an extension's templates may use of the host's: the frames and their
blocks, the public partials and what each reads, the host tags and filters,
and the classes and `data-` hooks. Then each part's rules, the host's own
parts' too, as its `Part` declares them (`rules`). Written by `manage.py
shared_ui_reference` from the host's templates
(`hq.platform.core.template_contract`); a test fails when this file falls
behind. The rules around it are in [Plugins](Plugins.md#shared-ui-contract),
and those that hold across parts in [Design](Design.md).

| Frame | Blocks a page fills | What it is |
| --- | --- | --- |
| `confirm_page.html` | `consequences` | A destructive action's confirmation: `consequences` says what will happen. |
| `form_page.html` | nothing | A page that is one form, a record's: its card and `partials/_form.html`, closing with `form_verb` and `cancel_url`; `form_fields` names a template for fields beyond the form's own loop and `form_note` says what saving does, each from the view, so the page fills no block. |
| `list_page.html` | `summary` | A `ListPageMixin` list: toolbar, the table its columns declare, empty state and pagination. `summary` sits above the table and is redrawn with it when a search or filter changes. |
| `page.html` | `page`, `print_footer` | Any page: the head, actions, dialogs and section navigation, drawn from `page`. |

| Partial | Required | Optional | What it is |
| --- | --- | --- | --- |
| `partials/_activity_calendar.html` | `calendar` | `calendar_id` | One month of plan against execution, from `hq_sdk.ui.ActivityCalendar`. |
| `partials/_allocation.html` | `allocation` | `legend`, `stacked` | Parts of one whole as one bar, from `hq_sdk.ui.AllocationBar`. |
| `partials/_ask.html` | `ask` | `extra`, `heard` | A do-this-now control and how its work stands, from `hq_sdk.outbound.ask`. |
| `partials/_cadence.html` | `cadence` | nothing | Whether a thing happened, period by period, from `hq_sdk.ui.Cadence`. |
| `partials/_cadence_matrix.html` | `matrix` | `compact`, `label` | Several cadences over the same periods, from `hq_sdk.ui.CadenceMatrix`. |
| `partials/_chart.html` | `chart` | nothing | A measure over time from `hq_sdk.ui.line_chart`, or quantities that add up from `hq_sdk.ui.stacked_bar_chart`, or a spatial path from `hq_sdk.ui.PathChart`. |
| `partials/_chart_tabs.html` | `charts`, `group` | nothing | Up to four charts of one thing in the space of one. |
| `partials/_document_picture.html` | `alt`, `src` | nothing | A picture of a document (a scan, a photo of a receipt), held in a page's shape before it arrives. |
| `partials/_dropzone.html` | `field` | `hint`, `title` | A file field that also accepts a drop. |
| `partials/_empty_state.html` | `message` | `absent`, `action_label`, `action_url`, `inline`, `remedy`, `why` | The one empty state: what is missing, then what would fill it. |
| `partials/_form.html` | `bar`, `cancel_url`, `choice_field`, `choices`, `form`, `note`, `verb` | `action`, `fields`, `gap`, `hooks`, `in_place`, `method`, `next`, `table_body` | A form that writes: its token, its errors, its fields or its table, and its closing row. |
| `partials/_form_actions.html` | `choice_field`, `verb` | `bar`, `cancel_url`, `choices`, `gap`, `in_place`, `live`, `note` | A form's closing row: its one verb, Cancel, and what saving does. |
| `partials/_form_errors.html` | nothing | `form` | What was wrong with a submitted form as a whole, above its fields. |
| `partials/_icon.html` | `name` | nothing | One of the host's icons, by `name`, the height of the text beside it. |
| `partials/_identity.html` | `name`, `tip`, `tone`, `url` | `about`, `badge`, `handle`, `picture` | An account's picture, name and handle. |
| `partials/_insight_grid.html` | `items` | `label`, `level` | Readings that need interpreting, each with its next step. |
| `partials/_job_progress.html` | `job` | nothing | A running job and what it is doing. |
| `partials/_meter.html` | `label`, `max`, `value` | `marks` | A proportion as a bar, or as a segment for each of what it counts. |
| `partials/_reading_age.html` | nothing | `at`, `reading`, `unread` | When a reading was taken, `at` the moment, in the one wording every page uses; `unread` why there is none. |
| `partials/_record_list.html` | `rows` | `empty`, `label`, `whole_rows` | A compact list of records, from `hq_sdk.ui.ListRow`. |
| `partials/_row_field.html` | `field` | nothing | One field of a form whose fields stand one to a row of a table, in its row. |
| `partials/_share_list.html` | `rows` | nothing | Ranked shares, each a bar against the largest. |
| `partials/_timeline.html` | nothing | `timeline` | Chronological linked events, from `hq_sdk.ui.Timeline`. |
| `partials/_toggle_set.html` | `legend`, `set` | `name`, `toggles` | A set of toggles that filter what a view shows. |
| `partials/_week_plan.html` | `plan` | nothing | Which days of the week a thing is scheduled for, from `hq_sdk.ui.WeekPlan`. |
| `partials/_work_queue.html` | nothing | `empty`, `fold`, `glance`, `grouped`, `here`, `items`, `triage` | What waits on the operator, each row what wants doing and why, or that nothing does. |

| Tag | Later parts | What it is | Written |
| --- | --- | --- | --- |
| `{% arrow %}` | nothing | The arrow that ends a link onward, written straight after its last word. | `<a href="/example/">All of them{% arrow %}</a>` |
| `{% button %}` | nothing | Every control that looks like a button: a link, a post through the page's one shared form, its form's own, or one a script acts on. | `{% button restart.label offer=restart look="primary" %}` |
| `{% button_row %}` | nothing | A row of buttons that stands on its own: a form's closing row, a card's decisions, a page's way on. | `{% button_row %}{% button "Save" submit=True look="primary" %}{% endbutton_row %}` |
| `{% card %}` | nothing | The frame a page's content is grouped in; a titled part of a page is a `{% section %}`. | `{% card label="Hosts" %}<p>Body</p>{% endcard %}` |
| `{% cell_line %}` | `detail` | A fact in a table cell as a line of its own, and the words said quietly on the line under it: given `text=` it encloses nothing, its words a link to `url=`; a line of markup is its block, its second line after `{% detail %}`. A second line that is a status, code or a link is a `{% list %}` item. | `{% cell_line %}{% entity "machine"\|entity_of:"example-host-01" %}{% detail %}Docker host{% endcell_line %}` |
| `{% data_table %}` | nothing | A table drawn whole from its declaration: its columns' headings and a row of cells for each record. | `{% data_table table %}` |
| `{% dot %}` | nothing | The middle dot between two parts of one line, written straight after the first. | `{{ count }}{% dot %}{{ age }}` |
| `{% empty_value %}` | nothing | The mark for a value that is not there, where there is no value to test; `\|or_empty` tests one. | `{% empty_value %}` |
| `{% entity %}` | nothing | One thing named, as the link builder answered for it: a link to its page, or its name. | `{% entity "machine"\|entity_of:name %}` |
| `{% fact %}` | `detail`, `heading`, `panel`, `foot` | One fact of a `{% facts %}`: its label, its value and the detail under it. Given `value=` it encloses nothing; a value in markup is its block, its detail after `{% detail %}`, and a panel its value opens in place after `{% heading %}`, `{% panel %}` and `{% foot %}`. | `{% facts %}{% fact "Version" icon="layers" %}<code>1.4.2</code>{% detail %}Tag only{% endfact %}{% endfacts %}` |
| `{% facts %}` | nothing | Every label and its value a page shows, each a `{% fact %}`; given `items=` it draws each item as a fact and encloses nothing. | `{% facts layout="band" %}{% fact "Running" value="3" url="/example/" %}{% endfacts %}` |
| `{% filter_bar %}` | `choices`, `summary` | Every query a page asks of what it lists, in one bar: its search and any controls of its own, its `choices` part, the one button that sends them, then its `summary` part. A list's table toolbar is the same bar. | `{% filter_bar search="Filter by name" query=q filtered=narrowed clear=every_url %}{% choices %}{% select "kind" kinds label="Kind" every="kind" %}{% endfilter_bar %}` |
| `{% layout %}` | nothing | A run of a page's sections, laid out as its kind says. | `{% layout %}{% section "Example identity" %}<p>An example line.</p>{% endsection %}{% section "Example link" %}<p>Another example line.</p>{% endsection %}{% endlayout %}` |
| `{% list %}` | nothing | A list, each `<li>` an item, read as its kind says. | `{% list %}<li>{% status "High" tone="serious" %}</li><li>An example advisory</li>{% endlist %}{% list kind="dots" %}<li><a href="/example/">example-host</a></li><li>2 days ago</li>{% endlist %}{% list kind="prose" %}<li>An example sentence.</li><li>Another example sentence.</li>{% endlist %}{% list kind="chips" %}<li><a href="/example/">Example page</a></li><li><a href="/example/2/">Another example page</a></li>{% endlist %}{% list kind="codes" %}<li><code>example:read</code></li><li><code>example:write</code></li>{% endlist %}{% list kind="labeled" %}<li><span>Example network</span> <code>192.0.2.10</code></li><li><span>Example tailnet</span> <code>192.0.2.20</code></li>{% endlist %}` |
| `{% muted %}` | nothing | Words said quietly beside what they are about: a context line, a sentence under a value. A cell or a list item holds it; a run of parts said quietly is `{% list kind="dots" muted=True %}`. | `{% muted element="p" %}A sentence under it.{% endmuted %}` |
| `{% notice %}` | `title` | Every banner and message a page shows, in one of its tones; a message among the page's messages is the same part in a toast's placement. A title the page composes is its `{% title %}` part, after the body. | `{% notice tone="warning" title="2 jobs failed" %}<p>What to do.</p>{% endnotice %}` |
| `{% outside_link %}` | nothing | A link to a page outside HQ around markup of the page's own (a chip, an icon, a card); a name is `{% entity url\|outside label=name %}`. | `{% outside_link url="https://example.com/docs/" %}<span class="tag">Docs</span>{% endoutside_link %}` |
| `{% present %}` | nothing | What it encloses, drawn only when the value is there, else the mark for a missing value. | `{% present repo.release %}<code>{{ repo.release }}</code>{% endpresent %}` |
| `{% reference %}` | nothing | The link for the reference a record holds, for `{% entity %}`, or none the viewer may not see. | `{% reference expense "paid_from" as account %}{% entity account %}` |
| `{% referenced_by %}` | nothing | Everything that names a thing, for that thing's page, a card per kind of reference. | `{% referenced_by "machine" machine.name %}` |
| `{% row_editor %}` | nothing | The way into a row's editor, and the editor itself under the row where the page's address opens it. | `{% row_editor form row.pk label="Edit" action=row.edit_url verb="Save" %}` |
| `{% section %}` | `lead`, `aside`, `empty`, `foot` | A titled part of a page in its frame, its head on the page and its body in the frame. | `{% section "Agents" count=n %}<p>Body</p>{% aside %}A line.{% empty %}None yet.{% endsection %}` |
| `{% status %}` | nothing | One short state, drawn in its tone and said in sentence case, the one way a page draws a state. | `{% status "Blocked" tone="attention" %}` |
| `{% with_query %}` | nothing | An address with a query added to its own, each value encoded: written where it stands, or kept with `as` for the part that links it. | `{% with_query "/example/" kind="host" as hosts_url %}{% button "Hosts" hosts_url %}` |
| `{% words %}` | nothing | What a button for a command says, from its declaration, for prose that names the one to press. | `Press {% words "agent.access.set" %}.` |
| `{% write_token %}` | nothing | What every write form carries in place of `{% csrf_token %}`, so the form sent twice acts once. | `<form method="post">{% write_token %}...</form>` |

| Tag | Option | Default | Choices | What it does |
| --- | --- | --- | --- | --- |
| `button` | `label`, first and unnamed | required |  | Its words. |
| `button` | `arrow` | `False` |  | Ends with the arrow of a link onward. |
| `button` | `checked` | `None` |  | A switch, its track drawn after its words, and which way it stands. |
| `button` | `count` | `None` |  | A count of what it holds, after its words. |
| `button` | `css` |  |  | Classes in place of its look, for a control not drawn as a button. |
| `button` | `current` | `False` |  | It names the page being drawn. |
| `button` | `disabled` | `False` |  | Neither a link nor a form, kept in the tab order. |
| `button` | `expanded` | `None` |  | Shows or hides what it governs, and which it does now. |
| `button` | `heard` |  |  | Its name aloud, where its words are a glyph or none and it has no title. |
| `button` | `hit` | `False` |  | Drawn smaller than a target, so it asks for its hit area. |
| `button` | `icon` |  |  | The icon its words open with. |
| `button` | `look` |  |  | How it looks, its words from `ui.BUTTON_LOOKS`. |
| `button` | `modal` |  |  | The dialog a link opens where script runs. |
| `button` | `name` |  |  | The field its form's own sends. |
| `button` | `offer` | `None` |  | A `PageAction` that posts, which it sends: where, its field and the rest. |
| `button` | `outside` | `False` |  | Its url is a page outside HQ, opened as every outside link is. |
| `button` | `pressed` | `None` |  | A toggle, and which way it stands. |
| `button` | `submit` | `False` |  | Sends its own form. |
| `button` | `title` |  |  | Its `title`, read where it stands. |
| `button` | `url` |  |  | Where it leads. |
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
| `cell_line` | `detail` |  |  | The words of the line under it, said quietly. |
| `cell_line` | `lines` | `2` | `1`, `2` | The lines its first line may take: one keeps its row as tall as the rest. |
| `cell_line` | `text` |  |  | Its words, which make it one tag with no end. |
| `cell_line` | `url` |  |  | The page its words link to. |
| `data_table` | `table`, first and unnamed | required |  | A `DataTable`: its columns and its rows, or its groups of rows. |
| `data_table` | `empty` |  |  | With no rows, what is missing. |
| `entity` | `link`, first and unnamed | required |  | The thing's link, as the link builder answered for it or a record's read writes it. |
| `entity` | `code` | `False` |  | Its name drawn as code. |
| `entity` | `label` |  |  | The words it is named by, in place of its own. |
| `entity` | `mark` |  |  | The punctuation after the name, held to it, or a list's `forloop`. |
| `fact` | `label`, first and unnamed | required |  | Its label. |
| `fact` | `cut` |  |  | A line longer than its cell, the detail's first, cut at its end and read whole from its tip. |
| `fact` | `cut_whole` |  |  | What the cut line's tip reads, where that is more than the line; the line by default. |
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
| `layout` | `aside` | `False` | `False`, `True` | Drawn as an `<aside>`, a split's companion. |
| `layout` | `id` |  |  | The layout's own id, for a region refreshed in place. |
| `layout` | `kind` | `"flow"` | `flow`, `charts`, `pair`, `split` | `flow`, as many to a row as keep each at least 22rem wide: the sections that close a page, a run of cards; `charts`, the flow at the width a chart keeps its scale, 30rem: a domain's charts; `pair`, two columns at most, like beside like: a table beside a table, a chart beside a chart; `split`, a main and its companion, sized to what it holds, sharing a row while both keep a readable width. |
| `layout` | `data_*` | | | Each a `data-` attribute. |
| `list` | `below` |  |  | The open row's label, for a `down` list the open row shows under it. |
| `list` | `kind` | `"down"` | `down`, `dots`, `prose`, `chips`, `codes`, `labeled` | `down`, read down, each item a line of its own: the notes under a row's name, a record's statuses; `dots`, a run on one line, a dot between each that no line opens on: a fact's links, a row's context line; `prose`, sentences, each marked as the browser marks it; `chips`, places to go, each item one link drawn as a chip of its own; `codes`, names in code, wrapping between them: policy names, ports, permissions; `labeled`, each item a label then its value, the labels in one column: addresses. |
| `list` | `muted` | `False` | `False`, `True` | A `dots` run said quietly. |
| `list` | `numbered` | `False` | `False`, `True` | `prose` items read in order, each numbered. |
| `list` | `data_*` | | | Each a `data-` attribute. |
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
| `reference` | `row`, first and unnamed | required |  | The record that holds the reference. |
| `reference` | `field`, second and unnamed | required |  | The field it is held in. |
| `referenced_by` | `thing`, first and unnamed | required |  | A record, or the kind of thing it is. |
| `referenced_by` | `identity` |  |  | Given a kind, what its page is addressed by. |
| `row_editor` | `form`, first and unnamed | required |  | The one form the view declares for the table's editors. |
| `row_editor` | `key`, second and unnamed | required |  | The row's key, which its ids and the address that opens it carry. |
| `row_editor` | `action` | required |  | Where the editor posts. |
| `row_editor` | `label` | required |  | The words of the way in. |
| `row_editor` | `verb` | required |  | Its button's words. |
| `row_editor` | `**initial` |  |  | The row's values, each by its field's name. |
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
| `status` | `shown` | `True` |  | Its words shown; with `False`, its icon alone, the words said aloud. |
| `status` | `tip` |  |  | What a press or a hover shows. |
| `status` | `title` |  |  | Its `title`. |
| `status` | `tone` |  |  | Its tone, or a subject's state in its own words and tone. |
| `status` | `**data` |  |  | Each a `data-` attribute. |
| `with_query` | `url`, first and unnamed | required |  | The address, which may hold a query of its own. |
| `with_query` | `**query` |  |  | Each name=value added to it. |
| `words` | `capability`, first and unnamed | required |  | The command, by name. |
| `words` | `variant` |  |  | Which of its words, where it has more than one. |

Host filters: `|breakable[:arg]`, `|counted:arg`, `|measured:arg`, `|money[:arg]`, `|or_empty`, `|outside`, `|queue_entries`, `|readout`, `|record[:arg]`, `|span`, `|web_url`, `|when[:arg]`, `|when_text[:arg]`. Django's own tags and filters, and `{% load %}` of `static` or the extension's own libraries.

Classes: `actions-col`, `allocation`, `cell-part`, `cell-whole`, `compact`, `count`, `danger`, `date-col`, `eyebrow`, `form`, `ghost`, `glyph`, `help`, `inline`, `key-col`, `keyboard-only`, `narrow-more`, `narrow-only`, `nowrap`, `one-line`, `optional-col`, `page-lede`, `primary`, `prose`, `row-actions`, `row-below`, `row-empty`, `row-gutter`, `row-main`, `row-meta`, `row-more`, `save-bar`, `section-foot`, `subtle-link`, `swatch`, `swatch-key`, `table-scroll`, `tone-attention`, `tone-good`, `tone-neutral`, `tone-serious`, `visually-hidden`, `whole`, `wide-only`.

Script hooks: `data-sort`, `data-submit-busy`, `data-submit-label`, `data-submit-status`, `data-tip`. Styled attributes: `data-chart`.

| Shape | Drawn by hand | Write instead |
| --- | --- | --- |
| `fold` | A fold drawn by hand, where the section part draws it. | `{% section "Heading" fold=True %}`body`{% endsection %}` |
| `heading` | A heading drawn by hand, where the section part or the page frame draws it. | the heading of `{% section "Heading" %}`, a sub-head a `{% section %}` inside it; the title is the frame's |
| `line-break` | A line broken by hand with `<br>`, where the cell line or the stack list part draws the lines. | `{% cell_line %}`first line`{% detail %}`second line`{% endcell_line %}`, or `{% list %}` for lines read down |
| `section` | A `<section>` element drawn by hand, where the section part draws it. | `{% section "Heading" %}`body`{% endsection %}`, or a `<div>` for one that only groups a layout |
| `icon` | An icon drawn by hand, where the icon part draws it. | `{% include "partials/_icon.html" with name="..." %}`; a drawing that says something is role="img" |
| `form` | A form that writes drawn by hand, where the form part draws it. | `{% include "partials/_form.html" %}`, or `partials/_page_action.html` of a PageAction.command |
| `filter-bar` | A search field or select drawn by hand, where the filter bar and select parts draw it. | `{% filter_bar search="..." %}``{% choices %}``{% select ... %}``{% endfilter_bar %}` |
| `button` | A button drawn by hand, where the button tag draws every control that looks like one. | `{% button %}`, which takes a popover, a dialog command and each data_* hook |
| `chip` | A chip, a badge, a count or an age drawn by hand, where the status part or a value filter draws it. | `{% status word chip=True %}`, a count by {{ n\|counted:"thing" }}, an age by {{ at\|ago }} |
| `filter-choice` | A choice of several drawn as boxes by hand, where the toggle set part draws it. | `{% include "partials/_toggle_set.html" %}`, or `{% select %}` for one choice |
| `form-control` | A form, a field or a label drawn by hand, where the form, field and filter bar parts draw it. | `{% include "partials/_form.html" %}` of a Django form, or `{% filter_bar %}` for what a list keeps |
| `list` | A list or a band of facts drawn by hand, where the list or the facts part draws it. | `{% list kind="down\|dots\|prose\|chips\|codes\|labeled" %}``<li>`...`</li>``{% endlist %}`, or `{% facts %}` |
| `record-link` | A link to a record's page drawn by hand, where the entity tag names the record by its model's page. | `{% entity row\|record label=row.name %}`, or row\|record:"section" for a part of its page |
| `run-state` | How work stands is drawn by hand, where the ask or the status part draws it. | an Ask (`partials/_ask.html`), or `{% status run.word tone=run.tone data_ask_label=True %}` |
| `empty-said-quietly` | An empty line drawn by hand, where the empty state part draws it. | `{% include "partials/_empty_state.html" with message="..." %}`, or a `{% section %}`'s `{% empty %}` |
| `missing-value` | A missing value's mark drawn by an if, where the value's own tag or filter draws it. | {{ x\|or_empty }}, a value filter alone for a figure or a moment, or `{% present x %}` |
| `query` | A query added to an address by hand, where the with_query tag adds and encodes it. | `{% with_query url name=value as named_url %}`, or `{% with_query url name=value %}` where it stands |
| `split-count` | A count split from its words, where the counted filter writes them as one. | {{ n\|counted:"thing" }}, the figure and its words as one, or `{% fact "Label" value=n %}` |
| `split-figure` | A figure apart from its word, where a value filter writes them as one. | {{ n\|counted:"thing" }} for a count, or {{ x\|measured:"mi" }} for a unit, the figure and its word as one |
| `stretched-shape` | A round shape or text inside a stretched drawing, which distorts it. | a dot as a zero-length line of class chart-dot, and a label in the unscaled svg.chart-labels layer |
| `cell` | A table's cell drawn by hand, where a declared table draws each cell and every mark it reads. | `{% data_table table %}` of a DataTable its view declares |
| `muted` | Muted words drawn by hand, where the muted tag draws them. | `{% muted %}`...`{% endmuted %}`, or `{% muted element="p" css="..." %}` for a paragraph |
| `typed-mark` | A dot or an arrow typed as text, which a reader hears and a line may strand. | `{% dot %}` between two parts, a .dot-list for a run of parts, `{% arrow %}` after a link |
| `figure-in-chip` | A figure inside a chip, which is a word. | the figure in a band of facts, a numeric column or a sentence |
| `counted-fold` | A list folded at a count, which hides parts its line has room for. | a Cell(fit=True) of the parts, which shows as many as the line holds |
| `cut-count` | A count cut off from the words it counts. | `<h2>`Words `<span class="count">`N`</span>``</h2>`, or {{ n\|counted:"thing" }} |
| `since` | A "since" written before a moment, which reads "since 3 days ago" beside an age. | {{ at\|when:"since" }}, which says its own "since" |
| `plural` | A word chosen by a count of one, which the count's own filter agrees. | {{ n\|counted:"one,many" }}, or {{ n\|agreed:"one,many" }} for the words alone |
| `code-block` | A code block a keyboard cannot reach, though it scrolls in its own box. | `<pre tabindex="0">` |
| `control-in-summary` | A control inside a summary, which is the control that opens its disclosure. | the link or button in the disclosure's body |
| `nested-form` | A form opened inside a form, whose opening tag the parser drops, so the inner form's end closes the outer one. | one form, or the second after the first closes |
| `open-comment` | A comment left open on its line, which Django prints on the page. | {# #} closed on its line, or `{% comment %}` for a longer note |
| `page-title` | A page title that is empty, or names the site, which the layout appends after it. | `{% block title %}` naming the page alone |
| `dollar-amount` | A dollar sign before a value, which writes a negative amount as "$-5.00". | {{ amount\|money }} |
| `stranded-mark` | A mark written after a value's tag or branch, which has no room left on its line, so it starts the next alone. | the mark straight after the value ({{ host }}.), or through its hold: \|breakable:forloop for a list's commas, `{% entity link mark="," %}`; a run of code chips takes no commas |
| `command-url` | A command's address built by hand, which encodes its target as the form does not read it. | the link command_url builds (action_links) |

## Each part's rules

### ask_button (host only)

An ask's button is drawn by `partials/_ask.html` alone, around its note. While its work is live it
is `aria-disabled`, which keeps its focus, and its title says what the work said, where a cell
shows only its word.

### button

Primary for the one thing a page is for; default for the rest; `ghost` for link-like actions that
sit inline; `danger` in red text on the same surface as its neighbors, loud only in its
confirmation; `compact` for one that stands in a row, a list or a head, a line tall; `glyph` for a
square that holds a glyph or an `icon` alone (a row's order and mark, a page's links on a queue
row with `ghost`), its words heard and unseen; `link` for one among words, drawn as a link; `menu`
for a row of a menu; `bar` for a control of the site header's bar, on the bar's ground. `link` and
`menu` draw no box, so `button_classes` drops a look that paints one beside them, and a
destructive row keeps only its red.

Every control that looks like a button is `{% button label url look="primary compact" %}`, the
one writer of `.btn` (`ui.button_classes` for Python's parts): a link, with `outside=True` one out
of HQ drawn as `{% outside_link %}` draws it, with an `offer` one that posts, with `submit=True`
its form's own, and with neither and no url one a script acts on (`data_*`, `popovertarget`,
`command`). Its words may open with an `icon`, close with a `count` or end with an `arrow`; one with
a glyph or no words is named aloud by its `heard`, else its title. A `<button>` may be a toggle
(`pressed`), a switch drawn with its track (`checked`) or a disclosure (`expanded`). A post
is only ever an offer's: a `PageAction` from `PageAction.command`, which is None for a reader who
may not run the command, or a `RowOrder`'s or `RowMark`'s press, so a page has no way to draw a
press its reader would be refused. `_page_action.html` draws one; a confirmation's is
`PageAction.command(..., confirms=True)`, loud where it destroys. A toggle that posts is a button
with `post` and `pressed`; the calendar's sources are a list of them, each in its series color
(`partials/_calendar_sources.html`).

The push button is `.btn`, and a `<button>` with no class of its own; a button with a class (a
menu row, a toggle, a close mark) starts from nothing painted (`base.css`) and draws itself, so no
rule takes the push button's look back off. A label keeps its line, from the button rule alone
(`page.css`); only a tight table lets one wrap. A label too long for a phone is shortened where it
is declared. A control drawn smaller than a target asks for its area with `hit=True`. *Gate:*
`test_a_controls_label_keeps_its_line_from_the_button_rule_alone`, and the
`action-intent-names-no-value` ast-grep rule.

### button_row

A row of buttons that stands on its own (a confirmation's, a card's decisions, a page's way on) is
`{% button_row %}`, which a form's closing row draws through too. A sentence beside its buttons
is `{% muted %}`, and a folded section in it (`{% section ... fold=True %}`) is one of its
items while closed, opening onto a line of its own the row's whole width.

### card

`{% card css="example" heading="example-heading" %}...{% endcard %}` frames what it encloses. It
draws a section unless `element=` names an article, figure, details or div. `heading` is its
heading's id, or `label` its name when it has no heading, and `busy` marks it while its content
is still coming. `alone` makes it the whole of a page that stands outside the page frame (the
sign-in): centered at a dialog's width, a picture leading it over its own title, and an action
alone on its line the card's width. Each `data_*` option is a
`data-` attribute (True draws it bare). A state of the card is a `data_*` option the stylesheet
reads, never a class written inside an `{% if %}`. No template writes `class="card"` itself
(`test_only_the_tag_draws_a_card`). A card is never wider than the column or track it is placed in
(`.card { min-inline-size: 0 }`), so on a phone what it holds wraps inside it. A card holding only
a table region (a thing's problems, the dashboard's short queue) has no inset at its sides, so the
table's rows reach them as a section's do (`:has()` in surfaces.css decides; a page picks
nothing).

A band draws no box of its own: a box is the card around it (`{% card element="div" %}`). A card
holding the band alone is its frame and nothing more: its facts keep their own padding and draw
their edges as every band does. A card holding nothing but a line of facts is a strip of counts,
with a narrower inset and its words a size under its values.

### cell_line

A fact in a cell with a quiet line under it (a name over its kind, a change over its risk) is `{%
cell_line %}`. Its words alone are `{% cell_line text=... url=... %}`. Markup is its block, its
second line after `{% detail %}`: `{% cell_line %}{% entity link %}{% detail %}Docker host{%
endcell_line %}`. The part draws that line on its own, so no page writes a break or styles a line
of its own. No template writes a `<br>`: the `line-break` shape refuses one, in an extension too.

### change_table (host only)

A change's before and after is `{% change_table preview %}`: a section named by its label. Each
value that moves stands under Field, Before and After, each row's kind of change under Status,
or under Field and Value where nothing stood before. A redacted value is said only to have changed, and a document
that would not parse is shown as its line diff. A comparison of many records of one kind (the
adopted playbooks, from `approval_review.grouped`) names each field a record moves by the
record and the field ("Empty a cache › Access"), says a record that comes or goes whole on one
row (its name and its summary, its fields folded under it), and names the ones that stay on one
line under the table. A record a held request's warning names has its rows marked for attention
(`data-attention` on the `tr`), and the card does not say the warning again. The rows keep the
change's own order. A preview whose rows are part of a larger change (an app update's pins, its files on GitHub) names
the whole as `more_label` and `more_url`, the section's quiet link. An empty preview draws nothing. An approval card and an audit event draw it
(*Gate:* the `change-table` shape of `hand_drawn.SHAPES`).

### check_ladder (host only)

**Checks read down a ladder.** `{% check_ladder %}` draws checks one after another, each a `{%
check_rung %}`. A rung holds its tick for how it stands (`state`: met, unmet, or unavailable when
not read), its `kind`, the statement with the `evidence` it was read from, and a `more` part
saying what opening it shows. Its `body` part is what it opens to. `route=True` draws the rungs as
the stops of a route, the connection panel's path to HQ, and a ladder inside a rung's body draws
its checks as rows of it.

**A route reads in one direction.** A route is one surface, each stop on one line with its mark
on the line; a stop no check reads draws what it is (its rung's `icon`), untoned, in place of the
tick. It runs across its box, one equal column a stop, while no stop is open and each has room,
and down it otherwise; it never wraps onto a second row. Its line runs from the first stop's mark
to the last one's, with any stop opened. A check names what it was read from right after its
statement, or under it where the line is full, and that evidence is shown at every width.
*Gates:* the survey's `evidence apart` and `summaries hiding`.

### command (host only)

Text to paste somewhere else is `{% command %}`, its Copy button in the code's corner. A command
is `{% command text=... %}`, one tag with no end. A block of code (a container's compose change)
is `{% command id=... %}` holding its code, then `{% title %}` (where it goes, over the code), `{%
detail %}` (the words under it) and `{% notes %}` (`<li>`s, drawn as a stack list), in that order.
The code wraps rather than scrolls, so a long path stays on screen; with no code the notes stand
alone.

### control_label (host only)

A control's visible name is `{% control_label "From" control="id" %}`, the `<label>` of the
control with that id; with no `control` it names what follows.

### control_plane/_connection_ability.html (host only)

A part of a connection's row, given the `state` and its `connection`: the ability's name, and its
permission note in its tone where it differs from its neighbors or cannot be used.

### control_plane/_connection_read.html (host only)

A part of a connection's Reads, given the `read` and its `connection`: its name, quiet where its
credential cannot use it, then on the row's open line its state word in its tone where it was not
read cleanly, its record count where it was, and why it was not read.

### control_plane/_credential_fix.html (host only)

**A credential's fix is one notice.** The permissions it lacks, then the steps of the one fix
`application.credential_mint` approves, a warning notice a connection's row opens to; the row's
own line says what is missing.

### core/_policy_setting.html (host only)

A rule is read and changed where it sits, in its agent's column of the rules table
(`capability_policy.table`): a select in the rule's tone for whoever may save the rules, else the
chosen words in that tone, and past the agent's grant the reason, muted, on one line.

### core/_response_check.html (host only)

A response header or policy directive is read from a real response once the Headers fold opens
(connection.js), never from the settings meant to produce it. Until then its state says it is
being read; the script shows the state drawn beside it, Sent or Enforced, or Absent.

### data_table

**A table fits its box, at every width.** No screen width is asked anywhere in the table
primitive, and nothing is measured: a table is a grid of its columns' tracks, each row a subgrid
of them, the same with script and without, so every cell stands under its column's heading and a
column is one width down the table. No width is computed in Python. Its columns are sized as
Primer's DataTable sizes its own (`auto` beside `grow`): each column starts at the least its cells
can wrap to, every column grows alike toward its values whole, and the room left past every
column's values goes to the column that names the rows, which is `auto` where every other is
`fit-content(100%)` (tables.css). The grid has a track for each of `table_layout.TRACKS` columns
after a row's tick-box, a row's controls among them, and a table that declares more is refused,
so no column is drawn over another. A value that keeps its line (a status, a figure, a row's
controls, a date) starts whole. A short value (`cell-short`, `table_layout.SHORT`: an address and
its port, a host, a mailbox, a project's name) starts whole up to its column's share, its region's
width over one more than the count of columns it shows (`--share`, over the count the region
writes, `data-columns`: its headings, or in a table with none its widest row; a column with
nothing in any row, one ranked to leave at the region's width, or a `wide-only` one in a narrow
box, is not counted), so the columns together never start wider than the region. A longer value
starts at its longest word or piece, so it gives way first: a short value is whole wherever the
table has room for it, and wraps (an identifier at its seams, words at their spaces) only where
the table has none. A long value is held to its third line (below). A heading is one of its
column's values, held as a short value is. *Gate:* the survey's `room unshared`, which fails a
cell or a heading squeezed below what it needs while another column holds the room it lacks.

The region turns a table's ranks (`TableColumn(drop=)`) into at most four levels from the widest
(`data-drop`, `table_layout`): its first rank leaves while the region is narrower than 64rem, its
next below 56rem, then 48rem and 40rem, by four container-query rules written once in tables.css,
and any rank past the fourth leaves with it. So a narrow box holds only the columns a row keeps.
Nothing is measured, so a column may leave a box that could still hold it. What leaves shows on
the row's open line. The gates fail a table that runs past its card.

A column its headings rank none for leaves by the rule `leaving_order` follows, after every
declared rank and only at the narrowest widths: its far column at the widest of them and its
nearest below 40rem, so a table keeps every column until a narrow box and a hand-written table
needs no rank of its own to fit a phone. A column holding a button stands,
since a cell held back holds none. This is the column-priority pattern of jQuery Mobile's and
Tablesaw's column toggle (six priorities, each shown from a set width, the columns with none
persisting) rather than their reflow mode, which stacks each cell under its label: at every width
a table is a table, its naming column and the columns it keeps side by side under their headings.
Below 36rem the table is in a narrow box (below).

What is marked `narrow-only` shows in a narrow box alone: a few words in the cell that names the
row, standing in for the columns that left (the audit log's action under its name). What is
marked `wide-only` leaves a narrow box outright, held for no toggle, since what it says is on a
page of its own.

A table of marks (`data-marks` on the table: one row a thing, one column a period, a mark in each
cell, as the cadence matrix is) centers every heading but the names over its column, and its
cells stand closer in a narrow box and where it shares a row with a chart
(`data-marks="compact"`). Its older periods are `wide-only`, so a narrow box keeps the newest
three.

The region marks each body cell that says nothing (`data-blank`) and every cell of a column blank
in every row (`data-blank-column`), which is not drawn. Each body cell carries its column's kind
(`data-kind`), so a figure aligns under its heading on fixed digit widths and a value is cut by
its kind, with nothing written on the cell by hand.

Since a table fits, its region is no scroll box and its heading pins under the page's chrome (a
scroll box in either axis would pin it to the region). Only a region declared a pane, or a
chart's (`chart=True`), scrolls, and pins its heading to its own edge. A region that scrolls takes
focus, named by its table's label so the stop says what it holds, as the focusable scroll box of
the usual responsive-table pattern is; a region that fits is a plain box. The stop is a `group`
where that pattern writes `region`, so it is no landmark: the section or card around a table is
one already, often by the same name. *Gates:*
`test_a_region_that_scrolls_is_a_stop_named_for_its_table`,
`test_a_section_and_the_table_in_it_are_one_landmark`. A pane (`pane=True`) is as tall as its rows
up to a fixed reading height, as the map's ledger is, and scrolls past it. A table is never
swiped through: in a narrow box its ranked columns have left. A column of words every row must
show, such as what a change sets (its After; its Before is spare and leaves first), is a
`key-col`, which takes no rank and never leaves, nor does the closing figure of a table that marks
none: a table whose kept columns cannot stand at a phone's width declares too many keys.

A key/value table (row headers, no `thead`) sizes its label column to its longest label.

Every row is ruled from the next, a row group's heading included; only the table's last row and a
row whose open line follows it draw no rule, since the card's edge or the open line ends them. A
row's open line holds what left the row: its list of columns (`.row-columns`) is drawn only while
one of its entries is, so a row with nothing gone opens to its controls alone and no empty box.
*Gates:* the survey's `rows unruled` and `empty panels`.

**A narrow box keeps its rows under their headings.** Below 36rem of its region (the
`table-region` container query in tables.css) every column a row can spare has left for the row's
open line, and what stands is the naming column, each `key-col` (or the closing figure), a row's
state (its pills, a date, or any column under a heading of kind `status` that declares no rank
in a table that ranks its others) and its controls, each under its heading. A column whose rows
each say a moment is a date (`date-col`, which `DataTable` writes), never ranked to leave unless
declared. A column of a row's choices (a field it fills in in each row, or the words standing
where one cannot be, as the agent rules draw a column per agent) stands each choice on a line of
its own beneath the row's name, named by its column (`data-field`), so the row keeps every one
however many columns there are. A heading of kind `status` that declares a
rank is one of a row's secondary checks: it leaves at that rank, whatever its cells hold, and no
later than the table's last ranked column, so a row's own state is declared with no rank and its
checks with one. In a narrow box a pill is as wide as its words up to its column's share, wraps
between them past it, and is never narrower than its longest word, so a row's name is whole before
its state is. Below every rank's width (20rem) the state goes to the open line last; a key never
does. A hash or key with no break in it may break anywhere (cuts.css).

A table's headings, cells, open lines and group headings keep their roles in its grid (table,
row, column header, cell), and its heading pins. A narrow table whose rows
hold labeled controls (below 48rem where a row holds four, which ask a line of their own sooner)
gives them a line of their own: a row's labeled controls take its last line, on
a line of their own from where its name starts, in every row alike, the row's pad under them,
wrapping only between whole controls. A row of icon controls (a star, the order arrows) keeps them
at its end on its first line. *Gates:* the survey's `tables` (each cell a row shows stands under
its column's heading, and no row draws a column's label), `rows unruled` (a row's bottom edge is
ruled, whichever cell draws it), `overlaps` (what two boxes draw never crosses) and `row state
kept` (a key or a state never leaves early, and a field its row fills in is never hidden).

**Opening a row moves no column.** An open row keeps its cells as they were, but for a clamp,
which it releases in place, and, in a narrow box, its `narrow-more` parts, which it shows in
place, each on a line of its own that asks its column for no width. What it holds back stands on
the line drawn after it (`tr.row-detail`), whose content asks its columns for no width: the blocks
the server moves there, then an entry for each cell that holds something back, under its column's
name. So every column keeps its edges and every other row its height. *Gate:*
`test_opening_a_row_moves_no_column_and_no_other_row`.

A fold that opens adds rows, whose controls a column shared down the table would widen for every
row. So a table of one column with no headings, whose rows each say one thing (the queue), may
size each row's controls apart (`DataTable(controls_per_row=True)`): in a box of 48rem or more
each row is a grid of its words and its own controls at its end, and opening a fold moves no other
row's words or controls. *Gate:* `test_opening_a_fold_moves_no_other_rows_controls`.

The server writes each cell once. A cell cannot also stand on a line beneath its row, so
tables.js copies what the row holds back onto that line as the row first opens (or as it arrives
drawn open), from the one frame the region draws (`template[data-open-line]`): each cell of a
ranked column, shown where its column has left (`data-drop`), and each cell holding a
part only the open line shows (`row-more`, `line-rest`), shown always, what the row's line already
shows of it only once its column has left (`data-row-shows`). A copy draws no id twice. A cell
held back holds no control, which a copy would draw twice: the region refuses one, so a button
stands in place or moves whole (`row-below`). A row's open line a script adds is
`{% open_line %}`. *Gates:*
`test_table_once.py`: `test_a_row_writes_each_cell_once`, `test_a_cell_held_back_holds_no_control`,
and the byte budgets `ROW_BYTES` and `CHROME_BYTES`.

- A `row-below` block, which the row's line never shows, and the row's editor are moved into a
  line the server draws after the row (`tr.row-detail`, `drawn_tables`), shown while the
  row is open: the blocks first, then each labeled block under its label. A row holding a block
  says so on its `tr` (`data-below`), which shows its toggle. A block's stacked parts are spaced
  by their list's gap, with the cell's normal pad after the last part.
- A table whose line is the rest of what a row says and does (`data-line="flat"` on the table)
  draws it flat: one column on no panel, the buttons of its parts side by side, and on a phone
  each the line's width, one to a line.
- The browser's find does not reach into a closed row: what it holds back is not drawn until the
  row opens.
- A part that reads differently on the row's line and on the line beneath asks where it stands
  (`@container style(--cell: line)`, or `open`), which the table primitive sets on its cells, and
  never names the table: an ask's note says its word alone on the row's line and its sentence
  beneath.

A row's opening is its only disclosure. What is shown only once a row is open is marked
`.row-more` (a word, a list item), or `.row-below` for a block, which opens on the line beneath
the row at the table's width. Never a `<details>` inside a cell.

**A table is laid out before any script runs.** Nothing is measured to lay a table out:
`tables.js` only sorts, selects and opens rows, and loads deferred (*Gate:*
`test_the_table_script_lays_nothing_out`). No table is drawn by hand, an extension's included:
a table is a `DataTable` its view declares, and the `cell` shape of `hand_drawn.SHAPES` refuses
a `<td>` or a `<th>` in any template of the host or an extension but the table's own partials,
as it loads. *Gate:* the `cell` shape.

A long value in a cell (a line past `table_layout.LONG`) wraps, and in a narrow box is held to its
third line by a clamp with its mark (`.cell-clamp`, cuts.css), its whole value where its row
opens; a wide box holds it whole. The name a row is read by is never held, nor is a row with no
toggle (a table with no headings, a fold's head), so their values wrap whole, and no cell carries
its value again as a title. Only words are held: each part of words in it (`data-cell`) is clamped
on its own, so a pill or a toggle beside them stays whole, and a cell holding such parts is the
clamp itself, with no box drawn around them. A value of words of 32 characters at most
(`table_layout.SHORT`: an address and its port, a host, a project's name) is one box (`cell-short`,
the value's own list where it is one) as wide as the value up to its column's share, and as wide
as its column where that is more, so its column holds it whole while the table is laid out as
one, and past its share it wraps inside its box. Its column still asks for the value whole
(`calc-size` holds only the share as its floor), so room the table has past every column's needs
goes to it; a longer one wraps at its words in a line of `--row-words`, or its share where that is
less, as a long one does; a longer identifier and a row's own name break anywhere their column
lacks room; a pill and prose break only inside a word wider than their column, a pill longer than
its column wrapping inside its one box. A status, a figure, a date or a control is never cut: its
column is as wide as it is.

An identifier the row is read by, in a row of many columns (a container's image), is
`code.cell-whole`, which the clamp never holds. A value told in several parts marks each
`cell-part`.

What each part of a cell is decides how it wraps, and the region marks it (`data-cell`,
`table_layout`). The stylesheet keys its cuts, holds and wraps on that mark, never on a
page's class or a cell's tags. The bold a cell leads with, or its leading link's, is the row's
`name`; free text (a message, a record's words) is declared `Cell(role="data")` and marked `data`;
what a row means in words is `Cell(role="prose")`. A context line is a muted cell. A table declared
later sets as every other does by declaring those, and `lint:css` refuses a table primitive rule
that tells a cell's part by anything else. Free text breaks a run with no seam in it (a link, a
digest) wherever it must, so it never holds its column wider than its box. An identifier in a
prose column waits for the open row (`row-more`) rather than holding that column at its cap.

A run of links longer than the count its template declares folds behind "+N", and a stacked list
longer than four shows three and "+N more". A row that holds anything back gets one toggle that
lifts every cut and opens every fold in it.

An open row and the line beneath it hold every box in them to its cell, in one rule of the table
primitive (`surfaces.css`). No box asks for a width of its own or grows wider than what holds it,
and a chip is a box, its padding inside that width. So nothing there runs past or over a cell at
any width, whatever a page's own rule says, and no page sizes a box inside a table. `lint:css`
refuses a width, an inline size, a flex basis or a grid's columns on a selector that reaches
inside one (`.data-table` and a combinator, a cell, `.row-detail`, `.row-columns`). Only the table
primitive's sheets may set them (`base.css`, `cuts.css`, `figures.css`, `tables.css`,
`surfaces.css`). *Gates:* `cells` and `cell labels` in the layout survey, read with rows open by
`dev:look --expand`; `lint:css`.

An intrinsic size (`max-content`, `min-content`) goes on an element inside a cell, never on the
`td`. Chromium ignores it on a table cell and WebKit does not, so the two engines would size the
column differently.

An identifier short enough to hold its line is never broken while every column stands; once
columns leave for the open line, where it is drawn whole, its seams are where it breaks. The row's
own name, free text, a preview and a context line wrap as they always do. *Gate:* `broken
identifiers` in the layout survey,
`test_an_identifier_in_a_table_is_whole_while_a_column_can_leave`.

A box set in a line that cuts what it holds (an inline block whose overflow is not visible) takes
its bottom edge as its baseline, so its words ride above the line's. So it is set by its top
(`vertical-align: top`).

A pill is as wide as its words wherever it stands, in a cell alone or beside words, and is one
box: a pill longer than its column wraps inside its own paint, never into two pills. *Gates:* the
survey's `pills in pieces`, `a_table_fits_its_box_at_every_width`,
`test_a_blank_column_is_not_drawn_and_a_short_word_is_not_split`, the survey's `sideways`, and the
page crawl, which fails any `.data-table` outside `.table-scroll`.

A region whose tables hold no row draws the one empty state (`partials/_empty_state.html`) in their
place, so no page draws headings over nothing. Its `empty=` says what is missing in the page's
words, with `remedy=` (or `action_url=` and `action_label=`). `filtered=` with `clear=` says its
filter emptied it, with the way back. A table whose rows open an editor (`Cell(editor=)`) is a
region of the page's own (`data-fragment`, its id from its label), so an editor opens, saves and
closes in place.

### django/forms/field.html (host only)

A field is the view's Django form field, drawn by this template, so its label, help, errors and
required mark come with it; one posted with a button outside the form names that form in its
widget's `form` attribute.

### django/forms/widgets/choice_cards.html (host only)

**A choice that has a consequence says it.** A field whose few answers each change something
different declares `ChoiceCards(described=...)` (`hq/platform/application/forms.py`). Each answer
is a card of its radio, with its few words as the radio's name and what choosing it does as its
description. The whole card is pressable. It draws through the form's own loop, so no page writes
its markup. *Catalog:* `form/choice-cards`.

### django/forms/widgets/suffixed.html (host only)

A number with a unit declares `SuffixedInput(suffix=...)`, the unit beside the box. It draws
through the form's own loop, so no page writes its markup.

### dot

A dot between two parts of a line is `{% dot %}` and a link's onward arrow `{% arrow %}`, each
written straight after its part. A no-break space joins the mark to that part and a reader never
hears it, so no line starts with a mark or holds one alone. A run of whole parts is `{% list
kind="dots" %}`; any other mark is the text of an `aria-hidden` element. Text built in Python
joins its parts with `dotted(...)` (`hq_sdk.ui`), which holds each dot to the part before it, and
`breakable` draws that dot as this tag does; markup built in Python joins with
`dotted_html(...)`. *Gate:* `test_template_separators`, which follows every branch and include
before each held mark, over the host's templates and an extension's.

### fact

Every label and its value is a fact: `{% fact "Label" value=n url=... detail=... %}` inside `{%
facts %}`, or the block form `{% fact "Label" %}markup{% detail %}lines{% endfact %}` for a value
that is markup. A zero and a linked count stand alike. A `Kpi` or a card that counts things
takes a `Counted` as its label, which words it to agree with its figure ("1 Draft", "2 Drafts").
A fact is drawn as a figure is: its label
with its mark before it, its value large under it, its detail under that. What a figure counts may
follow it in a `<small>` ("9 containers"), in the label's voice. A link in a value is the value's
ink, underlined only under the pointer and the focus, its fact tinted, so only a toned fact's value
is colored. A fact whose one target is its value's link is that link whole, its label and detail
pressed as its value is; one holding another target keeps each its own. Given `{% heading %}`,
`{% panel %}` and `{% foot %}` parts and an `id`, its value is the button that opens the panel
in place, as a menu does, and each `data_*` option is its attribute, so a fragment region in the
band holds facts refreshed in place as its own (`core/_dashboard_glance.html`).

A fact's detail is running words. Its lines are the ones its markup declares: a `<small>` each,
or a `detail=` given as a list. What a fact holds past its lines (its record list, a disclosure
of what runs now) follows them in the detail, so a page's summary cards are a band of facts too,
never a grid of its own. A line naming something longer than its cell (an image's path) is its
`cut`, the detail's first line, held to one line and cut at its end, so its cell is no taller than
its neighbors, and its tip reads it whole (`cut_whole`, where that is more than the line). A fact drawn by hand (a page's own band) writes the part's classes, which
no page may, and a figure set bold apart from the words that count it is refused by the
`split-count` shape of `hand_drawn.SHAPES`.

**A figure may be drawn as well as said.** `Kpi` (and `{% fact %}`) takes one `drawing`, or none,
drawn beside its value or under its detail:

- `Dots`, a part of a whole, one dot each. `Dots.of(filled, whole)` when the parts are only a
  count; `Dots((Dot(...), ...))` when each is something with a page of its own, which then names
  itself when pointed at and links there, beside the figure's own link on its value. Past six,
  dots stand in even rows at most four wide. Where the column has no room for them beside the
  value they take the line under it, so a figure's dots never reach the figure beside it (the
  survey's `into a neighbor`). Past a dozen, dots are a texture and are not drawn. A history is
  `Dots(..., ordered=True)`, drawn in its own order, oldest first, as the HQ page draws a piece of
  scheduled work's recent runs, each linking to its job. `partials/_dots.html` draws either,
  wherever it stands.
- `Trend`, recent readings oldest first, drawn as a line where the card has room, each reading
  saying what it is when pointed at.
- `AllocationBar`, the parts of a whole, drawn under the figure and its line as a bar with its
  legend set one under another (`partials/_allocation.html`), each part linking where it has a
  page. In a card the line and the parts share the line's row, so the figures beside it keep their
  lines level.

and an `icon`, a mark from `partials/_icon.html` before the label, on figures only: headings
carry none. A drawing repeats what the value and its note already say, so the words stay, and one
that is only decoration is not announced. Nothing is drawn against a measure the domain does not
have: a date that runs out shows the date, not a bar across an invented span.

A figure that is itself a change takes `tone`, a status tone its value is drawn in, at any size,
so a rise and a fall never read alike. A figure that is a level says how it moved in its line,
which takes `detail_tone` the same way. `detail_url` links the line where it counts things with a
page of their own ("12 expenses"). It needs a `detail`, since a link is said by its words.

### facts

A band of facts (`layout="band"`) is every run of small labeled figures: a page's status facts,
its headline figures, a forecast's hours, the dashboard's glance. It is the few facts that sum up
a page or a part, each a cell with its label and mark over its value over its detail. Its cells
are sized by what they hold: a wrapping row where each cell starts at its own widest line, a
row's cells share the room left over, and a cell takes the next row only when the band has no
room for it. So a label wraps only in a band too narrow for it on one line. A band holding an
identifier (a version, a tag) counts its rows on a floor as wide as one, so a short version
stands whole on its line (*Gate:* `split identifiers`). Each cell is its own
stack, top-aligned, its value right under its label, no taller than what it holds; a ragged line
across cells is better than a gap inside one.

A band draws no box of its own: a box is the card around it, so a band outside a card draws none.
Anywhere but a card holding it alone (a section's body, a card holding more, the page's ground)
the facts stand apart by a gap (`--band-gap`) with no edges (`--band-rules`), their words at the
content's edge.

`size` sets the values: `small`, an even grid of small centered figures read across, as a
forecast's hours are, a note under one marked; `regular`, status facts; `large`, the headline
figures a page is read for; `lead`, headline figures led by the first, which stands larger and on
a row of its own where the band has no room for it beside them. Given `items` (each a `Kpi`), the
band draws each as its fact. Under 24rem a band reads as a list, each fact a row of its own; small
figures keep their grid, and headline figures (`large`, `lead`) and a band beside a page's title
their counted rows, two to a row (a phone's head: label beside value, over a note whose run wraps
whole parts to the lines it needs). Headline figures stand on a wider floor, so three take a row
only once the band is about 360px wide, each value sized to its cell, never under the band's.

**A band's values are one size.** A domain's overview is a band of facts at `size="large"`, every
figure the same size, in its contributor's order. Values, context and links render once from the
`Kpi` collection. Overview cards fit their available width, placing three domains side by side
when space permits; empty contributors take no space.

A band of facts beside a page's title (the dashboard's glance) stands on the page's ground: beside
the title while its facts fit there, and on a line of its own under it, from the page's edge,
where not. Fewer than four readings take a column each as wide as four would, so two stand
together rather than at the far ends of the head. *Gate:* `readings apart` in the survey.

A list, the default, is the facts about one thing, each label beside its value. It lays as many
pairs to a row as keep each a short label and a short value wide, so a few short facts fill a
wide frame rather than stacking down its start. The pairs of a row share what it leaves, so a
row's last pair spans to its end and no pair stands alone beside empty columns; every label is
one width, so the values of a column line up. A value that is a run of several (a `labeled` or
`codes` list, addresses) asks for its widest line, sharing a row while it fits and taking the
row when it does not. Narrower than two pairs it is one pair to a row. A
label with several values holds them as a list inside one fact. An identifier in a value (code,
a `|breakable` name) runs inline, code a chip on each line it wraps to, so a value's first line
stands on its label's line in every engine. A list opened to read more (in a disclosure, a
popover, a dialog) or set above what a form changes keeps one pair to a row, sits closer and
ends with what holds it. A table row's small print keeps one pair to a row too. Where it stands
decides this, never an option, so the same summary drawn on a page and in a dialog takes each
one's spacing. *Gate:* `rows of facts short` in the survey.

A line (`layout="inline"`) is a card's summary, the counts across a domain or the whole of HQ:
each fact one phrase, its bold value then its words ("3 rules"), agreeing with it, then a dot
and its detail ("1 Draft · 22 published"), a fixed gap apart, wrapping fact by fact, so a wide
card holds them on one line. Under the card's head it takes a rule above.

**A frame is filled.** A list of facts lays its pairs across its frame. A card of only a pair or
two shares a row with the sections beside it, so its values never end a third of the way across
the page. A record page's facts are a `{% card %}` that opens its `{% layout %}`, beside its
notes and related records. In a flow, a frame of facts grows into its row as every member does.

### filter_bar

**One filter bar.** `{% filter_bar filtered=... clear=... shown=... total=... %}` draws the page's
controls in one order: the search, then its selects, then its toggles. The search is the bar's
own: `search=` the words it is named by and shows empty, `query=` what was asked, sent as `q`, and
`autofocus=True` where it is the page's first act. Then come one Filter (or the bar's own `verb=`
for a bar that asks a question rather than narrows a list), Clear filters back to `clear` when
anything is set, and the summary at the row's end. A bar with a search puts what follows it after
`{% choices %}`: those choices, with the button that sends them and Clear, stand on the search's
row while it holds them all, and otherwise fold behind one Filters control, a popover panel under
the bar that sends without a script. That button reads Apply unless the bar names its own verb, so
Filter never stands beside Filters. The control counts the choices that narrow (a select off its
"Any", a table's filter with a box ticked, a set of toggles with one off, a ticked toggle), so a
folded bar never hides that its list is narrowed. The panel's id is drawn from the names its
choices send, and from the bar's `id=` where a page draws two bars sending the same names, as a
page of maps does.

The fold is the layout's own measure, never a width written for one page: the bar's sizer, an
inert copy of the row unfolded, takes the room the row needs, and the choices fold exactly when
what is left is none, which is where the row would otherwise wrap. The bar asks beside its summary
only the room of the search and the Filters control, so the summary shares the row where it fits,
as the queue's count does at a phone. The panel is drawn as the header's sections are: inline
while there is room, a popover once there is none (base.css). A bar with no search, or nothing
after it, keeps its controls in a box of their own, the button beside their last line, so it
never takes a line alone.

The summary is the part's words: "`shown` of `total` shown." Then comes what the page holds back
without being asked (`held=`, each part counted, as the map's main lanes hold back its context),
and the one link that shows it (`more=`). Last are the page's own facts about what shows (`{%
summary %}`, "1 needs a look."), never a count of its own. A page passes counts and links, never
the sentence. A list's table toolbar is the same bar, its count its pager's. A state no control
holds is the bar's `keep=`, the query's other fields as the view hands them, each sent again while
it has a value. The search takes the line's room; every other control is as wide as what it says,
a select its longest option. A control the script shows only once it has something to act on is
drawn `hidden` in the bar and stays so. *Gate:* the `filter-bar` shape refuses
`.filter-bar`, a search field and a select written by hand, outside the parts that draw them.
`test_filter_tags.FilterBarUseTests` reads every `{% filter_bar %}` in the templates. It refuses a
count written in the summary and a narrowing bar with no count.

### headed (host only)

A part that draws its own title (a dialog's, an insight's) wraps what it holds in `{% headed %}`, so
a section in it heads itself a level under the one a section where the part stands would take.
`{% headed top %}` is a body under a title at the top level wherever the part stands (a dialog's),
so a dialog opened from inside a section still heads its own sections a level under its title.

### history_table (host only)

Every table of audit events is this part, its rows the ones the audit log draws (`history.table`,
drawn by `{% data_table %}`): the audit log, an event's other events and a record's recent
changes. On a phone only What and When stay, so What's second line says how it came out. Each
group (`history.HistoryGroup`) is a `tbody` under
its heading row where it has a label, and a group that starts folded away (`folds`) marks each of
its rows as folded under its heading. An event that changed things heads them: its gutter holds the
fold rather than the row's own toggle. A fold carries at most `history.SHOWN` of what the event
changed, and a last row counts the others and links to the list of them all. Its headings are
`history.COLUMNS`, without who did each where `hide_who` is set.

### layout

A run of sibling sections or cards (the sections that close a thing's page, the connection panel's
identity, link and route, a page's run of cards) is one `{% layout %}`, the flow. It lays as many
to a row as keep each at least `--flow-basis` wide (22rem), and one to a row where that is the whole
width. A row's last ones share what it leaves, and each stands on a row as tall as the tallest, a
section's foot at its frame's bottom. Every member grows into what its row leaves and none caps its
width, so every row ends at the flow's end. It is measured against the box it sits in, so the same
sections pair in a dialog and on a page. A part that needs more room before it shares a row sets a
wider basis on itself (the connection panel's route); a run of charts is `kind="charts"`, the flow
at the width a chart keeps its scale. A wide one (a table of four columns or more, a fold, anything
that is not a section or card) goes across the row.

The flow paints its members in the order the markup holds them, so what is read aloud and where the
keyboard goes is what is drawn (WCAG 1.3.2, 2.4.3): nothing reorders them. A page therefore writes
its wide ones ahead of its short ones, which collect after them to share rows rather than one
standing beside the gap a wide one leaves. Registry sections say which they are
(`ServiceSection.spans_row`), and `partials/_page_sections.html` draws one kind per pass,
`spans_row=True` ahead of a page's short sections and `False` among them. *Gate:* `flows in reading
order` in the layout survey.

Content with a narrower companion is `kind="split"`: the two share a row while both keep a readable
width and wrap to a column when they cannot. A month as the main takes the room, its companion
keeping its basis beside it; a run of sections in a companion is a flow with `aside=True`, so wrapped
under the main they pair up. A pair (`kind="pair"`) is two columns at most and pairs like with like:
a table beside a table, a chart beside a chart, so each row ends together.

Inside a layout the gap is the separation, so a member's own stacking margin is dead space and a
member is as narrow as its track. No page writes a layout's class or an auto-fit grid of its own:
the classes are this part's, and `lint:css` refuses `repeat(auto-fit` and `repeat(auto-fill` outside
the parts that lay out something else inside themselves. A layout with nothing in it draws nothing.

### line_list (host only)

A run of short parts in a cell (a project's technologies, what a connection reads or reaches) is
`{% line_list values as value %}…{% endline_list %}`, with `kind=` one of the list's kinds where
the body draws each `li`. No count decides how many show. Its first three size the column and
wrap with it. The rest ask the column for nothing and show as many as the line they end has room
for, each whole, and the row opens only when one is cut. A list folded at a fixed number behind
`row-more` is refused (`test_template_line_lists`).

### list

Each item is an `<li>` and nothing stands between them. A list with no class, or a kind's class
written by hand, is refused by the `list` shape. A list with no item draws nothing, so a page
writes each `<li>` under its own test.

A plain list read down (a row's notes, a record's statuses, a section's records) is `{% list %}`.
Its `<li>`s each run their parts inline a small gap apart, and an item that reads as a sentence is
marked `.prose`. In a table's cell its items are the cell's own lines: a status and the line under
it, links one to a line, a name's notes. They sit at the row's rhythm with no gap, and a line that
holds a link is a target tall. `below="Label"` makes a `down` list a block the open row shows
under that label.

A run of whole parts read across one line (a fact's links, a row's context line, a band's
details) is `{% list kind="dots" %}`, each part an `<li>`, with `muted=True` for one said quietly.
The stylesheet draws the dot in the gap before each part, so no template writes a separator and no
line opens on one. The facts a page states under its title (a record's state, a machine's
addresses) are `{% list kind="dots" muted=True %}`, written by the template its view names as
`page_aside`, where the stylesheet sets it small.

A row of places to go (the dashboard's links, a saved copy's own pages) is `{% list kind="chips"
%}`, each item an `<li>` holding one link, `{% entity url|outside label=... %}` for a page outside
HQ. Each link is a chip, a target by its frame and never underlined at rest, and an outside one's
arrow sits quieter than its name. A run of names inside a line stays a `dots` list.

### muted

Words in the muted voice that name nothing (a context line beside a value, a sentence under it)
are `{% muted %}...{% endmuted %}`, a line among other words, or a paragraph with `element="p"`;
`css` adds the classes of where it stands (`narrow-more`, `row-more`, `cell-part`). What names a
value (`.fact-label`, a tile's label, a `<dt>`) is the same voice, drawn by its own part.

### notice

`{% notice tone="warning" title="2 jobs failed" %}<p>What to do.</p>{% endnotice %}` is every
banner and message a page shows: a callout in its tone, for what needs saying before the content
it is about. `tone` is one of Django's message levels, so a message's `level_tag` is its tone as
it stands; with none it is neutral. An error is an `alert`, which a screen reader says at once,
and any other a `status`. `title` is its first line, in bold; a title the page composes (words
around a value, a loop) is the `{% title %}` part, written after the body (`{% notice
tone="warning" %}<p>Pick another.</p>{% title %}{{ name }} cannot be used{% endnotice %}`). `css`
and `data_*` are the page's, as a section's are. Among the page's messages (`<ul
class="messages">`, base.html) the same part is a toast, set closer; it is never a second part.
Drawn with no body it is the frame a script clones and fills: base.html holds one per tone in a
`<template data-notice>`. So a line a script adds (`fragment.js`, `demo.js`) is the part the
server draws, and no script writes a notice's class. An error page is a page of its own, never
this part.

### open_line (host only)

The line a row opens to that a script adds is `{% open_line %}value{% endopen_line %}`, the line
the region draws, so it is the same part: a frame the script clones for a row that comes to hold
something beneath it once drawn (ask.js), setting the label and width.

### page_main (host only)

base.html draws every page's content in it, and the component catalog draws each of its entries
in the same part, so an entry stands where a page's content does. It is the host's frame; an
extension's page is drawn in it and never writes it.

### partials/_approval_card.html (host only)

Its heading is what was asked, the aside when and how, and what it acts on, with the edge of its
tone while it waits. A warning no row of the change carries stands above the change. Its buttons
are the decisions its reader may take (`entry.offer`), and a decision withheld from them says why.

### partials/_ask.html

**Asked-for work is an ask.** A button that starts work which outlives the request is this part,
from an `Ask` (`hq/platform/application/asks.py`). It stands where a `PageAction` does, in a page
or panel head. In a body or panel its note takes a line of its own under the row its button stands
in, reserved before anything is said. In a page's head it takes no room: the head's reading line
("Last read …", `partials/_reading_age.html`) says it, over the words the server drew there, which
keep their room. Under way that is "Refreshing…" for any work; a failure's sentence is cut to the
line, whole in its title. The note is then heard and not seen, and once the work lands the head is
drawn again, its line saying the new reading's age. A head with no reading line says it on the
busy button alone.

Pressing it posts to a view that declares only what it asks (`asks.AskMixin`): the request runs
the work's capability, stores the ask and answers at once, a refusal as failed work in the
refusal's own sentence, and the control then says how the work stands. Every capability an ask
runs answers `asks.Asked` (its standing, where to follow it, and what asking did), so the view
reads the answer and words nothing of its own.

- The note beside the button is a live region (`role="status"`) that changes only when the state
  does. The elapsed time ticks next to it outside the region, so nothing is read out every second.
- While its own work is live the button is `aria-disabled`, not disabled: it keeps the focus the
  operator just gave it and ignores a second press.
- When the work ends, the part of the page named by the ask's `refresh` selector is fetched again
  and swapped in place; without one, the page's content (`#content`, a region of its own). Only if
  that cannot be had does the page load again, though never while the operator is using it and
  never twice running; then it offers the reading as a link.
- A failure is said in the note, where it was asked.

In a table's row the note's line is as wide as the button, so there it says the state's word alone
("Waiting", "Failed") and the row keeps its height. The sentence is the button's title, and the
line the row opens to says the word and the sentence under the button's label, so a reader with no
title to hover still reads why.

Each thing is said once. A sentence that only says the state's word again ("Waiting" and
"Waiting…") is dropped where every standing is made (`asks.Standing`), so the word stands alone,
in a cell and beside a head's button alike, and the row opens no line for it. A sentence earns its
place by saying what the word cannot: what the work waits on, or why it failed.

Without script the same button is a form post back to the page, which draws the control from what
is stored. So a page loaded while the work is live says so and resumes following it.

A job's panel (`_job_progress.html`) and the readings a page asks for when it opens
(`_visit_refresh.html`) are the same ask drawn differently; one behavior in `ask.js` follows all
three. *Gates:* `RequestNeverWaitsTests.test_asked_for_work_is_followed_by_one_script_behavior`;
`lint:ast-grep` (`ask-answered-by-ask-mixin.yml`);
`ComponentLayoutTests.test_a_rows_ask_says_its_sentence_on_the_line_its_row_opens_to`;
`hq/platform/core/tests/test_action_budgets.py`.

### partials/_calendar_sources.html (host only)

A `{% list %}` of the sources, each a menu row's `{% button %}` with `post` and `pressed`, its box
in its series color, or its swatch beside its name for a reader who may not choose.

### partials/_change.html (host only)

**A change is one primitive.** A value and the one it changes into (a running version and the
newer release, an installed package and the version that fixes it) is `{% include
"partials/_change.html" with from=… to=… %}`, with `code=True` for identifiers and `url` to link
the new value. Old value, arrow and new value share one baseline. A narrow line breaks only between
the two values, with the arrow kept beside the new one, and a reader hears "1.2.3 to 1.3.0" rather
than an arrow.

### partials/_chart.html

Every drawing's legend is `partials/_chart_legend.html`, given the drawing, which says its keys
(`legend_keys`, each a `drawings.LegendKey`): a chart's series, a path's ordered bands, a
calendar's kinds and its missed mark, a whole's parts with each figure, share and link.

Spatial drawings use the same chart frame as time series: `PathChart` supplies paths, markers, an
optional scale and its named bands, which its legend keys. The drawing makes no outside requests.

### partials/_connection_peering.html (host only)

**A connection is its two ends and the link between them.** It draws You, the link and HQ side by
side, the link in its network's color and the verdict's. It stacks the three in one column when
its own box is too narrow for them, wherever it is drawn. Its `#carriage` is a fact's value: which
network the link rides over, with a lookup of the address a link across the internet comes from.
The dialog's shell before the panel arrives (`partials/_connection_loading.html`) is the panel's
own box.

### partials/_empty_state.html

**Nothing empty takes space.** A panel with nothing to show is not drawn: the page closes up rather
than carrying a card that says "nothing here". An empty list says whether its own search or
filters emptied it, and offers the way back. A page with nothing yet says what is missing and,
where it can, what would fill it and where (`message`, `why`, an action); controls that only make
sense over data wait for the data. An absence where a value would stand (a fact, a cell, a row) is
the same part with `inline=True`: its words, quiet, in the line that holds them, with no frame.

The message is the fact alone, one sentence ("No playbooks adopted yet."), with no how-to after
it: the action and the page's controls say what to do. An absence every page shares (nothing
read yet) is `absent="no reading yet"`, said in the words `tone.Absent` declares, which the
`absent` filter also hands `{% status %}` and refuses any other wording for.

### partials/_error_page.html (host only)

`{% include "partials/_error_page.html" with code="404" title="Page not found" body="..."
search=user.is_authenticated only %}` is the whole of what 403, 404 and 500 show. It stands
outside the page frame and so titles itself: the status over the title, what happened and what to
do, and the one way back to the dashboard. `request_id` names the id the log line for the failure
carries, and `search` offers the search field.

### partials/_example_connection.html (host only)

**A saved copy never says "you" about another reader.** Anything that describes the reader's own
connection includes this part. On a live HQ it renders nothing; on a saved copy
(`SEVERINO_RENDERED_COPY`, set only in code by whatever renders the copy, as the demo does) it
marks the line as an example connection, not the reader's. *Gate:*
`hq/platform/core/tests/test_example_connection.py`, and `demo/check.py` on the built demo.

### partials/_form.html

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
`form/write-gap`. *Gate:* the `form` shape of `hand_drawn.SHAPES`, which holds every form that
writes to the form part, and `test_form_page.py`, which holds every routed create and edit view to
the frame and the card to it alone.

A form that asks one question is one visible field of a line (words, a number, a date, one
choice) closed by its verb alone. It draws its button on the field's line where the field keeps
18rem beside it, and under it where it cannot, as a search or lookup form does. The partial
derives it (`asks_one_question`, then `.form.is-question`), so no page flags it. *Catalog:*
`form/lookup`, and `form/lookup-help` with its help and an error.

A form whose fields stand one to a row of a table (a group chosen for each record, a verdict for
each request) is the same partial with `table_body` naming the template that draws the table, its
`{% data_table %}`. The form lays out no fields of its own, so it adds no box: the table reaches
its section's sides as a table section's does, and the closing row stands under it. The page draws
it inside the one section that heads what it saves, never with sections of its body standing
alone, so the closing row is framed with every field it sends. The form
declares one field for each row, named by the row (`group-<id>`), so each control's name and id
are its own. A row moved between two drawings still posts its own value, where a formset's names
follow each row's place. Each row's cell draws its field with `partials/_row_field.html`: the
control, its label for a screen reader alone (the column's heading names it on the page), and its
errors under it. A row's own buttons are posting `{% button %}`s, which post the page's own form,
so they sit in this one.

### partials/_identity.html

**An account is named by this part.** It draws its picture (a `data:` URI or an HQ path, never
another host's address) and its name, with a state beside it drawn by `{% status %}`. Its handle
leads to it, then a line in its own words. In a card followed by a band of counts (`{% facts
layout="band" %}`), the two share one line once the card holds the identity beside four counts
across, as the Starred page heads with your GitHub profile, and the band takes the line below when
narrower.

### partials/_linked_text.html (host only)

Every free text written by hand (a record's notes, a message sent through the contact form) is this
part, over the text's runs that `application.where_it_runs.named_links` finds: each name a link as
every mention is, and its line breaks kept.

### partials/_meter.html

**A proportion is a meter.** A share of a whole, or of the largest of several, is a `<meter>` of its
`value` against its `max`, which the browser draws from its value, so no template writes a width; a
share too small to round up still shows. Given `marks` (each with a `tone`, and a `group`), it is a
segment for each in its tone instead, a gap where the group changes, every segment one width, so a
column of meters lines each mark up under the same mark above. The segments are one control, its
`label` both what it says aloud and its tip.

### partials/_modal.html (host only)

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

### partials/_origin_panel.html (host only)

Given the item's decision (`application.decisions`), it is drawn after the line its urgency
stands on, never inside its words, and a Cmd- or Ctrl-click on the urgency opens it (app.js).

### partials/_page_action.html (host only)

A page's action is `{% include "partials/_page_action.html" with action=... %}`, and a run of an
item's actions (`ActionLink`s, or their serialized dicts) is `partials/_action_links.html` with
`actions=`, which leaves out a way that leads only to the view it is drawn on (`remedies`), so a box
around them asks it first and is drawn only around a control. `extra` adds looks: a head draws a
control twice, beside its title and in its menu, and both are the same control. `heard` marks the
copy beside the title, whose ask is heard there and seen on the head's reading line.

A disabled one, by any method, is never a link or a form: it stays in the tab order
(`aria-disabled`) so its reason, the title, is read where it stands. One that names the view it is
drawn on (a switch between views) is that view's state, `aria-current`, never a link to itself,
unless it opens a dialog: then it is the dialog's one way in. A POST one is the button's `offer`,
which sends what it carries.

### partials/_page_head.html (host only)

**One head.** Trail, title, controls, lede, laid out the same at every width. The controls stand
at the head's top right, beside the trail and the title together, so the trail's line is as tall
as its words, or beside the title alone where there is no trail (the title wraps before the
controls move). Every line after the title has the head's whole width: what the page says about
its thing and the lede never share a line with the controls. Two controls stand there at every
width, the lead and the next the page names, and the rest go behind the overflow menu. A head too narrow for three (a container query on
the head) folds the second in too. The lead is chosen by what each control is for, never by where
it stands: not a destructive one while another is offered, then one that can be pressed, then the
page's primary (`head_actions`). A destructive verb (a stop) is a link to its confirmation, which
names what it stops.

**A lede earns its line.** Most pages have none. A lede says what the page cannot show: a fact
that changes how you read it ("Lookups run from outside this network"), or the record's own
details. Never a table of contents of the page, a pointer to another page (link to it instead), or
how HQ works inside (a file path, a background mechanism). Never "What X may run: ...", which says
the heading again.

A thing's state is the line under its title (`page_aside`), as words, each of its parts (a
machine's addresses) moving to the next line whole. A page about a day (the
dashboard) names it with `page_dateline`, drawn over the title where a trail stands. A head with
an aside and a line of facts (`page_facts`) stacks its words in a column of their own: the trail
or day, the title, the facts. The aside stands beside the whole column, as the dashboard's
readings stand beside its greeting and what needs you. The facts about it at a glance are a band
under the head, as a service, a machine and HQ's own page open.

### partials/_page_sections.html (host only)

A band on a detail page (`ServiceSection`) is drawn by this part, never by a page's own markup, so a
page grows a band because a resolver produced one: a function and a registry entry, not an edit to
a template. Its table is `{% data_table %}`; a band of audit events (`history`) is
`{% history_table %}`; each readout a fold under it. A band whose rows are the latest of more links
to the rest from its heading (`more_url`), and a folded one's heading is its toggle, its count
saying what is behind it. A page draws the bands that take a flow's whole row (`spans_row=True`)
ahead of its short ones (`spans_row=False`), so its flow paints in the order it reads.

### partials/_problems.html (host only)

**Only what is urgent is framed.** A thing's page draws the problems about it from this part. An
urgent one is the queue's row in a box above the page. Anything less is one unframed line (how
urgent, what is wrong, what it means, its button). On a page made of parts it sits in the card of
the part it is about (`partials/_problems.html#lines`); one whose part the page does not draw
stays in the problems' place above the page. The status at the top counts them by urgency ("1
urgent · 1 needs attention") and links to the first, and a figure in the summary band takes the
tone of its part's problems.

### partials/_reading_age.html

**A reading says how old it is.** Wherever a page shows what was read, its age is this part, over
the reading's `Freshness` where the page has one. It says "Last read 3 hours ago", with "out of
date" beside it past the line `application.freshness` draws for its kind.

### partials/_record_list.html

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

### partials/_row_actions.html (host only)

A row reads from its first line: the toggle stands there, so every cell is set at the top, and no
page sets its table's cells otherwise. What can be done to a row's record ends the row as
`Row(actions=)`, under a heading of kind `control`. Each is a `PageAction` drawn as a
compact control with its middle on the first line's. A page never draws a button in a cell
(*Gate:* the `cell` shape). A row's controls stand side by side on one line,
which their column asks for, and the words beside them wrap first; in a narrow box they take the
row's last line. Every row's controls end on one edge, and each draws its edge of its own but the
last, which is quiet only when it sets the row aside (`put_away`, a button). A cell of the row's
actions is a control whatever its heading says. A sentence in a control cell (what a write gap
links) is long, so it keeps a measure of `--row-words` as any long value does.

**A row's controls stand in the row.** The rows' actions, or a column of kind `control`, stand at
every width in the row, every one in the row's cell, with its order and mark, side by side on one
line, all one size. A label keeps one line, uncut. A field a reader fills in (a select) is the
row's value, so a column holding one never leaves, and in a cell it fills its cell and gives way
with its column. The reader's way to set a row aside (`Row(put_away=)`, the queue's Dismiss) is the
last of its buttons, drawn quiet; alone on a row's line its words start at the name's edge.
*Gates:* the survey's `row actions beside squeezed words`, and `test_row_controls.py`.

A row's own arrangement (a favorite's star, the pair that moves it) is no part of the cell that
names the row: `Row(mark=)` and `Row(order=)` draw it first in the row's controls column, the star last of
the two, a `RowMark` and a `RowOrder` (`application.pages`), so the table sizes and counts it and
the name keeps its column's whole line.

### partials/_row_group.html (host only)

Rows that belong to sections share one table as a `tbody` per group, headed by
its `Group`'s label and count, which spans every column the headings declare. They are never
two tables kept aligned by hand.

Rows that belong under one row name it (`data-folded`) and show while it is open
(`data-folds`, its toggle in the same gutter as the row toggle); with no script they all show.
A group folded away is `Group(folds=<id>)`, its rows naming that id.

### partials/_row_select.html (host only)

A cell of fixed width, so selecting moves nothing. A row with no key keeps the cell empty.

### partials/_row_toggle.html (host only)

Every row that can open has its toggle drawn by the server. A table with headings whose rows can
hold anything back (a column that leaves, by its rank or the region's, a long value a narrow box
clamps, a part only the open line shows, a block moved there, an editor or a fold) keeps the
gutter in the column that names each row: the region marks that column's cells `row-gutter` and
draws each row's toggle last in its cell, so no table can hide a value without the way to read
it whole; a table with no headings keeps it in its first column. It shows only where opening the row shows something now: a cell whose column has left, a
`narrow-more` part in a narrow box, a `row-below` part, a part the row never shows, a clamp in a
narrow box, or a fold. A wide box clamps nothing, so a row owes no toggle there for a long
value.

The toggle stands in a gutter that every row of the table keeps while any row opens, so names
share one edge whether or not a given row opens. The gutter follows from the server's markup and
the box's width alone.

### partials/_share_list.html

A ranked list of shares, as the pages and sources of a site's traffic are: each row its label and
figure on one line over a bar of its size against the largest row's, so a long tail still reads
apart. The bar is `partials/_meter.html`'s, and a label past its line is cut, whole on hover.

### partials/_site_header.html (host only)

**The header is one row.** When room runs out, the search field collapses to a button that opens
over the navigation, then the sections move behind the toggle. The navigation never shrinks below
its own content, and the bar's controls all take their size from `--control-size`. The
person's control shows their name beside the mark at any width whose bar has room for it, cut
past four controls, and only the mark where there is none.

**The user menu says whose session it is first.** It shows the person's picture, kept by HQ from
sign-on and never loaded from the provider by a page. Then come their name, what the account may
do, and how long ago they signed in, in hours or days. Then what can be set: the two switches
share a line, names over tracks, and the theme has its row. Then where the menu leads.

### partials/_table_heading.html (host only)

Each column that may leave has a rank, 1 first (`TableColumn(drop=N)`). A table that declares none
has its order derived by the region (`table_layout.leaving_order`): `optional-col` first, then
from the far end. It never lets go the column that names the row, one marked `key-col`, the
closing figure of a table that marks none, a row's state or its controls; the region turns the
ranks into the widths each leaves at.

**Every table sorts.** A list its server sorts has links in its headings (`TableColumn(sort=)`). Every other
heading has the same control, looking the same, drawn as a button, which the region keeps only
where the table holds two rows to put in order, counted across its groups. A column of controls
and a heading named for assistive technology alone have nothing to order, and
`DataTable(keeps_order=True)` keeps a table's own order, as a list page does with the one page of its
server's order it holds and a change table does with a change. `tables.js` sorts the rows on
the page: ascending, descending, then as they came. Amounts, dates and lengths of time sort as
what they are; a cell may say its own value with `data-sort`. Sections keep their own rows.
*Gate:* `test_any_table_sorts_by_a_heading`.

Rows selected together (`data-selectable-table`) start with a column of tick-boxes: the heading
that selects every row, then each row's, keyed by `Row(key=)` (`DataTable(selectable=True)`).

### partials/_table_toolbar.html (host only)

**A table's filter is a popover.** Its button names its panel with `popovertarget`. The panel
(`popover`, its id `table-filter-<name>`) opens in the top layer, placed against the button by
anchor positioning. It sits below the button from its left edge, or from its right edge when that
would cross the screen (`position-try-fallbacks: flip-inline`, which the browser decides against
the viewport). The browser dismisses it on an outside press or Escape, and the fragment primitive
shows it again after a swap that replaces it, found by its id, so a ticked box leaves its menu
open. No script places or closes it.

### partials/_toggle_set.html

No chip walls: a set of on-or-off choices is this part, checkboxes in a row of their own words
under one hidden legend, named (`name=`) when Filter sends them and unnamed when the page's script
narrows what it already holds.

### partials/_topology_map.html (host only)

**One map.** A graph is drawn by this part from what the map's view (`drawn`) hands it: the filter bar
that narrows it, a lane of cards for each kind, and the one panel beside the lanes that holds the
selected node, never a card. The edges a node stands at the end of are
`partials/_relation_list.html`, grouped by direction under a kicker each, every row the link to
the other end. A page's own list of what a record relates to, grouped by what each says, is
`partials/_relationships.html` instead. The `graph/` examples draw both from one example graph.

### partials/_visit_refresh.html (host only)

**A page about one thing asks for that thing's readings.** A machine's page, a service's page and
a record's page name which page they are as `page_visit` (`visit_refresh.page_visit`). They do so
only where a controller will read what it asks for (`cadence.WorkerStanding.takes_reads`): one
whose heartbeat says it reports to this HQ, and is not silent. Where none will, the page draws no
ask and no note, so it never promises a refresh that cannot come. `page.html` draws the ask, and
its note ends the head's first line, in room of its own; a line whose longest word or date leaves
it none hides it. Nobody pressed the ask, so its note says nothing while the readings are under
way or once they land, which the page drawn again shows: it says only a failure, and the offer of
a newer reading it could not draw in place. An unseen strut of that offer, word for word, sizes
its slot (`asks.NEWER_READING` and `SHOW_NEWER`, which the script writes from the page), so the
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
  line kept. Only if that fails does the page load again, once; if the operator has touched it or it
  reloaded a moment ago, it says a newer reading is in and leaves showing it to them.

### partials/_work_queue.html

**A queue row says what is wrong and holds what fixes it.** The queue is named "Needs you"
wherever it appears. Every list of problems is drawn by it, so every row's urgency, words and
buttons share one edge. The queue page, the findings page, the dashboard's short queue, a thing's
problems, the checks a subject does not meet, the steps a request contradicts and a domain's own
attention list all use one projection (`hq/platform/application/decisions.py`). They draw it as
one declared table (its rows `queue_table`). A page never draws a list of its own (the
`list` shape of `hand_drawn.SHAPES`). A row is a table row as any other: its words in the cell that names
it, what it offers in its actions (`Row(actions=)`), so it stands at the table's own density and gives way
as every table does. It reads, in order:

- its first line (`row-line`): the title and, only when it is urgent, the word "Urgent" after it
  in its tone (the words are `decisions.URGENCY`; a row that needs attention carries no mark, its
  word kept for a screen reader), then what it means (`row-gist`), quiet. The title states what is
  wrong and links to the thing it is about. It is what a reader presses, so it leads; a domain's
  heading, a family's name and their counts read below it. What it means is one line cut at its
  end, on the title's line where both fit, released in place by the row's toggle; a held
  request's says what is asked, the whole change its buttons decide one press away;
- quiet under it, once out of date, how old the reading it is drawn from is
  (`Insight(observed_at=...)`). An out-of-date reading never asserts a live condition: its owner
  says what was read, in the past tense, as a notice. These are words under the title, not a
  column: most rows have none. Since when (`Insight(since=...)`) is said in the origin panel, never
  on the row;
- buttons: the owner's actions, the first step that can be taken now when the owner offered none,
  and the check that reads again. At most one is primary. Where the reader sorts their own queue,
  Dismiss or Restore is the last of them (`row_actions put_away=`).

Closed, a row is one line where its words fit. Everything taller is the row's `row-below`, opened
by its toggle into the line beneath, from the title's edge to the table's. A row toggles only where
opening it shows something more, or releases a cut meaning, and with no script every row shows all
of it. The line beneath holds, in order:

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
and how many, which opens to their rows: on the queue page (`queue_table`) and
wherever a thing's problems are drawn (`fold`). The findings page keeps each row its own, its
filter being by rule.

A queue of three or fewer is read on the dashboard, under the greeting, as the queue's own rows
(`glance`), under no heading of their own. A longer one is a count and a link to its page, except
the held requests, which are read there first at any length. There a row is its title, what it
means and its buttons, holding nothing beneath; its title opens the row on the page that shows the
rest, steps included, or a held request's own page.

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

### partials/_write_gap.html (host only)

Every page that would offer a change its connection cannot make offers this instead: what the change
needs, linked to the setup step that gives it, with the whole reason as its title (`data-write-gap`).
A row draws it through `Row(gap=)` and a form through its `gap`.

### row_editor

**A row's editor opens under it.** A row edited on the page holds `{% row_editor form key
label=... action=... verb=... field=value %}` in a cell: a link to the page with that row's
editor open (`?edit=<key>`, the other query kept). On that page the region moves the form
(`.row-editor`) into the line beneath the row and draws the row open, its toggle saying so, and
with no script that line shows, since no toggle can open it. The view declares one form for the
table, drawn for the open row alone. It holds the values the cell gives by field name, or what
was sent when it is bound. Its ids carry the row's key, so none is drawn twice. A region that
holds an editor's link is a region of the page (`data-fragment`, its id from its label). So with
script the link, the form and its Cancel are answered there through `fragment.js`'s own paths.
The form posts `next`, the page with the editor closed. *Gate:*
`hq/platform/core/tests/test_row_editor.py`.

### run_evidence (host only)

What a run of automation shows for itself is `{% run_evidence evidence %}` in a declared row's
open line (`Row(evidence=)`). That is its machines, its correlation id, its lease while a worker holds it, and
each task's outcome, each a labeled `row-below` block of the row's open line. Jobs draws it in a
run's row, and a section's rows draw it through `ServiceSection.below`, so a machine's page and
a playbook's page show a run as Jobs does. A run's own page draws the same pieces as its facts,
`layout="facts"` inside its `{% facts %}`. A section shows it only to a reader who may open Jobs
(`service_context.recent_work`). A section's rows end with what may be done to each record
through `ServiceSection.actions`, drawn as a row's actions as Jobs draws a run's Cancel
button (`automation_runs.cancel_action`); a section none of whose rows has one draws no column
for them.

### section

`{% section "Agents" count=n lede="..." %}body{% aside %}a line, a link, a control{% empty %}No
agent has connected yet.{% foot %}How to read the body.{% endsection %}` is a titled part of a
page: a `<section>` labeled by its heading. Its head stands on the page: the heading with its count
standing in it, the lede under it and the aside beside it. Its body follows in the frame. After
the body comes its foot, drawn only with one: the line that says how to read it (what a figure
means, where the numbers come from), quieter than the body. The heading's line is one control
tall, so a control beside it adds no height and heads in one row are one height. The aside is one
box at the row's end, so its parts (a count, a dot, a link) wrap as one line of text, and it keeps
to one line while the row has room for it. What draws its own edges, cells or fill fills the frame
with no inset and no second border, as a card's flush list group does in Bootstrap. That is a
table's region, a band of facts, a row of metrics, a month's grid, or a list whose rows carry
their own padding. Prose, facts and the empty line sit in the frame's padding. With no body it
draws its empty part as the empty state's line, followed by the link that fills it when
`remedy_label` and `remedy_url` name one (`{% section "Receipts" remedy_label="Upload one"
remedy_url=url %}{% empty %}No receipts.{% endsection %}`), and with neither it draws nothing.

`more_label` and `more_url` end the aside with the quiet link to the whole of what the section
shows part of ("View Estate", "All runs"), after a dot when the aside says something too, never a
foot under the list. With no address the link is left out, so a page passes the one it has. A
line that dates or sources a section goes beside its heading, not loose under it. A section whose
rows are the latest of more names the rest as its `more_label` and `more_url`.

`{% lead %}` is the start of the body, drawn in one box with the head, for a section whose body
runs down columns: the head is kept whole, so it never ends a column with what it heads in the
next. The body after it continues the same list (`{% section "Lane" %}the rest{% lead %}the
first{% endsection %}`). The columns' container sets the space above each head as
`--section-lead-above`, which the head holds as padding so every column starts level. A folded
section's toggle and a figure's caption take none.

`figure=True` draws the section as a `<figure>`, its head the `<figcaption>`, for a drawing (a
chart, a calendar) whose title, line under it and legend are a section's head like any other. A
section inside a section heads itself a level down inside the outer frame, and so does one inside
any other frame (a card, a form, a band's cell). The frame's `--surface-inset` is 0 there, so its
head draws no rule and its body adds no frame or inset of its own. A part that draws its own title
(a dialog, an insight card) wraps what it holds in `{% headed %}…{% endheaded %}`, so a section
inside heads itself a level under that title. A `<pre>` in a section's body wraps and scrolls only
in height. Two of one heading name their own ids, and `id` is the section's own (a link to it, a
region refreshed in place).

`fold=True` draws it closed with its head as its toggle (`open`, any truthy value such as a form's
errors, starts it open), its count beside it; never a separate "Show all". What it holds sits on
a panel like its neighbors: a fold of one table has the table's region, and any other fold is a
folded section, its body in the section's frame. `css` and `data_*` are the section's, on a
landmark that never pads itself, whatever `css` names: the frame holds the padding.

A section draws its head inside its frame, over a rule, unless its body is one table region and
nothing else. Then its head stands above the frame so the table reaches its edges (`:has()` in
surfaces.css decides; a page picks nothing). `head="above"` or `head="inside"` overrides it for a
page that needs the other look. Frames that share a row start together, so in a pair (`{% layout kind="pair" %}`)
a table section beside a section whose body is anything else takes its head inside its frame;
alone, or beside another table, it keeps its head above. A section's heading that follows a form,
a table or a list in the same box starts a section of its own: `.section-head` clears what is
above it by the stack gap, so it is never closer to what came before than to what it heads.

`edge` takes a status tone (`{% section "Request" edge=entry.tone %}`) for a section that stands
out from those beside it while it is in a state, such as a request waiting for a decision. The
tone runs down the starting edge of whichever box frames it, and with no tone it has no edge.

A heading is plain words, never markup a filter draws (`|when`): `SectionPart` refuses markup in
it, and a template meets that only as it renders. A heading that names a moment gives it as
`moment`, in the `when` form `|when` takes, and the part draws the `<time>`: after the heading's
words, quieter than them (`{% section day.name moment=day.day when="day" %}`), or as the whole
heading when it has none (`{% section moment=month when="month" %}`). A page built in code builds
the same part as `hq_sdk.ui.SectionPart`, its body drawn from `template` and `context`, and writes
`{{ part }}`. The part is the head, the frame and the spacing between, so a page states the
content and never the spacing, the frame or the count's look.

### select (host only)

A choice of one drawn from data is `{% select "rule" choices chosen=rule label="What it sets"
tone=tone %}`: a `<select>` named by its first argument, an option for each `(value, words)` of
its second, the chosen one selected, and `label` the name a reader hears (a table cell's
setting). With an `id` its `label` is shown, the `{% control_label %}` drawn before it for that
id, as a question's fields are named (From, To, Port). A value alone is its
own words (a year, a port), and `submit_on_change=True` sends the form as the choice changes,
for a bar whose one choice is its whole query. `tone` says the choice in a status tone, the
status part's ink, edged in it unless neutral, as an agent's rule reads "On" in the good tone and
"Off" in the serious one. A select that narrows names the dimension it narrows (`every="type"`),
and the part leads it with "Any type", which claims no total, so no summary contradicts it. A
select that picks a view or an order (the map's View, a table's Sort) offers only named choices.
Its default is named for what it shows ("Main lanes"), never Any or All. The empty option comes
only from `every=`.

### status

**A state is a pill drawn by `{% status %}`.** `{% status row.state %}` (`hq_sdk.ui.status_pill`
from Python) says the state in sentence case (`labels.sentence`), whatever its source wrote:
Docker's "exited" reads "Exited" beside a model's "Done". Its color is one of the four tones
(`good`, `attention`, `serious`, `neutral`), never a word of its own: `tone` takes a `Tone`, and a
subject's state is a `Toned` member that carries its words and its tone (a machine's
`MachineState`, a model's stored status declared as `TonedChoices`, `ARCHIVED = "archived",
Tone.NEUTRAL, "Archived"`). Any other word is refused where it is drawn. Several parts are one
label (`{% status "HQ expects" desired tone="serious" %}`); `href`, `title`, `tip`, `css` and
`data_*` say the rest. No template or module writes a pill by hand. With `chip=True` it is one of
a record's names (a technology, a domain, a role) as a neutral chip, said as written and taking no
tone, so no template writes a `.tag` by hand. With `pill=False` it is the state's words in its
tone, as a headline or a line of text says a state. The sentence that says what a state means
takes the same tone beside it. A record's title line is "Working · Matches HQ's config.", both
green, as parts of one `.dot-list`, so the sentence shares the state's line where it fits and
wraps under it where it does not. With `icon=` beside `pill=False` a mark (`partials/_icon.html`)
leads those words in their tone. So a table cell says its state by its mark as well as its
color, as a container's standing does ("1 serious" after a shield). With `ink="context"` only the
mark is toned ("Up to date"), and with `shown=False` the mark stands alone, its words said only
to a screen reader, as a certificate's check after the name it serves is. A first word holding a digit, a version, is said as written. A cell
that is a state carries its tone (`Cell(word, tone=...)`) and is drawn by it. *Catalog:*
`status/icon`. *Gates:* the `status-pill-built-by-status-pill` ast-grep rule and
`test_status_tags.py`, which reads every template root for a hand-written pill or a state's tone
written as a class.

### steps (host only)

A list of short steps (`{% steps %}`) is read down its columns, so its columns end within a step
of each other. Each step is `{% step icon=... %}`: its icon, then what it says; `tone` colors the
icon by how a check stands, `done` draws a finished step quiet, and `title` is its tooltip. Steps
done in order are `{% steps numbered=True %}`: read down wider columns, as many as fit up to
three, so a short run fills a wide frame; each step's number stands where its icon would be, so
its steps take no icon. A step an operator takes on their own machine (a `Run`) or in words (a
`Do`), each with its notes, is `partials/_operator_step.html` given `step`: HQ offers it and never
runs it, and it carries no secret.

### switch (host only)

`{% switch label=... %}{% choice url=... current=... %}...{% endchoice %}{% summary %}...{%
endswitch %}`: a row of links to the same view (a window, a status, a tab, a sibling), each a
`scope-chip`, the current one `aria-current="page"`, a choice's `count` beside its chip and its
`note` and `mark` inside it. A choice of the view's state is a `.scope-chip`, never a chip of
places to go.
