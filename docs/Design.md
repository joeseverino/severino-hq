# Design

HQ has one look, built from a small set of primitives. A page is assembled
from them; it does not style itself. If a page needs something the primitives
cannot express, the primitive grows (in its sheet under `static/css/app/` and its partial),
and every page that uses it gets the improvement. Extensions ship no CSS for
the same reason: they render host classes and inherit every fix.

The stylesheet is the sheets `hq/platform/core/stylesheets.py` lists, each
one responsibility under the 1,000-line ceiling, linked in that order. The
first declares the cascade layers (`tokens`, `base`, `components`) and the
rest fill them; inside a layer a later sheet wins a tie, so a sheet is added
to the list where its rules belong in the cascade, and every check of the
stylesheet reads it through that list.

Each rule names its gate where one enforces it. A lower-case gate without
`test_` is a rule of `LayoutBrowserTests` (`hq/platform/core/tests/browser/layout.py`), held on
every page at every width it applies to, each a subtest named for the rule,
the page and the width. The browser gate judges what a reader would lose:
text out of its box or on other text, content out of reach, a page wider than
the screen, an unreadable color, a page that jumps. A rule with no gate is a
matter of taste, held by review and by looking (`mise run dev:look`), never by
a number; [Held by eye](#held-by-eye) collects the ones a gate is most
tempted to measure.

## Rules

**The owner's words, not HQ's.** One person reads this interface. He knows his
machines and does not know how HQ is built, so a page never says the names HQ
has for its own parts: a thing is called by its own noun ("DNS record",
"certificate"), HQ "reads" and "applies", and what differs was "changed
outside HQ". The Vale style `tools/lint/vale/styles/Plain` holds each word that is never
shown with what to say instead, and each label that was retired for one name
per thing: the queue is "Needs you", search is "Search" and its box "Find
anything", a fresh read from a thing's page is "Refresh" ("Refresh all" for
everything) and from a problem card "Check again". A capability and the
button that runs it say the same words, a filter form's button is "Filter",
and a wait is "shortly". Comments and identifiers keep the internal vocabulary.
*Gate:* `lint:prose`, over the paths `tools/lint/vale/.vale.ini` gives Plain.

**One frame per thing.** A box that holds content is a *surface*: one border,
one radius, one background, one shadow, drawn by the single surface rule in
`css/app/surfaces.css`. A component joins by adding its selector to that rule's list and
declares only its layout. A surface inside a surface draws no frame and no
inset: the outer one already says "these belong together". Nesting is handled
by inheritance, so nobody has to remember it.

**Size to content, fit the window.** Layouts are intrinsic: grids fit as many
columns as their content allows (`repeat(auto-fit, minmax(min(100%, X), 1fr))`),
rows wrap (`flex-wrap` with a basis), text wraps by measure. When a component
must change shape, it asks its own container (`@container`), not the screen,
so it reads the same in a narrow card as on a phone. A viewport breakpoint is
for what the viewport decides (the header, a full-screen modal) and says why
in a comment. Content uses the window up to `--content-max`; prose keeps
`--measure`. Never make an element holding buttons a size container: Chrome
restyles its contents once the container is measured and runs their
transitions from nothing, so every button fades in on load.
*Gates:* the overflow sweep at a phone's width (402), 820 and 1360
(`WIDTHS` in `hq/platform/core/tests/browser/widths.py`);
`test_viewport_breakpoints_only_go_down`, a ceiling that only falls.

**A table fits its box, at every width.** No screen width is asked anywhere
in the table primitive. A table takes its box's width and wraps, the browser
sizing each column from what it holds. Each column that may leave has a rank,
1 first (`{% table_heading ... drop=N %}`; a list's are derived by `ranked`:
`optional-col` first, then from the far end, never the column that names the
row, one marked `key-col`, or the closing figure of a table that marks none).
Only the browser knows how wide a table is, so `tables.js` decides what shows,
the established responsive-table pattern: while a table is wider than its
region, its next ranked column leaves (rank 1 first, the rightmost of a rank
first, `data-dropped` on its cells), and once none is left to go the table is
in a narrow box (`data-narrow`), then tight (below); as the region grows
they come back, the last to leave first, each only if the table still fits. A ResizeObserver on every
region does it in the frame the table is laid out, before that frame is
painted, and the script loads in the head without `defer` so every region is
watched from the moment it is parsed, and nothing moves once a page is
drawn. What leaves is in the row's toggle. The columns showing set
`--showing`, which bounds what a held line asks for (`--hold`). In a narrow
box (`--box: narrow`, else `whole`, read by `@container style()`) a table gets
roomier rows and holds back what is marked `narrow-more`, and what is marked
`narrow-only` shows there alone, a few words in the cell that names the row
standing in for the columns that left (the audit log's action under its
name). A table still wider in a narrow box is tight (`data-tight`): its
headings, times, statuses and a value's parts wrap at their spaces and its
cells stand closer, so the narrowest phone (320px) holds every table the
gates draw (`a_table_fits_its_box_at_every_width`). With no script nothing
leaves and the table scrolls in its region.
The region marks each body cell that says
nothing (`data-blank`) and every cell of a column blank in every row
(`data-blank-column`), which is not drawn. Since a
table fits, its region is no scroll box and its heading pins under the page's
chrome; only a region declared a pane (`pane=True`: a height of its own, as a
chart's data or the topology ledger, or a table sized to its content) or a
chart's (`chart=True`) scrolls, pins its heading to its own edge, and takes
focus. A table never stacks into cards and is not swiped through. A key/value table (row headers,
no `thead`) sizes its label column to its longest label. Rows that belong to
sections share one table as a `tbody` per group, headed by
`{% row_group label count %}`, which spans every column the headings declare,
never two tables kept aligned by hand. Rows that belong under one row
name it (`data-folded`) and show while it is open (`data-folds`, its toggle
in the same gutter as the row toggle); with no script they all show. A group
folded away is `row_group ... folds=<id>`, its rows naming that id. A row
reads from its first line: the toggle stands there, so every cell is set at
the top, and no page sets its table's cells otherwise.
*Gates:* `a_table_fits_its_box_at_every_width`,
`test_a_blank_column_is_not_drawn_and_a_short_word_is_not_split`,
`only_a_table_scrolls_sideways`, and the page crawl, which fails any
`.data-table` outside `.table-scroll`.

**Opening a row moves nothing.** What a row holds back, the columns that
left, the `narrow-more` parts and the `row-below` blocks, is in a line of its
own beneath the row, drawn by the server after the row (`tr.row-detail`,
`table_tags._detail_row`) and shown while the row is open: the blocks first,
then each part under its column's name (`dl.row-columns`). A column's part
shows while the column is gone (`data-dropped`, which `tables.js` sets on it
as on the cells), a narrow part in a narrow box, a labeled block always; a
line with nothing to show is a blank row of no height. The line holds a copy
of what the row holds back, so the row's own cells stay as they were, and the
columns are held at their widths for as long as any row is open, so every
other row keeps its place. Every row that can open has its toggle drawn by the server
(`{% row_toggle %}`), and it shows only where the row holds something back
now: a column that left with something in it, a `narrow-more` or `row-below`
part, a fold, or a value `tables.js` found cut (`data-cut`). The toggle
stands in a gutter that every row of the table keeps while any row opens, so
names share one edge whether or not a given row opens. The gutter follows from
the server's markup and the box's width alone; a cut value shows its toggle
only in a gutter already kept, so nothing the script marks moves a name. In a
narrow box every table whose rows the server made openable keeps its gutter,
since touch has no hover title and a value cut there must still open.
With no script a toggle opens nothing, so nothing is held back for one: every
column and part stays, and the table is sized to its content inside its region.
*Gate:* `test_a_wide_table_fits_a_phone_and_opening_a_row_moves_nothing`.

**Every table sorts.** A list its server sorts has links in its headings
(`table_heading sort=`). Any other table whose rows are worth sorting has the
same control, looking the same, drawn as a button (`local_sort=rows|sortable`,
or a row count where rows come in groups), and `tables.js` sorts the rows on
the page: ascending, descending, then as they came. Amounts, dates and lengths
of time sort as what they are; a cell may say its own value with `data-sort`.
Sections keep their own rows. No table is drawn by hand, an extension's
included: `hand_drawn_tables` in `plugin_testing` refuses one, and the host's
suite runs it over every installed extension's templates.
*Gate:* `test_any_table_sorts_by_a_heading`,
`test_every_table_is_declared_through_the_tags`.

**A table is laid out before any script runs.** A long identifier is one
line, cut with an ellipsis at a share of its region's width
(`--identifier-max`, in container units between a floor and a ceiling), its
whole value in its title and in the open row. A region whose rows leave an
identifier less of the row names it with `room=`: `shared` when the
identifier sits beside wide columns (it gets little until the region is wide),
`snug` when it sits beside other identifiers (each is held near one short
length); a list view says it as `table_room`. An identifier the row is read
by, in a row of many columns (a container's image), is `code.cell-whole`: it
wraps at its seams instead, so its column gives way without hiding any of it.
Outside a narrow box a status keeps its line and a one-token name is never cut; a
value told in several parts marks each `cell-part`, and where its line is
short it wraps only between them. Every such hold, and an identifier's cut,
asks for at most its column's even share of the region (`--hold`, counted
from the columns the box draws, the naming column's half a share more), so
the holds of a table never sum past its box: what fits its share stays whole,
and what does not wraps or is cut. The region wraps what a status or figure
cell says in one `.kept-line` for the stylesheet to bound. Free text in a cell (a message,
a record's words) is marked `cell-data` and breaks a run with no seam in it (a
link, a digest) wherever it must, so it never holds its column wider than its
box; an identifier in a prose column waits for the open row (`row-more`)
rather than holding that column at its cap. A run of links longer than the count its template
declares folds behind "+N", and a stacked list longer than four shows three
and "+N more". A row that holds anything back gets one toggle that lifts every
cut and opens every fold in it. Nothing is measured to lay a table out:
`tables.js` asks only whether a value is cut (`data-cut`), which shows a
toggle in a gutter already kept, and holds the columns still while a row is
open or the rows are refreshed.
An intrinsic size (`max-content`, `min-content`) goes on an element inside a
cell, never on the `td`: Chromium ignores it on a table cell and WebKit does
not, so the two engines would size the column differently.

Outside a narrow box an identifier in a cell is one line, never broken at a
seam: one that breaks only at its seams (an address with a port, a host name)
holds its whole length, so its table lets a ranked column go before the name
would break, and a name the cell cuts shows one line, cut. A narrow box has no
column left to let go, so there it wraps at its seams. The row's own name,
free text, a preview and a context line wrap as they always do.
*Gate:* `broken identifiers` in the layout survey,
`test_an_identifier_in_a_table_is_whole_while_a_column_can_leave`.

A row's opening is its only disclosure: what is shown only once a row is open
is marked `.row-more` (a word, a list item), or `.row-below` for a block,
which opens on the line beneath the row at the table's width; the face that
stands in for it while closed is `.row-less`. Never a `<details>` inside a
cell.

**One band.** A row of facts, readings or choices is a band: one surface,
cells divided by hairlines each cell draws itself, so a wrapped row keeps its
dividers at any width and a short last row ends in plain surface. A band of a
fixed count lays out as whole even rows (never 3+1): a band counts its own
cells (not a dialog a cell opens, not its note) and takes the tracks for that
count, two to six (`--tracks-2-1` up to `--tracks-6-3-1`), and a lone cell takes
the band, so no page says how many it holds. A band is listed once, in the band rule in `css/app/figures.css`; its
cells keep their own padding. Too narrow for two cells, a band reads as a
list: label left, value right. What names a value (`.band-label`, a tile's
label, a `<dt>`) is sentence case in the muted voice; capitals are for a
kicker over a group (`.eyebrow`), never a value's name
(`test_no_value_is_named_in_capitals`). A list of short steps (`.step-list`) is read
down its columns, so its columns end within a step of each other. The
dashboard's readings are one row when each has room and a list when not.

**A route reads in one direction.** A path from one thing to another
(`control_plane/_path_hops.html`) is one surface, each hop a stop on one
line with its icon on the line. It runs across its box when every hop has
room for its kind and an address with its port, and down it otherwise, as
the connection panel draws its route; it never wraps onto a second row.
Its line runs from the first stop's mark to the last one's, with any stop
opened. A check names what it was read from right after its statement, or
under it where the line is full, and that evidence is shown at every width.
*Gates:* `a_checks_evidence_follows_its_statement`, `nothing_a_summary_names_is_hidden`.

**One main-and-aside.** Content with a narrower companion (`.split`) shares a
row while both keep a readable width and wraps to a column when they cannot.
Pairs (`.two-col`) are at most two columns and pair like with like: a table
beside a table, a chart beside a chart, so each row ends together.

**Forms are two columns, or one.** Fields flow in reading order, two to a row
where there is room and none narrower than 18rem, and a row's fields share its
whole width: pairs sit side by side and the odd one of a run takes its row, so
no row is left with a field and a hole beside it. A textarea, a group of
choices, errors, a command's confirmation and the actions take the row. A
group of checkboxes reads across. A command's form is this grid too: `.form`
is the one form layout.

**Nothing empty takes space.** A panel with nothing to show is not drawn: the
page closes up rather than carrying a card that says "nothing here". An empty
list says whether its own search or filters emptied it, and offers the way
back. A page with nothing yet says what is missing and, where it can, what
would fill it and where (`partials/_empty_state.html`: `message`, `why`, an
action); controls that only make sense over data wait for the data. A
`{% table_region %}` whose tables hold no row draws that card in their place,
so no page draws headings over nothing.

**One head.** `partials/_page_head.html`: trail, title, controls, lede, laid
out the same at every width. The title and its controls share a row (the title
wraps before the controls move); the lede runs the full width under both. Two
controls stand beside the title at every width, the lead and the next the page
names, and the rest go behind the overflow menu; a head too narrow for three
(a container query on the head) folds the second in too. The lead is chosen by what each control is for, never by where it stands: not
a destructive one while another is offered, then one that can be pressed, then
the page's primary (`lead_action`). A destructive verb (a stop) is a link to
its confirmation, which names what it stops.

**A lede earns its line.** Most pages have none. A lede says what the page
cannot show: a fact that changes how you read it ("Lookups run from outside
this network"), or the record's own details. Never a table of contents of the
page, a pointer to another page (link to it instead), or how HQ works inside
(a file path, a background mechanism).

**Say what HQ knows, once.** A sentence on a page is derived from the record
that holds its answer (the registrar the zone record names, the issuer and
expiry a certificate's status holds), never a generic placeholder or a
hardcoded string. Each fact appears once, where it belongs: a card does not
repeat the strip beside it.

**A title states the fact it checks.** "Tokens hold only the permissions HQ
uses", "Keys are kept in a vault": what was checked, in plain words. Not a
sweeping claim ("every credential is limited to what HQ needs"), and not
"knows", "ensures" or "limited".

**A zero is information.** A count of zero says the thing was checked and there
is none; it stays on the page. Hiding it leaves the reader wondering whether it
was counted.

**Dense, never empty.** Content sits on a surface; when one is too big or too
loud, make it compact (one row, tighter padding, an urgent tone only when
urgent), never bare on the page background. A verdict sits where it belongs (on
the link between two ends), evidence beside its value, one line per check with
detail on demand, side-by-side cards of similar height. A card that is mostly
empty, or a row with a gulf in it, is a layout to redesign, not padding to trim.

**A card is drawn by `{% card %}`.** `{% card css="example" heading="example-heading" %}...{% endcard %}`
frames what it encloses (`partials/_card.html`): a section unless `element=` names
an article, figure, details or div, `heading` its heading's id or `label`
its name when it has no heading, `busy` while its content is still coming, and
each `data_*` option a `data-` attribute (True draws it bare). A state of the
card is a `data_*` option the stylesheet reads, never a class written inside an
`{% if %}`. No template writes `class="card"` itself
(`test_only_the_tag_draws_a_card`). A card is never wider than the column or
track it is placed in (`.card { min-inline-size: 0 }`), so on a phone what it
holds wraps inside it.

**The header is one row.** When room runs out, the search field collapses to a
button that opens over the navigation, then the sections move behind the
toggle. The navigation never shrinks below its own content, and the bar's
controls all take their size from `--control-size`.

**One menu.** `.overflow-menu` for "more actions", the nav dropdown for
navigation. Every dropdown carries `data-menu`, which gives it the shared
dismissal (outside click, Escape). Inside a menu every control is a plain row;
a destructive one is only its color.

**The user menu says whose session it is first.** The person's picture (kept
by HQ from sign-on, never loaded from the provider by a page), their name,
what the account may do, and how long ago they signed in, in hours or days.
Then what can be set: the two switches share a line, names over tracks, and
the theme has its row. Then where the menu leads.

**One disclosure.** Every `details` that is not a menu shows the same mark,
turned when open. A section that folds (`section-fold`) uses its heading as the
toggle, with its count beside it; never a separate "Show all".

**One filter bar.** `.filter-bar`: a search field, selects, Apply, and Clear
when anything is set. No chip walls.

**Values read as what they are.** `|readout` puts identifiers (keys, hosts,
paths, ports) in `<code>` and leaves counts, words and phrases as text.
Numbers are never chips: a figure stands in a KPI cell, a numeric column or
a sentence (`hq/platform/core/tests/test_template_chips.py`); one that carries
a state says it in ink, as `.figure` with its tone class
(`<span class="figure tone-attention">`). An address, a
path or a command breaks only where `|breakable` lets it (after a dot or a
slash, before a port), never inside a hyphenated name, and never so that
one or two characters of it stand alone on a line; a length of time keeps
each count with its unit. So every value a
template prints as text, in a sentence too, ends in `|breakable` or a filter
that holds its names itself (`|readout`, `|or_empty`, `|when`), and markup
built in Python holds each value it prints the same way; `|breakable` passes
a plain word and markup as they are, so it is safe on any value
(`hq/platform/core/tests/test_template_values.py`). A seam is an
empty `.seam` whose generated text is a zero-width space, never a `<wbr>`:
Chromium breaks at a `<wbr>` even under `white-space: nowrap`, and WebKit
does not. Chromium also breaks a part held to one line (`.whole`) that has no
box (`display: contents`) at an element's edge inside it when the box around
the part has `overflow-wrap: break-word`, so a box that cuts such a part sets
`overflow-wrap: normal`. An external link's arrow (`.external-link`) is joined
to its last word by a no-break space, and a link whose name is one box (a cut
token, a chip in a table) lays the name and the arrow out as one row, since a
line may break after any box.

**A change is one primitive.** A value and the one it changes into (a running
version and the newer release, an installed package and the version that
fixes it) is `{% include "partials/_change.html" with from=… to=… %}`, with
`code=True` for identifiers and `url` to link the new value. Old value, arrow
and new value share one baseline, a narrow line breaks only between the two
values with the arrow kept beside the new one, and a reader hears "1.2.3 to
1.3.0" rather than an arrow. *Gate:* the `changes` layout check
(`a_change_reads_on_one_line_and_baseline`).

**A mark drawn by CSS is not read.** Generated content (`content:`) says what
a reader hears after a slash: `"›" / ""` for a chevron, a separator or an
arrow, the words themselves for text that means something. *Gate:* `lint:css`
refuses a generated string with no alternative, or a space before a mark.

**A mark between words is held to the word before it.** A dot between two
parts of a line is `{% dot %}` and a link's onward arrow `{% arrow %}`, each
written straight after its part: a no-break space joins the mark to that
part and a reader never hears it, so no line starts with a mark or holds one
alone. A run of whole parts is a `.dot-list`; any other mark is the text of
an `aria-hidden` element. *Gate:* `test_template_separators`, which follows
every branch and include before each held mark, over the host's templates
and an extension's.

A moment, a date and an age are each written one way, by `hq/platform/application/moments.py`:
`when` ("Oct 3, 9:29 AM"), `when_day` ("Oct 3"), `ago` ("5 days ago", one
unit) and `when_exact` ("Oct 3, 2026, 9:29:15 AM CDT"). The year is said only
when it is not this one. The spaces inside a date and inside a time are
no-break spaces, so neither wraps inside itself. A page shows them through the built-in `|when`
filter, which wraps the words in `<time datetime title>`: `{{ at|when }}` on a
detail page, `{{ at|when:"ago" }}` in a list (the exact moment on hover),
`"day"` and `"exact"` for the other two. A calendar asks `"weekday"`
("Monday"), `"month"` ("October 2026") and `"numeral"` ("3"); a wait asks
`"length"` ("5 minutes", either way from now) and `"ahead"` ("in 3 days").
The `datetime` is what a table sorts on, so a date column needs nothing else.
`when` is the only way a template writes a moment: HQ's engine
(`hq/platform/core/template_engine.py`) has no `date`, `time`, `timesince`,
`timeuntil` or `{% now %}`, so a template that asks for one fails as it
compiles. A column's weekday heading is a label its projection writes, and a
machine value (a `?month=` address) is built by the view. `|ago` and
`|day` are the same age and day as plain words, for a sentence, an attribute or
a label where a `<time>` cannot go. A run of days is
`when_range` ("Oct 3 – Oct 9"), a year both ends share said once. Extensions
get all five from `hq_sdk.ui`.
*Gate:* `test_a_template_that_writes_a_moment_itself_fails_as_it_compiles`.

A count's plural has one owner too: `counted`, which spells a single noun
through `labels.plural` ("2 entries"). An audit type's plural is
`AuditLog.type_plural`: the model's `verbose_name_plural` where declared.

A bare figure counting what a heading heads (`.count`) stands in that
heading after its words: `<h2>Readings <span class="count">7</span></h2>`,
and the same in a summary, a group row and a tab. Never in parentheses, and
never alone at the far end of a `.section-head`, cut off from the words it
counts; what stands there is a line about the section, a count with its
unit ("3 events", "2 left") among them.
*Gate:* `test_every_bare_count_stands_in_its_heading`.

**Color comes from tokens.** No color literal outside `@layer tokens`, so a
theme is a set of token values and dark mode is not a second stylesheet.

**Buttons.** Primary for the one thing a page is for; default for the rest;
`ghost` for link-like actions that sit inline; `danger` in red text on the same
surface as its neighbors, loud only in its confirmation.

**An action says its command's words.** What a button, link, remedy or toast
says is declared on the command it runs ([Plugins](Plugins.md), "Capabilities")
and read from there, so a page and the button it describes cannot disagree. A
link that runs a command is `action_links.command_action`, which takes a
`variant` of the declared words and no label. A finding's `FindingRemedy` says its
command's words too, or its own `intent` where the command's name would not
say what this press does ("Put an access list in front of x"), declared once
on the remedy. A template names a button with `{% words "name" "variant" %}`,
in a control and in a sentence that says which one to press. A press answers
"Refresh requested." (`integration_specs.requested`); asked-for work waiting
its turn says `{% waiting %}`, and a saved change says when it goes live with
`{% applies_shortly %}` (`asks.APPLIES_SHORTLY`), beside the save button, so a
field's note says only what the change does.
*Gate:* `hq/platform/application/tests/test_action_words.py`.

**A queue row says what is wrong and holds what fixes it.** The queue is
named "Needs you" wherever it appears. The queue page, the findings page, a
thing's urgent problems and a domain's own attention list use one projection
(`hq/platform/application/decisions.py`) and draw it as one table
(`partials/_work_queue.html`, its rows `partials/_queue_rows.html`), so every
row's urgency, words and buttons share one edge. A row reads, in order:

- the title and, only when it is urgent, the word "Urgent" after it in its
  tone (the words are `decisions.URGENCY`; a row that needs attention carries
  no mark, its word kept for a screen reader), which states
  what is wrong and links to the thing it is about. The title is what a reader
  presses, so it leads; a domain's heading, a family's name and their counts
  read below it;
- since when, where its owner knows (`Insight(since=...)`), and never a guess;
  how old the reading it is drawn from is (`Insight(observed_at=...)`). An
  out-of-date reading never asserts a live condition: its owner says what was
  read, in the past tense, as a notice;
- one line of what it means;
- buttons: the owner's actions, the first step that can be taken now when the
  owner offered none, and the check that reads again. At most one is primary.
  Where the reader sorts their own queue, Dismiss or Restore ends the row.

Closed, a row is those one or two lines. Everything taller is the row's
`row-below`, opened by its toggle into the line beneath, from the title's edge
to the table's: what it means in full; what was read that shows it
(`Insight(facts=...)`), each label once with its values beside it; what to do, where a command or an instruction is a step,
one standing alone and several numbered; the reason it cannot be done from
here, as a sentence and never as a step; and the pages that help look into
it (`Insight(links=...)`), the topology's as icons with HQ's tooltip. The
buttons past the first two follow, in one place under the rest, and on a
phone every button waits there, each the line's width, while Dismiss stays in
the row as a mark. A row toggles only where opening it shows something more.
A command is `partials/_command.html`, with its copy button in its corner.
The word is
`partials/_urgency_pill.html`: a Cmd- or Ctrl-click opens what raised it (the
finding rule, its severity and the line its detector is defined at, or the
domain and key of anything else), read from the code rather than written
down. With no script every row shows all of it.

A next step an owner names as a short label with a `url` is a link to that
page; one it writes as a sentence is shown as a sentence. Each action renders
once, by method and URL, through the page-action renderer and the shared POST
form, and the transport keeps the owner's full workflow. A dismissed row is
its title and the way back. A link to a row (`/action-items/#…`) lands on it
open.

A queue of three or fewer is read on the dashboard, under the greeting, as
short cards (`partials/_queue_glance.html`). A longer one is a count and a
link to its page. There a card is its title, one line of what it means, its
age and its buttons; its title opens the row on the page that shows the rest,
steps included.

**A reading says how old it is.** Wherever a page shows what was read, its age
is `partials/_reading_age.html`, over the reading's `Freshness` where the page
has one: "Last read 3 hours ago", and "out of date" beside it past the line
`application.freshness` draws for its kind.

**A frame is filled.** Facts are `.kv`: each label beside its value, as many
pairs to a row as the width holds, never a two-column table stretched to the
page. A pair whose value is a list of names (`.address-list`, or a
`.code-list` of two or more) takes a row of its own, so it never wraps beside
another pair and leaves that pair's row a gap; the layout gates' `lists of
facts` fails one that does. A value
goes under its label only where the box cannot hold both on one line, and
the same holds for a node key beside its machine and a piece of evidence
beside its kind (`.conn-rows` flows each row once it is too narrow for three
columns). A thing's
state sits on its title's line (`page_aside`), as words; the facts about it
at a glance are a band under the head, as a service, a machine and HQ's own
page open. A line that dates or sources a section goes beside its
heading (`.heading-aside`), not loose under it. The sections that
close a thing's page are `.section-flow`: each a card holding its own heading,
two to a row at one height, with a wide one (four columns or more, or
folded) across the row and the short ones collected to pair. A frame across
the page is filled by what it holds, or narrowed to it.

**Only what is urgent is framed.** A thing's page draws the problems about it
from `partials/_problems.html`. An urgent one is the queue's row in a box
above the page; anything less is one unframed line (how urgent, what is wrong,
what it means, its button) and, on a page made of parts, sits under the part it
is about. The status at the top counts them by urgency ("1 urgent · 1 needs
attention") and links to the first, and a figure in the summary band takes the
tone of its part's problems. A link to a part of a page lands below the sticky
header, from one `scroll-padding-top`.

**An item is open until its owner stops raising it.** Nobody closes one by
looking at it. What a reader can do with one they are not acting on is dismiss
it: it leaves their queue and their counts, stays reachable in a fold below,
and comes back on its own when it gets worse or grows. The header and the
dashboard count what waits; a dashboard with only dismissed items says so and
links to them, and never claims all is clear.
**A family folds.** An owner names what kind of matter an item is
(`Insight(family="Image advisories")`), in the words it would head a list
with. Several open items of one family from one domain are one line in the
queue (how bad, what kind, how many), a row that opens to theirs, and can be
dismissed whole.
A family of one is the item alone.

**Dismissing a row costs a write, not a queue.** A row's button names the row
itself (its revision and key), so dismissing or restoring one composes
nothing; with script it happens in place, the row becoming one quiet line with
the way back and the counts over it following. Only "all of these" asks the
server what "these" are.

**A notice is not something to do.** An owner marks an item a notice
(`Insight(notice=True)`) when it reports something that happened or was
measured and there is nothing to resolve. The queue keeps notices in their own
section, out of the "needs you" count, and a dismissed notice is done with: it
returns only when what it reports changes. The line is drawn once
(`action_items.split_waiting`) from what each item says of itself.

The queue page is sections by the domain that raised each item, derived from
what the items carry (`hq/platform/application/action_items.py`), so a domain that starts
raising items has its section without being listed anywhere.
Filters that match nothing offer a way back; they never imply a healthy estate.

**Overview figures have a hierarchy.** The shared KPI partial accepts
`featured=True`: a four-fact overview leads with its contributor's first fact,
then the three supporting facts together. Values, context and links render
once from the KPI collection. Other counts keep their even band. Overview cards fit their available width, placing three domains
side by side when space permits; empty contributors take no space.

**A figure may be drawn as well as said.** `Kpi` takes one `drawing`, or none:

- `Dots`, a part of a whole, one dot each. `Dots.of(filled, whole)` when the
  parts are only a count; `Dots((Dot(...), ...))` when each is something with a
  page of its own, which then names itself when pointed at and links there.
  The figure's own link moves from the cell to its value, since a link cannot
  hold links. Past six, dots stand in even rows at most four wide; where the
  column has no room for them beside the value they take the line under it,
  so a figure's dots never reach the figure beside it (the layout gates'
  `nothing_is_drawn_into_its_neighbor`). Past a dozen, dots are a texture and
  are not drawn. A history is
  `Dots(..., ordered=True)`, drawn in its own order, oldest first, as the HQ
  page draws a piece of scheduled work's recent runs, each linking to its job.
  `partials/_dots.html` draws either, wherever it stands.
- `Trend`, recent readings oldest first, drawn as a line where the card has
  room, each reading saying what it is when pointed at.

and an `icon`, a mark from `partials/_icon.html` before the label, on figures
only: headings carry none. A drawing repeats what the value and its note
already say, so the words stay, and one that is only decoration is not
announced. Nothing is drawn against a measure the domain does not have: a date
that runs out shows the date, not a bar across an invented span.

In a card, a row of figures shares its lines: labels start level and values
stand level however a label wraps, and a label wraps beside its mark, never
under it.

**A saved copy never says "you" about someone else.** Anything that describes
the reader's own connection includes `partials/_example_connection.html`. On a live
HQ it renders nothing; on a saved copy (`SEVERINO_RENDERED_COPY`, set only in
code by whatever renders the copy, as the demo does) it marks the line as an
example connection, not the reader's. *Gate:* `hq/platform/core/tests/test_example_connection.py`, and
`demo/check.py` on the built demo.

**The dashboard says how you reached it.** Beside the at-a-glance readings
sits one for the request itself: the channel, the path, the encryption and
whether the admission checks hold. It is the same reading the connection
panel opens on and a link to that panel, so it adds no second account of a
request and works without script. It is rendered with the page rather than
fetched after it: a line that arrives late is a line that visibly changes.
Beside it, whether the controller is still arriving: every reading on the
page is the controller's, so one that has stopped leaves them all as they
were. Nothing on the dashboard is fetched after it has rendered unless
somebody opens it, or a reading its queue is drawn from is due (below).

**A page about one thing asks for that thing's readings.** A machine's page,
a service's page and a record's page include `partials/_visit_refresh.html`, naming which page
they are; the dashboard includes it only when a reading its queue is drawn
from is due (`visit_refresh.PAGES`). Once the page is showing, it posts that, and HQ works out which kinds
the page is assembled from and asks the controller for those whose last attempt
is older than their cadence (`hq/platform/application/freshness.py`). Opening a page never
logs in to a machine: a kind whose reader opens a shell is left to the sweep.
A page in a background tab asks for nothing, a GET never does, and the request
cannot name a connection or a kind. When the reading lands the page loads
again, once; or, if somebody has touched it or it reloaded a moment ago, it
says a newer reading is in and leaves showing it to them.

**Asked-for work is an ask.** A button that starts work which outlives the
request is `partials/_ask.html`, from an `Ask`
(`hq/platform/application/asks.py`), and stands where a `PageAction` does,
in a page or panel head: its note takes a line of its own under the row its
button stands in, reserved before anything is said. In a table's row that
line is as wide as the button, so there it says the state's word alone
("Waiting", "Failed") and the sentence is the button's title, and the row
keeps its height. Pressing it posts; the request stores the ask and answers at once; the
control then says how the work stands. The note beside the button is a live
region (`role="status"`) that changes only when the state does, and the elapsed
time ticks next to it outside the region, so nothing is read out every second.
While its own work is live the button is `aria-disabled`, not disabled: it
keeps the focus the operator just gave it and ignores a second press. When the
work ends the part of the page named by the ask's `refresh` selector is fetched
again and swapped in place; without one the page loads again, though not under
somebody's hands and never twice running. A failure is said in the note, where
it was asked. In a table's row the note says the state's word alone, so the row
keeps its height, and the line the row opens to says the word and the sentence
under the button's label, so a reader with no title to hover still reads why.
Without script the same button is a form post back to the page,
which draws the control from what is stored, so a page loaded while the work is
live says so and resumes following it. A job's panel (`_job_progress.html`) and
the readings a page asks for when it opens (`_visit_refresh.html`) are the same
ask drawn differently; one behavior in `ask.js` follows all three.
A link to a command (an `ActionLink`) is drawn by `partials/_action_links.html`
through `_page_action.html`, and one whose command would refuse now carries
the refusal: it stands as an `aria-disabled` control, focusable, with the
reason as its title, never a link or a form.
*Gates:* `RequestNeverWaitsTests.test_asked_for_work_is_followed_by_one_script_behavior`;
the layout gates' `asks in rows`, which has every ask in a table's row say
the longest thing an ask says and fails a row that grows;
`ComponentLayoutTests.test_a_rows_ask_says_its_sentence_on_the_line_its_row_opens_to`;
`hq/platform/application/tests/test_refused_links.py`;
`hq/platform/core/tests/test_action_budgets.py`.

**A read is a fragment.** A part of a page that is fetched after the page,
refreshed in place or asked again is a *region*: an element that says where it
comes from, and nothing else. `static/js/fragment.js` is the one script that
fetches a part, parses it and puts it in place, and the one timer that asks
again.

| On the element | Means |
| --- | --- |
| `data-fragment="<url>"` | a region, and the address that answers it; empty for the page it is on |
| `data-fragment-name="<part>"` | the part asked for by name, sent as `X-Fragment` |
| `data-fragment-load` | a placeholder, fetched when the page is up or when the disclosure or dialog around it opens |
| `data-fragment-poll="<seconds>"` | asked again for as long as the server keeps drawing the attribute |
| `data-fragment-part="<key>"` | inside a region: replaced one by one, and only when changed |
| `data-fragment-links` | links and forms inside that lead to the region's own page are answered in place |
| `data-fragment-history="replace"` | an in-place answer updates the address bar |
| `data-fragment-failure`, `data-fragment-fallback` | what the region says when it cannot be had, and the page that answers the same thing |
| `data-fragment-target="<id>"` on a link or form | its answer belongs in that region; empty for the region it is inside |

On the server a part is `{% partialdef name inline %}` in the page's own
template, so the page and the part are one definition. Any `PageMixin` view
answers a request that names a part with the part alone
(`hq.platform.application.fragments`); a view that does not know the name
answers the page and the region is found in it by its id, so a region never
depends on its server having been taught about it. A view may compose only
what the part shows (the dashboard does for its calendar and links). A part
whose inputs are known by revision answers an unchanged question with 304.

Pressing a control moves nothing else: its feedback is drawn in a line already
reserved for it, never inserted beside the button or drawn over the page.

A swap keeps what the reader did: the keyboard stays on the control that was
pressed, a disclosure with an id stays as it was, the page does not scroll,
and the region is `aria-busy` while it waits. A newer question for a region
ends the older one. A question that fails falls back to the link or form it
enhanced; a post is never sent twice, and says what went wrong beside the
form. A session that ended says so and offers the way back in. A hidden tab
is not polled, and a region that fails is asked less often.

Script that needs a part calls `hqFragment.swap(region, {url})`; script that
needs to ask again calls `hqEvery(work, {ms})`. After a swap the new content
hears `hq:fragment`, which is where a behavior binds to it.
*Gates:* `lint:js` (no other script fetches, reads a response as text,
parses markup or keeps an interval, and fragment.js does each once);
`FragmentPrimitiveTests` (every named part is defined);
`FragmentBrowserTests` (browser).

**Moving between pages is not a flash.** A same-origin navigation fades the
new page in under a header that does not move (`@view-transition`, with the
header as its own unanimated layer), in browsers that have it and for readers
who have not asked for less motion. Every page names one set of speculation
rules by header: a link to a page of HQ's is prefetched when the pointer or
finger goes down on it, so the document is arriving while the click completes.
Prefetch only, never prerender, and only on press, so a page is never held
fetched and unread while the operator changes what it would show. A
speculative request has no effect: `hq.platform.core.speculation` refuses one
at every route that answers without a session, at the audit writer and at the
outbound boundary, and the rules leave out those routes and any link that is a
download, a dialog or a region answered in place.
*Gate:* `hq/platform/core/tests/test_speculation.py`, which prefetches every
page and fails on one that writes.

## Held by eye

What these say is design, judged by looking at the page at every width
`dev:look` draws. A number for any of them is a guess at taste, and it fails a
page that reads well as readily as one that does not.

Movement:

- What the server draws is where things stay: a script or a fetched part fills
  room the page already holds and pushes nothing aside. The gate holds only
  the far end of this, the browser's Cumulative Layout Shift, on the Chromium
  legs, since WebKit keeps no record of shifts.
- A note HQ shows beside something (an ask's answer, a page's readings, a
  job's note) stands in the page's flow in a line reserved for it, so nothing
  beside it moves and nothing is covered.
- Opening a disclosure moves nothing before it: the summary stays under the
  pointer that pressed it.

Tables and names:

- A table row stays a few lines tall; a cell that would make it a column of
  text gives the rest to the open row.
- An identifier that fits its line moves to the next line whole, and one
  longer breaks only at a seam `|breakable` marks, never at a hyphen.
- No identifier leaves a scrap of one or two characters alone on a line.
- A mark that closes or opens what it punctuates stays on that word's line.
- A name's parts are one run that wraps, never side-by-side items of a flex or
  grid box.
- A name in a table wide enough to hold it is whole, and a short name there is
  one line.
- Nothing is cut or held back while its table has room to show it, and a cut
  value's box ends where its words end.

Headers and asides:

- On a phone a page's lead control stays beside its title.
- A title breaks at its spaces; it is never squeezed into a column beside its
  controls.
- A heading's aside wraps under the heading, never beside it, and leaves no
  word alone on its last line.

Alignment:

- A link sits on the line of the words it continues, and an external link's
  arrow stays with its last word.
- A route is one row or one column, its hops spaced evenly, its line running
  from the first stop's mark to the last's, each mark holding its glyph in its
  middle.
- Paired sections end each row together, and so do cards drawn side by side,
  the dashboard's included.
- A band lays out as whole even rows, and each of its cells keeps its padding;
  so does any filled box.
- A form fills its rows, controls in a row share one height, a wrapped bar of
  controls fills its rows, and a section of queue cards ends its buttons at
  one edge.
- A value sits beside its label wherever both fit on a line, a figure reads
  under its label, and an identifier takes the alignment of the box it sits
  in.
- A row reads from its first line, and a table's names share one edge whether
  or not a row opens.
- A frame across the page is filled by what it holds.
- What reads as one unit (a state, a time, a part of a value, a control's
  label) does not wrap.
- The dashboard's readings are one shape: each mark beside its lead, every
  lead one size, every second line at one depth.

Decoration:

- No frame inside a frame; one disclosure to a row and one mark to a summary.
- A dividing line shows against the surface it is drawn on.
- No column is drawn that no row has anything in.
- A card ends where its content does; controls in a row are even; a tooltip
  is on something that takes focus; a column seam holds its place from band
  to band; a sentence is never set in a row of chips.

## Adding UI

1. Find the primitive. Most pages are a head, a band or card of facts, and a
   table or list.
2. If one almost fits, extend it in its sheet and its partial, with a comment
   saying what it covers and why.
3. A new container of content is a surface: add its selector to the surface
   list, write only its layout.
4. A new state of a page (empty, failing, waiting, crowded) gets a fixture in
   the browser gate, so the layout rules run over it.
5. Run `mise run check` and `mise run browser`, then look at the page at a phone,
   a tablet and a desktop width (`mise run dev:look`), against the rules held
   by eye. The gates catch classes of mistakes; they do
   not tell you it reads well. Review by walking a real workflow end to end, not
   page by page: every entity named is a link, every relationship is visible
   from both ends, and nothing shows a raw kind name, an empty table or a nag.
