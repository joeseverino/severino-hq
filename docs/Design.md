# Design

HQ has one look, built from a small set of primitives. A page is assembled
from them; it does not style itself. If a page needs something the primitives
cannot express, the primitive grows (in its sheet under `static/css/app/` and its partial),
and every page that uses it gets the improvement. A page that needs a
different look of a part gets an option on that part; a new part is added
only when the structure differs (what it is, never how it looks), as the
command palette's group labels inside its menu are. Extensions ship no CSS for
the same reason: they render host classes and inherit every fix.

The stylesheet is the sheets `hq/platform/core/stylesheets.py` lists, each
one responsibility under the file-size ceiling (`SEVERINO_FILE_LINE_LIMIT`), linked in that order. The
first declares the cascade layers (`tokens`, `base`, `components`) and the
rest fill them. Inside a layer a later sheet wins a tie, so a sheet is added
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
has for its own parts. A thing is called by its own noun ("DNS record",
"certificate"), HQ "reads" and "applies", and what differs was "changed
outside HQ". Comments and identifiers keep the internal vocabulary.

**A title is a short noun.** A section heading, a column heading, a label and
a button say the plainest name a stranger would understand at a glance:
"Devices", "Apps", "Rules", "Setup", "Permissions", "Approve". Two things on
one page never share a name. The sentence that explains it, if one is needed,
is the lede or the help text under it, never the title itself. A label (a
heading, a tab, a fold's title, a column, a row group, a dialog's title, a
button, a link to another page) is one to three words in sentence case, the
term GitHub, Stripe, Cloudflare, Vercel or Tailscale's dashboards use:
"Access", "History", "Source", "Defaults", "Installed on", "Unmanaged names".
It never opens with a question word and holds no pronoun, no
everything/each/every and no clause saying what the part holds. A name that
must break it is in `PROPER_NAMES` in `hq/platform/core/plain_words.py` with
its reason; `lint:prose` refuses the rest.

The Vale style `tools/lint/vale/styles/Plain` holds each word that is never
shown with what to say instead, and each label that was retired for one name
per thing:

| Thing | Its one name |
| --- | --- |
| the queue | "Needs you" |
| search, and its box | "Search", "Find anything" |
| a fresh read from a thing's page | "Refresh" ("Refresh all" for everything) |
| a fresh read from a problem card | "Check again" |
| a filter form's button | "Filter" |
| making a new record, on its list, its form and its command | "Add" and the record: "Add an asset" (`labels.adding`) |
| saving the form of a record that exists | "Save changes" (`labels.SAVE_CHANGES`) |
| a wait | "shortly" |

A capability and the button that runs it say the same words.
*Gate:* `lint:prose`, over the paths `tools/lint/vale/.vale.ini` gives Plain.

**A sentence says what is true, in a few plain words.** A value HQ does not
have is left out, and its row with it: never "Not known yet", never an
explanation of why. A detail is one short sentence the reader needs in order
to act, or nothing, and deleting a line is the first fix. No sentence argues
with itself, hedges, explains HQ's machinery, or narrates the page ("Open one
to see", "Each connection is a..."). `Plain.Long` refuses a sentence past
twenty words and `Plain.Narrate` the narrating lede; a file that still breaks
either is counted in `hq/platform/core/prose_baseline.py`, which only falls.

**No page writes a style.** A page's look comes only from HQ's own sheets.
The policy's `style-src-elem 'self'` refuses a `<style>` element, and HQ
writes no `style` attribute either: one would be refused once
`style-src-attr` is `'self'` too, which waits only on a WebKit defect
(`WebKitSelectCanaryTests` says when it is fixed). A value drawn to scale is
an element that carries it (`<meter>`, `<progress>`, an SVG mark placed in
percent, `partials/_allocation.html`); a script that places a part writes
through the CSSOM (`style.setProperty`), which the policy does not govern. A
browser test is answered under the same policy, and the gate lifts a style
its own fixture writes into a sheet it serves (`gate.lifted`).
*Gates:* `test_style_attributes_are_the_only_relaxation_in_the_policy`;
`RenderedPageTests` over every page and fragment, an extension's included;
the template contract, for an extension's template; every browser test,
which fails on a refusal or a report to `/csp-report/`, and
`PolicyReportBrowserTests` over the navigation.

**One frame per thing.** A box that holds content is a *surface*: one border,
one radius, one background, one shadow, drawn by the single surface rule in
`css/app/surfaces.css`. A component joins by adding its selector to that rule's list and
declares only its layout. A surface inside a surface draws no frame and no
inset: the outer one already says "these belong together". Nesting is handled
by inheritance, so nobody has to remember it.

**Size to content, fit the window.** Layouts are intrinsic: grids fit as many
columns as their content allows (`repeat(auto-fit, minmax(min(100%, X), 1fr))`),
rows wrap (`flex-wrap` with a basis), text wraps at its box's edge. When a component
must change shape, it asks its own container (`@container`), not the screen,
so it reads the same in a narrow card as on a phone. A viewport breakpoint is
for what the viewport decides (the header, a full-screen modal) and says why
in a comment. Content uses the window up to `--content-max`; prose fills the
card or section it sits in: no text stops at a reading measure (`lint:css`
refuses a measure token and a width cap in `ch`). Never make an element holding buttons a size container: Chrome
restyles its contents once the container is measured and runs their
transitions from nothing, so every button fades in on load.
*Gates:* the overflow sweep at a phone's width (402), 820 and 1360
(`WIDTHS` in `hq/platform/core/tests/browser/widths.py`);
`test_viewport_breakpoints_only_go_down`, a ceiling that only falls.

**A table fits its box, at every width.** No screen width is asked anywhere
in the table primitive. A table takes its box's width and wraps, the browser
sizing each column from what it holds. With no script nothing leaves: in a
phone's region the table takes every give below at once, and it scrolls in
its region only once it has none left.

Each column that may leave has a rank, 1 first
(`{% table_heading ... drop=N %}`). A list's ranks are derived by `ranked`:
`optional-col` first, then from the far end. It never ranks the column that
names the row, one marked `key-col`, or the closing figure of a table that
marks none.

Only the browser knows how wide a table is, so `tables.js` decides what shows,
the established responsive-table pattern:

- While a table is wider than its region, its next ranked column leaves (rank
  1 first, the rightmost of a rank first, `data-dropped` on its cells).
- Once none is left to go, the table is in a narrow box (`data-narrow`), then
  tight (below).
- As the region grows they come back, the last to leave first, each only if
  the table still fits.
- What leaves is in the row's toggle.

A ResizeObserver on every region does it in the frame the table is laid out,
before that frame is painted. The script loads in the head without `defer`,
so every region is watched from the moment it is parsed and nothing moves once
a page is drawn.

The columns showing set `--showing`, which bounds what a held line asks for
(`--hold`).

In a narrow box (`--box: narrow`, else `whole`, read by `@container style()`)
a table gets roomier rows and holds back what is marked `narrow-more`. What is
marked `narrow-only` shows there alone: a few words in the cell that names the
row, standing in for the columns that left (the audit log's action under its
name).

A table still wider in a narrow box is tight (`data-tight`). Its headings,
times, statuses and a value's parts wrap at their spaces, the name gives up
the room it holds out for, its row's controls stand one above another (only
where the script measured the table), each label whole on its line, and its
cells stand closer. Still wider, its identifiers break where they must (`data-break`).
A table wider than its region even then scrolls in it (`data-scrolls`), so no
table runs past its card at any width, and the gates fail it: the narrowest
phone (320px) and every width above it hold every table the gates draw.
`a_table_fits_its_box_at_every_width` and the example estate's
`ExampleEstateTableFitTests` sweep each page from 320px up, reading it at the
first width of each shape its layout takes (`TABLE_LAYOUT`), where a table has
the least room.

The region marks each body cell that says nothing (`data-blank`) and every
cell of a column blank in every row (`data-blank-column`), which is not drawn.
Each cell of a column whose heading is `numeric` carries that kind too, so a
figure aligns under its heading on fixed digit widths with nothing written on
the cell.

Since a table fits, its region is no scroll box and its heading pins under the
page's chrome (a scroll box in either axis would pin it to the region). A
table that scrolls once it has given all it can (`data-scrolls`) is in such a
box, so its heading sticks to nothing and stays on its first row.
*Gate:* `test_a_rows_controls_stay_in_its_card_and_a_table_that_cannot_fit_scrolls`.
Only a region declared a pane, or a chart's (`chart=True`),
scrolls, pins its heading to its own edge, and takes focus: it is named, so
the stop says what it holds, as the focusable scroll box of the usual
responsive-table pattern is. It is a `group` where that pattern writes
`region`, and a region that fits is a plain box, so neither is a landmark:
the section or card around a table is one already, often by the same name.
*Gate:* `test_a_section_and_the_table_in_it_are_one_landmark`. A pane
(`pane=True`) has a height of its own, as a chart's data or the topology
ledger has, or is a table sized to its content. A table never stacks into
cards and is not swiped through.

A key/value table (row headers, no `thead`) sizes its label column to its
longest label.

Rows that belong to sections share one table as a `tbody` per group, headed by
`{% row_group label count %}`, which spans every column the headings declare.
They are never two tables kept aligned by hand.

Rows that belong under one row name it (`data-folded`) and show while it is
open (`data-folds`, its toggle in the same gutter as the row toggle); with no
script they all show. A group folded away is `row_group ... folds=<id>`, its
rows naming that id.

A row reads from its first line: the toggle stands there, so every cell is set
at the top, and no page sets its table's cells otherwise. What can be done to
a row's record ends the row as `{% row_actions actions %}`, each a
`PageAction` drawn as a compact control with its middle on the first line's,
under a heading of kind `control`; a page never draws a button in a cell
(*Gate:* the survey's `actions on the first line`, and the
`hand-drawn-row-actions` rule). A row's controls stand side by side on one line, which their
column asks for, and wrap only in a tight table, where every kept line does
(*Gate:* the survey's `row actions stacked with room`).
*Gates:* `a_table_fits_its_box_at_every_width`,
`test_a_blank_column_is_not_drawn_and_a_short_word_is_not_split`,
`only_a_table_scrolls_sideways`, and the page crawl, which fails any
`.data-table` outside `.table-scroll`.

**Opening a row moves nothing.** What a row holds back (the columns that
left, the `narrow-more` parts and the `row-below` blocks) is in a line of its
own beneath the row. The server draws it after the row (`tr.row-detail`,
`table_tags._detail_row`), and it shows while the row is open: the blocks
first, then each part under its column's name (`dl.row-columns`).

- A column's part shows while the column is gone (`data-dropped`, which
  `tables.js` sets on it as on the cells), a narrow part in a narrow box, a
  labeled block always. A line with nothing to show is a blank row of no
  height.
- The line holds a copy of what the row holds back, so the row's own cells
  stay as they were.
- The browser's find reaches a closed row's line: `tables.js` hides its parts
  `until-found` (never the server, since a browser without it would hide the
  line for good), and a match opens the row through `beforematch`.
- The columns are held at their widths for as long as any row is open, so
  every other row keeps its place. A table with a row open, however it was
  opened, is fitted as it would stand with its rows closed and held there, in
  a table without headings too.

Every row that can open has its toggle drawn by the server. The region draws
it, last in the row's cell under the heading that keeps the gutter
(`table_heading ... gutter=True`), and marks that column's cells `row-gutter`;
a toggle written by hand there is refused. Only a table with no headings to
name its gutter draws its own (`{% row_toggle %}`). It shows only where the row holds something back now: a
column that left with something in it, a `narrow-more` or `row-below` part, a
fold, or a value `tables.js` found cut (`data-cut`).

The toggle stands in a gutter that every row of the table keeps while any row
opens, so names share one edge whether or not a given row opens. The gutter
follows from the server's markup and the box's width alone. A cut value shows
its toggle only in a gutter already kept, so nothing the script marks moves a
name.

In a narrow box every table whose rows the server made openable keeps its
gutter, since touch has no hover title and a value cut there must still open.

With no script a toggle opens nothing, so nothing is held back for one: every
column and part stays, and the table is sized to its content inside its region.
*Gate:* `test_a_wide_table_fits_a_phone_and_opening_a_row_moves_nothing`.

**A row's editor opens under it.** A row edited on the page holds
`{% row_editor form key label=... action=... verb=... field=value %}` in a
cell: a link to the page with that row's editor open (`?edit=<key>`, the
other query kept). On that page the region moves the form (`.row-editor`)
into the line beneath the row and draws the row open, its toggle saying so,
and with no script that line shows, since no toggle can open it. The view
declares one form for the table; it is drawn for the open row alone, with the
values the cell gives by field name, or as it was sent when it is bound, and
its ids carry the row's key, so none is drawn twice. A region that holds an
editor's link is a region of the page (`data-fragment`, its id from its
label), so with script the link, the form and its Cancel are answered there
through `fragment.js`'s own paths; the form posts `next`, the page with the
editor closed.
*Gate:* `hq/platform/core/tests/test_row_editor.py`.

**Every table sorts.** A list its server sorts has links in its headings
(`table_heading sort=`). Every other heading has the same control, looking
the same, drawn as a button, which the region keeps only where the table
holds two rows to put in order, counted across its groups. A column of
controls and a heading named for assistive technology alone have nothing to
order, and `local_sort=False` keeps a table's own order, as a list page does
with the one page of its server's order it holds. `tables.js` sorts the rows on the
page: ascending, descending, then as they came. Amounts, dates and lengths
of time sort as what they are; a cell may say its own value with `data-sort`.
Sections keep their own rows. No table is drawn by hand, an extension's
included: `hand_drawn_tables` in `plugin_testing` refuses one, and the host's
suite runs it over every installed extension's templates.
*Gate:* `test_any_table_sorts_by_a_heading`,
`test_every_table_is_declared_through_the_tags`.

**A table is laid out before any script runs.** Nothing is measured to lay
a table out. `tables.js` asks only whether a value is cut (`data-cut`), which
shows a toggle in a gutter already kept, and holds the columns still while a
row is open or the rows are refreshed.

Text is cut in one sheet, `css/app/cuts.css`, in one of two ways that each
show the reader there is more: on its line with an ellipsis, or at its last
line by a clamp. A value that may be cut has its whole rule there, so it is
styled in one place. Every other text wraps. A box that
clips something other than text (a frame's corners, a scroll region) says why
beside its declaration.
*Gate:* `lint:css` (`cutText` in `tools/lint/stylelint.config.mjs`), and the
layout survey's `cut and closed`, `cut when open`, `cut with room` and
`cut beside room`.

A long identifier is one line, cut with an ellipsis at a share of its region's
width (`--identifier-max`, in container units between a floor and a ceiling).
Its whole value is in its title and in the open row.

A region whose rows leave an identifier less of the row names it with `room=`
(a list view says it as `table_room`):

| `room=` | When the identifier sits beside | What it gets |
| --- | --- | --- |
| `shared` | wide columns | little until the region is wide |
| `snug` | other identifiers | each is held near one short length |

An identifier the row is read by, in a row of many columns (a container's
image), is `code.cell-whole`. It wraps at its seams instead, so its column
gives way without hiding any of it.

Outside a narrow box a status keeps its line and a one-token name is never
cut. A value told in several parts marks each `cell-part`, and where its line
is short it wraps only between them. The region wraps what a status or figure
cell says in one `.kept-line` for the stylesheet to bound. A description cut
to a preview is a `.table-clamp`.

A few words that read wrong split (an age, a hyphenated word) are one
`.whole`, the box `|breakable` gives each part of a name, or a `.nowrap`; a
line that must stay one line (a commit's message) is a `.one-line`, cut with
an ellipsis. A key to press is said in a `.keyboard-only`, which a touch screen
hides; `.visually-hidden` is the other way round, read only by a screen
reader.

Every such hold, and an identifier's cut, asks for at most its column's even
share of the region (`--hold`). The share is counted from the columns the box
draws, the naming column's half a share more. So the holds of a table never
sum past its box: what fits its share stays whole, and what does not wraps or
is cut.

What each part of a cell is decides how it wraps, and the region marks it
(`data-cell`, `table_tags.mark_cells`), so the stylesheet keys its cuts,
holds and wraps on that mark and never on a page's class or a cell's tags.
The bold a cell leads with, or its leading link's, is the row's `name`; free
text (a message, a record's words) is declared `cell-data` and marked `data`;
what a row means in words is declared `cell-prose`. A context line is the
shared `.muted`, and a row's name set in one line with its controls (a star,
a move) is a `.name-line`, where the name is cut rather than wrapped. A table
written later sets as every other does by writing those, and
`lint:css` refuses a table primitive rule that tells a cell's part by
anything else. Free text breaks a run with no seam in it (a link, a digest)
wherever it must, so it never holds its column wider than its box. An identifier in a prose column
waits for the open row (`row-more`) rather than holding that column at its cap.

A run of links longer than the count its template declares folds behind "+N",
and a stacked list longer than four shows three and "+N more". A row that
holds anything back gets one toggle that lifts every cut and opens every fold
in it.

An open row and the line beneath it hold every box in them to its cell, in
one rule of the table primitive (`surfaces.css`): no box asks for a width of
its own or grows wider than what holds it, and a chip is a box, its padding
inside that width. So nothing there runs past or over a cell at any width,
whatever a page's own rule says, and no page sizes a box inside a table:
`lint:css` refuses a width, an inline size, a flex basis or a grid's columns
on a selector that reaches inside one (`.data-table` and a combinator, a
cell, `.row-detail`, `.row-columns`) in any sheet but the table primitive's
(`base.css`, `cuts.css`, `figures.css`, `tables.css`, `surfaces.css`).
*Gates:* `cells` and `cell labels` in the layout survey, read with rows open
by `dev:look --expand`; `test_stylesheet_lint`.

An intrinsic size (`max-content`, `min-content`) goes on an element inside a
cell, never on the `td`. Chromium ignores it on a table cell and WebKit does
not, so the two engines would size the column differently.

Outside a narrow box an identifier in a cell is one line, never broken at a
seam. One that breaks only at its seams (an address with a port, a host name)
holds its whole length, so its table lets a ranked column go before the name
would break. A name the cell cuts shows one line, cut. A narrow box has no
column left to let go, so there it wraps at its seams. The row's own name,
free text, a preview and a context line wrap as they always do.
*Gate:* `broken identifiers` in the layout survey,
`test_an_identifier_in_a_table_is_whole_while_a_column_can_leave`.

A box set in a line that cuts what it holds (an inline block whose overflow
is not visible) takes its bottom edge as its baseline, so its words ride
above the line's; it is set by its top (`vertical-align: top`).
*Gate:* `raised text` in the layout survey.

A row's opening is its only disclosure. What is shown only once a row is open
is marked `.row-more` (a word, a list item), or `.row-below` for a block,
which opens on the line beneath the row at the table's width. The face that
stands in for it while closed is `.row-less`. Never a `<details>` inside a
cell.

**One band.** A row of readings or choices is a band: one surface, cells
divided by hairlines each cell draws itself. So a wrapped row keeps its
dividers at any width and a short last row ends in plain surface.

A band of a fixed count lays out as whole even rows (never 3+1). A band counts
its own cells (not a dialog a cell opens, not its note) and takes the tracks
for that count, two to six (`--tracks-2-1` up to `--tracks-6-3-1`). A lone
cell takes the band, so no page says how many it holds.

A band is listed once, in the band rule in `css/app/figures.css`; its cells
keep their own padding. Too narrow for two cells, a band reads as a list:
label left, value right.

A band of facts draws no rule between them and counts nothing. Its facts
share its width in equal tracks, as many to a row as keep each at least
`--fact-min` wide, so a few facts spread across a wide card rather than
collecting at its start beside an empty end; past that they wrap. That floor
lets a narrow band hold fewer facts to a row, and the facts of a row still
share their lines. *Gate:* `facts across their band` in the layout survey. Standing alone the band is a surface with its own inset. In a card or a
section, the frame's padding is its inset. Under 27.5rem it reads as a list,
each fact laying out its own rows.

Every label and its value is a fact: `{% fact "Label" value=n url=... detail=... %}`
inside `{% facts %}`, or the block form `{% fact "Label" %}markup{% detail %}lines{% endfact %}`
for a value that is markup. A band (`{% facts layout="band" %}`) is the few
facts that sum up a page or a part, each a cell with its icon, its label over
its value over its detail; the facts of a row share their lines, so every
value stands on one baseline, a zero beside a linked count alike. A list, the
default, is the facts about one thing, each label beside its value, its label
column as wide as its longest label, so a few short pairs never spread across
a wide page. A label with several values holds them as a list inside one fact.
An identifier in a value (code, a `|breakable` name) runs inline, code a chip
on each line it wraps to, so a value's first line stands on its label's line
in every engine. A list opened to read more (in a disclosure, a popover, a
dialog) or set above what a form changes sits closer and ends with what holds
it; that is where it stands, never an option, so the same summary drawn on a
page and in a dialog takes each one's spacing.
A line (`{% facts layout="inline" %}`) is a card's summary: each fact its bold
value, then its words ("3 rules"). Its facts stand a fixed gap apart, under a
rule when they follow the card's head. Its labels read on from the value, so
they are written in lower case. A fact with an address is a link whole.
A fact's detail is running words. Its lines are the ones its markup declares:
a `<small>` each, or a `detail=` given as a list. What a fact holds past its
lines (its record list, a disclosure of what runs now) follows them in the
detail, so a page's summary cards are a band of facts too, never a grid of its
own.
A fact drawn by hand (a page's own band) is refused by the
`hand-drawn-facts` rule, and so is a figure set bold apart from the words that count it.

What names a value (`.fact-label`, a tile's label, a `<dt>`) is sentence case
in the muted voice. Capitals are for a kicker over a group (`.eyebrow`), never
a value's name, a state or a tag; `lint:css` refuses a change of case
anywhere but a kicker, whose rule says what it heads in its disable comment.

A list of short steps (`.step-list`) is read down its columns, so its columns
end within a step of each other. Each step is `{% step icon=... %}`: its icon,
then what it says; `tone` colors the icon by how a check stands, `done`
draws a finished step quiet, and `title` is its tooltip. The
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

**A form's fields take as many columns as fit.** Fields flow in reading order
across as many columns as hold a field of 18rem, so a wide frame gains columns
rather than stretching each input, and a phone's is one. A textarea, a group
of choices, a field marked wide, errors, a command's confirmation and the
actions take the row. A group of checkboxes reads across. A command's form is
this grid too: `.form` is the one form layout.

**Every form a page is about is drawn one way.** A page that is a record's
form or a command's is the frame `form_page.html`: one card spanning its
column holding one partial,
`partials/_form.html` (its token, errors, fields and closing row; `fields`
names a template for fields beyond the form's own loop, `method="get"` makes
it a query with no write token, `bar=True` pins the
closing row to the screen's foot once the form has changed, drawn in its place
from the first draw so nothing moves), the form's errors above
the fields (`partials/_form_errors.html`), each field with its help under it
(`templates/django/forms/field.html`), and one closing row
(`partials/_form_actions.html`): the primary verb, Cancel, and a note on what
saving does where the page has one. A field's help is its command's meaning
(`records.described`), so the form and the command page say the same; a form
writes its own only for what it alone asks. The view supplies the verb and
the way back, never the template, and a record's form page is the frame with
nothing of its own (`form_fields`, `form_note` from the view).
A form whose verb can't be pressed because its connection can't make the
change passes `gap`; the closing row draws the verb unusable and what it
needs beside it. A form a script or test finds names its `data-` attributes
as `hooks`. A form answered in another region's place gives that region's id
as `in_place`. A toggle that posts is a `post_button` with `pressed`.
*Catalog:* `form/write-gap`.
*Gate:* `hq/platform/core/tests/test_form_actions.py`, which reads every form
template, and `test_form_page.py`, which holds every routed create and edit
view to the frame and the card to it alone.

A form that asks one question, one visible field of a line (words, a number,
a date, one choice) closed by its verb alone, draws its button on the field's
line where the field keeps 18rem beside it and under it where it cannot, as a
search or lookup form does. The partial derives it (`asks_one_question`, then
`.form.is-question`), so no page flags it.
*Catalog:* `form/lookup`, and `form/lookup-help` with its help and an error.

**A choice that has a consequence says it.** A field whose few answers each
change something different declares `ChoiceCards(described=...)`
(`hq/platform/application/forms.py`): each answer a card of its radio, its
few words as the radio's name and what choosing it does as its description,
the whole card pressable. A number with a unit declares
`SuffixedInput(suffix=...)`, the unit beside the box. Both draw through the
form's own loop, so no page writes their markup.
*Catalog:* `form/choice-cards`.

A form whose fields stand one to a row of a table (a group chosen for each
record, a verdict for each request) is the same partial with `table_body`
naming the template that draws the table, in its `table_region`. The form lays
out no fields of its own, so it adds no box: the table reaches its section's
sides as a table section's does, and the closing row stands under it. The form
declares one field for each row, named by the row (`group-<id>`), so each
control's name and id are its own and a row moved between two drawings still
posts its own value, where a formset's names follow each row's place. Each
row's cell draws its field with `partials/_row_field.html`: the control, its
label for a screen reader alone (the column's heading names it on the page),
and its errors under it. A row's own buttons are `post_button`s, which post
the page's own form, so they sit in this one.

**Nothing empty takes space.** A panel with nothing to show is not drawn: the
page closes up rather than carrying a card that says "nothing here". An empty
list says whether its own search or filters emptied it, and offers the way
back. A page with nothing yet says what is missing and, where it can, what
would fill it and where (`partials/_empty_state.html`: `message`, `why`, an
action); controls that only make sense over data wait for the data. An
absence where a value would stand (a fact, a cell, a row) is the same part
with `inline=True`: its words, quiet, in the line that holds them, with no
frame. A
`{% table_region %}` whose tables hold no row draws that card in their place,
so no page draws headings over nothing: its `empty=` says what is missing in
the page's words, with `remedy=` (or `action_url=` and `action_label=`), and
`filtered=` with `clear=` says its filter emptied it, with the way back.

**One head.** `partials/_page_head.html`: trail, title, controls, lede, laid
out the same at every width. The title and its controls share a row (the title
wraps before the controls move); the lede runs the full width under both. Two
controls stand beside the title at every width, the lead and the next the page
names, and the rest go behind the overflow menu. A head too narrow for three
(a container query on the head) folds the second in too. The lead is chosen by what each control is for, never by where it stands: not
a destructive one while another is offered, then one that can be pressed, then
the page's primary (`head_actions`). A destructive verb (a stop) is a link to
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

**A check's title states the fact it checks.** "Tokens hold only the permissions HQ
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
frames what it encloses (`partials/_card.html`). It draws a section unless
`element=` names an article, figure, details or div. `heading` is its
heading's id, or `label` its name when it has no heading; `busy` marks it while
its content is still coming; `alone` makes it the whole of a page that stands
outside the page frame (the sign-in): centered at a dialog's width, a picture
leading it over its own title, and an action alone on its line the card's
width (*Catalog:* `part/card-alone`); each `data_*` option is a `data-`
attribute (True draws it bare). A state of the
card is a `data_*` option the stylesheet reads, never a class written inside an
`{% if %}`. No template writes `class="card"` itself
(`test_only_the_tag_draws_a_card`). A card is never wider than the column or
track it is placed in (`.card { min-inline-size: 0 }`), so on a phone what it
holds wraps inside it.

**A section is drawn by `{% section %}`.** `{% section "Agents" count=n lede="..." %}body{% aside %}a line, a link, a control{% empty %}No agent has connected yet.{% foot %}How to read the body.{% endsection %}`
(`part_tags.py`, drawn by `partials/_part_section.html`) is a titled part of
a page: a `<section>` labeled by its heading, its head on the page (the
heading with its count standing in it, the lede under it and the aside
beside it), then its body in the frame, and after the body its foot: the
line that says how to read it (what a figure means, where the numbers come
from), quieter than the body, and drawn only with one. The heading's line is one control
tall, so a control beside it adds no height and heads in one row are one
height. The aside is one box at the row's end, so its parts (a count, a dot, a link) wrap as one line of text, and it
keeps to one line while the row has room for it. What draws its own edges,
cells or fill (a table's region, a band of facts, a row of metrics, a month's
grid, a list whose rows carry their own padding) fills the frame with no
inset and no second border, as a card's flush list group does in Bootstrap;
prose, facts and the empty line sit in the frame's padding.
*Gate:* `section asides` and `inset in the frame` in the layout survey. With no body it draws its empty part as the empty state's line,
followed by the link that fills it when `remedy_label` and `remedy_url` name
one (`{% section "Receipts" remedy_label="Upload one" remedy_url=url %}{% empty %}No receipts.{% endsection %}`),
and with neither it draws nothing. `more_label` and `more_url` end the aside
with the quiet link to the whole of what the section shows part of ("View
Estate", "All runs"), after a dot when the aside says something too; with no
address the link is left out, so a page passes the one it has. `figure=True`
draws the section as a `<figure>`, its head the `<figcaption>`, for a drawing
(a chart, a calendar) whose title, line under it and legend are a section's
head like any other. A section inside a section heads itself a level
down inside the outer frame, and so does one inside any other frame (a card,
a form, a band's cell): the frame's `--surface-inset` is 0 there, so its head
draws no rule and its body adds no frame or inset of its own. A part that
draws its own title (a dialog, an insight card) wraps what it holds in
`{% headed %}…{% endheaded %}`, so a section inside heads itself a level under
that title. A `<pre>` in a section's body wraps and scrolls only in height. Two of one
heading name their own ids, `id` is the section's own (a link to it, a
region refreshed in place), `fold=True` draws it closed with its head as its
toggle (`open`, any truthy value such as a form's errors, starts it open), and `css` and `data_*` are the section's, on a
landmark that never pads itself, whatever `css` names: the frame holds the
padding. A section draws its head inside its frame, over a rule, unless its
body is one table region and nothing else, whose head stands above the frame
so the table reaches its edges (`:has()` in surfaces.css decides; a page picks
nothing). `head="above"` or `head="inside"` overrides it for a page that
needs the other look. Frames that share a row start together, so in a pair
(`.two-col`) a table section beside a section whose body is anything else
takes its head inside its frame; alone, or beside another table, it keeps its
head above. A
heading is plain words, never markup a filter draws (`|when`): `SectionPart`
refuses markup in it, and a template meets that only as it renders. A
heading that names a moment gives it as `moment`, in the `when` form
`|when` takes, and the part draws the `<time>`: after the heading's words,
quieter than them (`{% section day.name moment=day.day when="day" %}`), or
as the whole heading when it has none (`{% section moment=month
when="month" %}`). A page built in code builds the same part as
`hq_sdk.ui.SectionPart`, its body drawn from `template` and `context`, and
writes `{{ part }}`. The part is the head, the frame and the spacing between,
so a page states the content and never the spacing, the frame or the count's
look.

**A notice is drawn by `{% notice %}`.** `{% notice tone="warning" title="2 jobs failed" %}<p>What to do.</p>{% endnotice %}`
(`notice_tags.py`, drawn by `partials/_notice.html`) is every banner and
message a page shows: a callout in its tone, for what needs saying before the
content it is about. `tone` is one of `info`, `success`, `warning` and
`error`, Django's message levels, so a message's `level_tag` is its tone as
it stands; with none it is neutral. An error is an `alert`, which a screen
reader says at once, and any other a `status`. `title` is its first line, in
bold; a title the page composes (words around a value, a loop) is the
`{% title %}` part, written after the body
(`{% notice tone="warning" %}<p>Pick another.</p>{% title %}{{ name }} cannot be used{% endnotice %}`).
`css` and `data_*` are the page's, as a section's are. Among the page's
messages (`<ul class="messages">`, base.html) the same part is a toast, set
closer; it is never a second part. Drawn with no body it is the frame a script
clones and fills: base.html holds one per tone in a `<template data-notice>`,
so a line a script adds (`fragment.js`, `demo.js`) is the part the server
draws, and no script writes a notice's class. An error page is a page of its
own, never this part.

**An error page is drawn by `partials/_error_page.html`.** `{% include "partials/_error_page.html" with code="404" title="Page not found" body="..." search=user.is_authenticated only %}`
is the whole of what 403, 404 and 500 show, standing outside the page frame
and so titling itself: the status over the title, what happened and what to
do, and the one way back to the dashboard. `request_id` names the id the log
line for the failure carries, and `search` offers the search field.
*Catalog:* `part/error-page`, `part/error-page-request`.

**A state is a pill drawn by `{% status %}`.** `{% status row.get_status_display tone=row.status %}`
(`status_tags.py`; `hq_sdk.ui.status_pill` from Python) says the state in
sentence case (`labels.sentence`), whatever its source wrote: Docker's
"exited" reads "Exited" beside a model's "Done". Its `tone` names its color
(`good`, `attention`, `serious`, `neutral`, or a status the stylesheet colors),
and a declared resource's health state ("healthy", "drifted") takes the tone
every page draws it in. Several parts are one label (`{% status "HQ expects"
desired tone="serious" %}`); `href`, `title`, `tip`, `css` and `data_*` say
the rest. No template or module writes a pill by hand. With `chip=True` it
is one of a record's names (a technology, a domain, a role) as a neutral chip,
said as written and taking no tone, so no template writes a `.tag` by hand. With `pill=False` it
is the state's words in its tone, as a headline or a line of text says a
state, and the sentence that says what a state means takes the same tone
beside it: a record's title line is "Working · Matches HQ's config.", both
green, as parts of one `.dot-list`, so the sentence shares the state's line
where it fits and wraps under it where it does not.
*Gates:* the `status-pill-built-by-status-pill` ast-grep rule and
`test_status_tags.py`, which reads every template root for a hand-written pill
or a state's tone written as a class.

**The header is one row.** When room runs out, the search field collapses to a
button that opens over the navigation, then the sections move behind the
toggle. The navigation never shrinks below its own content, and the bar's
controls all take their size from `--control-size`.

**One menu.** `.overflow-menu` for "more actions", a section group's
dropdown in the header, the user menu and a dashboard reading's panel are one
primitive: a `[data-menu]` holding a button and the `popover` it names with
`popovertarget`. The browser opens the panel in the top layer, closes it on
Escape, a press outside or another menu opening (a menu inside another, a
section's links in the open sections drawer, leaves the outer one open), and
hands focus back to the button, with no script. The panel hangs from its own
button by anchor positioning (`anchor-scope: --menu`, page.css "Menus"):
below it from its right edge, or from its left where that would cross the
screen (`position-try-fallbacks: flip-inline`, decided against the viewport);
a section's links open from its left edge, and in the drawer beside the list.
A menu opens on a press. A section group also opens on hover where the
pointer can hover, and a press then keeps it open: WebKit has no
`interestfor`, so a few lines of app.js do that one job, and the browser
still opens, closes and dismisses the popover.
Inside a menu every control is a plain row; a destructive one is only its
color.

**A table's filter is a popover.** Its button names its panel with
`popovertarget`. The panel (`popover`, its id `table-filter-<name>`) opens in
the top layer, placed against the button by anchor positioning. It sits below
the button from its left edge, or from its right edge when that would cross the
screen
(`position-try-fallbacks: flip-inline`, which the browser decides against the
viewport). The browser dismisses it on an outside press or Escape, and the
fragment primitive shows it again after a swap that replaces it, found by its
id, so a ticked box leaves its menu open. No script places or closes it.

**An account is named by `partials/_identity.html`.** Its picture (a `data:`
URI or an HQ path, never another host's address), its name with a state
beside it drawn by `{% status %}`, its handle leading to it and a line in its
own words, as the Starred page names your GitHub profile above its counts.
*Catalog:* `part/identity`.

**The user menu says whose session it is first.** The person's picture (kept
by HQ from sign-on, never loaded from the provider by a page), their name,
what the account may do, and how long ago they signed in, in hours or days.
Then what can be set: the two switches share a line, names over tracks, and
the theme has its row. Then where the menu leads.

**One disclosure.** Every `details` shows the same mark,
turned when open. A section that folds (`section-fold`) uses its heading as the
toggle, with its count beside it; never a separate "Show all". What it holds
sits on a panel like its neighbors: a fold of one table has the table's
region, and any other fold is a folded `{% section %}`, its body in the
section's frame (the survey's "bare folds").

**About text opens in a dialog.** Long reference text a page explains itself
with (a server's instructions to its clients, a reading's method) sits behind
`partials/_about.html`: an info button that opens it in the shared dialog
through the platform's `command="show-modal"` invoker, with no script. It is
never content the page's job depends on, which stays on the page. It goes in a
section's body or a head's aside, never in a fold's summary: a button inside
the summary is a control nested in the toggle, whose role a screen reader
loses. Host-only, as the dialog it opens is. A panel a status opens in place
(an urgency's origin) is the same `partials/_modal.html` with `popover=True`:
it opens as a popover, and its close button hides it.

**One filter bar.** `{% filter_bar filtered=... clear=... %}`: the page's
search field and selects, one Filter (or the bar's own `verb=`), Clear
filters back to `clear` when anything is set, and the count of what shows
(`{% summary %}`) at the row's end. A list's table toolbar is the same bar.
The search takes the row's room; every other control is as wide as what it
says, a select its longest option. No chip walls.
*Gate:* the `hand-drawn-filter-bar` rule refuses `.filter-bar` written by hand; the
survey's "filter choices as wide as their options" finds a choice stretched
over its bar.

**Values read as what they are.** `|readout` puts identifiers (keys, hosts,
paths, ports) in `<code>` and leaves counts, words and phrases as text.

Numbers are never chips: a figure stands in a KPI cell, a numeric column or a
sentence (`hq/platform/core/tests/test_template_chips.py`). One that carries a
state says it in ink, as `.figure` with its tone class
(`<span class="figure tone-attention">`).

An address, a path or a command breaks only where `|breakable` lets it (after
a dot or a slash, before a port). It never breaks inside a hyphenated name, or
so that one or two characters of it stand alone on a line. A length of time
keeps each count with its unit.

So every value a template prints as text, in a sentence too, ends in
`|breakable` or a filter that holds its names itself (`|readout`, `|or_empty`,
`|when`). Markup built in Python holds each value it prints the same way.
`|breakable` passes a plain word and markup as they are, so it is safe on any
value (`hq/platform/core/tests/test_template_values.py`).

A seam is an empty `.seam` whose generated text is a zero-width space, never a
`<wbr>`: Chromium breaks at a `<wbr>` even under `white-space: nowrap`, and
WebKit does not.

Chromium also breaks a part held to one line (`.whole`) that has no box
(`display: contents`) at an element's edge inside it, when the box around the
part has `overflow-wrap: break-word`. So a box that cuts such a part sets
`overflow-wrap: normal`.

**A link is underlined only among words.** A link set in a sentence, a
definition, help text or a muted line differs from the words around it by a
quiet underline at rest (WCAG 1.4.1): no link color reaches 3:1 against muted
text while keeping 4.5:1 against the surface, so color alone cannot do it. A
link that is the whole of its line, row, item or chip is never underlined at
rest; it reads by its color and underlines on hover and focus, as every link
does. One rule in `base.css` draws it: words are a `p`, a `small`, a `.muted`
line, or a value or item marked `.prose` because it reads as a sentence (a
name followed by a note, a summary with a link in it), and a link with a class
of its own is a control, `.external-link` aside. A table cell's `prose` stays
on its part when the column leaves for the row's open line. The survey's
`links at rest` check reads every link on every page from the rendered layout
and names one underlined alone or among words without a cue of its own (an
underline, or a weight, size or face as axe accepts).

**A link's line runs under its name alone.** The mark a sentence puts after
a name (`breakable`'s `mark`, `{% entity link mark="," %}`) keeps to the
name's last line inside the link, and `base.css` draws it clear of the
underline. A separator between items (a dot list's dot, the meta line's) is
drawn in front of an item that holds the link, never on the link itself, and
a status tag sits beside a link, never inside it. The survey's
`lined past the name` check reads every link on every page and names one
whose drawn text ends in a sentence's mark, begins or ends with a separator,
or holds a tag.

An external link's arrow (`.external-link`) is joined to its last word by a
no-break space. A link whose name is one box (a cut token, a chip in a table)
lays the name and the arrow out as one row, since a line may break after any
box.

**A change is one primitive.** A value and the one it changes into (a running
version and the newer release, an installed package and the version that
fixes it) is `{% include "partials/_change.html" with from=… to=… %}`, with
`code=True` for identifiers and `url` to link the new value. Old value, arrow
and new value share one baseline. A narrow line breaks only between the two
values, with the arrow kept beside the new one, and a reader hears "1.2.3 to
1.3.0" rather than an arrow. *Gate:* the `changes` layout check
(`a_change_reads_on_one_line_and_baseline`).

**A mark drawn by CSS is not read.** Generated content (`content:`) says what
a reader hears after a slash: `"›" / ""` for a chevron, a separator or an
arrow, the words themselves for text that means something. *Gate:* `lint:css`
refuses a generated string with no alternative, or a space before a mark.

**A mark between words is held to the word before it.** A dot between two
parts of a line is `{% dot %}` and a link's onward arrow `{% arrow %}`, each
written straight after its part. A no-break space joins the mark to that
part and a reader never hears it, so no line starts with a mark or holds one
alone. A run of whole parts is a `.dot-list`; any other mark is the text of
an `aria-hidden` element. Text built in Python joins its parts with
`dotted(...)` (`hq_sdk.ui`), which holds each dot to the part before it, and
`breakable` draws that dot as `{% dot %}` does; markup built in Python joins
with `dotted_html(...)`. *Gate:* `test_template_separators`, which follows
every branch and include before each held mark, over the host's templates
and an extension's, and the `dot-typed-in-text` ast-grep rule over the
host's Python and an extension's `src`.

A moment, a date and an age are each written one way, by
`hq/platform/application/moments.py`: `when`, `when_day`, `ago`, `when_exact`
and `when_range`. The year is said only when it is not this one. The spaces inside a date and inside a time are no-break spaces, so
neither wraps inside itself.

A page shows them through the built-in `|when` filter, which wraps the words
in `<time datetime title>`. Its forms are `moments.WHEN_FORMS`, where a new one is added. The `datetime` is what a table sorts on, so a date
column needs nothing else.

| Write | Says | For |
| --- | --- | --- |
| `{{ at\|when }}` | "Oct 3, 9:29 AM" | a detail page |
| `{{ at\|when:"ago" }}` | "5 days ago", one unit, the exact moment on hover | a list |
| `when:"day"` | "Oct 3" | a day |
| `when:"exact"` | "Oct 3, 2026, 9:29:15 AM CDT" | the exact moment |
| `when:"clock"` | "Oct 3, 9:29:15 AM" | a list of events, where several fall in one minute |
| `when:"weekday"`, `"month"`, `"numeral"` | "Monday", "October 2026", "3" | a calendar |
| `when:"length"` | "5 minutes", either way from now | a wait |
| `when:"ahead"` | "in 3 days" | a wait |
| `\|ago`, `\|day` | the same age and day as plain words | a sentence, an attribute or a label where a `<time>` cannot go |
| `when_range` | "Oct 3 – Oct 9", a year both ends share said once | a run of days |

`when` is the only way a template writes a moment. HQ's engine
(`hq/platform/core/template_engine.py`) has no `date`, `time`, `timesince`,
`timeuntil` or `{% now %}`, so a template that asks for one fails as it
compiles.

A column's weekday heading is a label its projection writes, and a machine
value (a `?month=` address) is built by the view. Extensions get all five from
`hq_sdk.ui`.
*Gate:* `test_a_template_that_writes_a_moment_itself_fails_as_it_compiles`.

A count's plural has one owner too: `counted`, which spells a single noun
through `labels.plural` ("2 entries"). An audit type's plural is
`object_type_plural`: the model's `verbose_name_plural` where declared.

A figure and the word it counts or measures share one line, held by the
no-break space `breakable` puts between them, so the word never opens a line
alone below its number. A template never writes the word after the figure
itself: `{{ n|counted:"check" }}` for a count,
`{{ miles|floatformat:2|measured:"mi" }}` for a unit or a word the count
leaves alone (`measured:"more"`), and the value tags that say a figure in
words (`bytes`, `seconds`, `when`) join theirs.
*Gate:* the `hand-drawn-split-figure` rule, over the host's
templates and every extension's.

A bare figure counting what a heading heads (`.count`) stands in that
heading after its words: `<h2>Readings <span class="count">7</span></h2>`,
and the same in a summary, a group row and a tab. Never in parentheses, and
never alone at the far end of a `.section-head`, cut off from the words it
counts; what stands there is a line about the section, a count with its
unit ("3 events", "2 left") among them.
*Gate:* `test_every_bare_count_stands_in_its_heading`.

**Color comes from tokens.** No color literal outside `@layer tokens`, so a
theme is a set of token values and dark mode is not a second stylesheet.

**Text never fades.** An opacity under 1 is for an inactive control
(disabled, `aria-disabled`, `aria-busy`) or a mark that is not text. Text a
reader still uses takes an ink token that clears 4.5:1: `--ink-3` on a tinted
fill, `--muted` on a surface, `--code-muted` beside code.
*Gate:* lint:css runs the faded-text rule
(`tools/lint/stylelint-faded-text.mjs`), and the survey checks contrast.

**Buttons.** Primary for the one thing a page is for; default for the rest;
`ghost` for link-like actions that sit inline; `danger` in red text on the same
surface as its neighbors, loud only in its confirmation. A label keeps its
line, from the button rule alone (`page.css`); only a tight table lets one
wrap. A label too long for a phone is shortened where it is declared.
*Gate:* `test_a_controls_label_keeps_its_line_from_the_button_rule_alone`, and
the `action-intent-names-no-value` ast-grep rule.

**An action says its command's words.** What a button, link, remedy or toast
says is declared on the command it runs ([Plugins](Plugins.md), "Capabilities")
and read from there, so a page and the button it describes cannot disagree.

- A link that runs a command is `action_links.command_action`, which takes a
  `variant` of the declared words and no label.
- A finding's `FindingRemedy` says its command's words too, or its own
  `intent` where the command's name would not say what this press does ("Add
  an access list"), declared once on the remedy. An intent names no value: the
  row it stands in names the thing, and a name would stretch the button past a
  phone's width.
- A template names a button with `{% words "name" "variant" %}`, in a control
  and in a sentence that says which one to press.

A press answers "Refresh requested." (`integration_specs.requested`).
Asked-for work waiting its turn says `{% waiting %}`. A saved change says when
it goes live with `{% applies_shortly %}` (`asks.APPLIES_SHORTLY`), beside the
save button, so a field's note says only what the change does.
*Gate:* `hq/platform/application/tests/test_action_words.py`, and the
lint:ast-grep rules it names.

**A queue row says what is wrong and holds what fixes it.** The queue is
named "Needs you" wherever it appears. Every list of problems is drawn by it:
the queue page, the findings page, the dashboard's short queue, a thing's
problems, the checks a subject does not meet, the steps a request
contradicts and a domain's own attention list use one projection
(`hq/platform/application/decisions.py`) and draw it as one table
(`partials/_work_queue.html`, its rows `partials/_queue_rows.html`), so every
row's urgency, words and buttons share one edge. A page never draws a list
of its own (the `hand-drawn-findings` rule). A row reads, in order:

- the title and, only when it is urgent, the word "Urgent" after it in its
  tone (the words are `decisions.URGENCY`; a row that needs attention carries
  no mark, its word kept for a screen reader). The title states what is wrong
  and links to the thing it is about. It is what a reader presses, so it
  leads; a domain's heading, a family's name and their counts
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
to the table's. A row toggles only where opening it shows something more, and
with no script every row shows all of it. The line beneath holds, in order:

- what it means in full;
- what was read that shows it (`Insight(facts=...)`), each label once with its
  values beside it;
- what to do, where a command or an instruction is a step, one standing alone
  and several numbered. A command is `partials/_command.html`, with its copy
  button in its corner;
- the reason it cannot be done from here, as a sentence and never as a step;
- the pages that help look into it (`Insight(links=...)`), the topology's as
  icons with HQ's tooltip;
- the buttons past the first two, in one place under the rest. On a phone
  every button waits there, each the line's width, while Dismiss stays in the
  row as a mark.

The word is `partials/_urgency_pill.html`. A Cmd- or Ctrl-click on it opens
what raised it, read from the code rather than written down. That is the finding rule,
its severity and the line its detector is defined at, or the domain and key of
anything else.

A next step an owner names as a short label with a `url` is a link to that
page; one it writes as a sentence is shown as a sentence. Each action renders
once, by method and URL, through the page-action renderer and the shared POST
form, and the transport keeps the owner's full workflow. A dismissed row is
its title and the way back. A link to a row (`/action-items/#…`) lands on it
open.

Several items of one family (`Insight(family=...)`) fold under a row saying
how bad, what kind and how many, which opens to their rows: on the queue page
(`partials/_queue_entries.html`) and wherever a thing's problems are drawn
(`fold`). The findings page keeps each row its own, its filter being by rule.

Problems drawn inside a section of a page about something else (a machine's
containers, a container's checks, the request's path) are the row's dense
form (`dense`): the title in the table's own size and weight, its marks and
its buttons on one line, and what it means first in the line beneath, so the
section's own content stays what it is about. `partials/_problems.html` draws
a thing's problems this way under its urgent box and in each part's card.

A queue of three or fewer is read on the dashboard, under the greeting, as
the queue's own rows (`glance`), under no heading of their own. A longer one
is a count and a link to its page. There a row is its title, what it means,
its age and its buttons, holding nothing beneath; its title opens the row on
the page that shows the rest, steps included, or a held request's own page.

**A reading says how old it is.** Wherever a page shows what was read, its age
is `partials/_reading_age.html`, over the reading's `Freshness` where the page
has one: "Last read 3 hours ago", and "out of date" beside it past the line
`application.freshness` draws for its kind.

**A frame is filled.** A frame across the page is filled by what it holds,
or narrowed to it.

A list of facts (`{% facts %}`) is as wide as its longest pair, so its card
shares a row with the sections beside it rather than crossing the page with
its values ending a third of the way in. A record page's facts are a
`{% card %}` that opens its `.section-flow`, beside its notes and related
records. In a flow, a frame of facts grows into its row as every member does;
inside it the label column is as wide as the longest label and the values take
the rest. *Gate:* `lists across an empty frame` in the layout survey.

A row of a record list (`.list-rows`) reads as one line: its meta follows its
name a gap after, never at the row's far side across an empty middle. What can
be done to the record ends its line: `ListRow.actions`, each a `PageAction`,
drawn as compact controls, a POST through `post_button`, a glyph named aloud
by its `title`, and one that cannot be pressed as `disabled`. A row that
names a thing HQ links (a related record, a project) is given its `link`, an
`EntityLink`, in place of a `title` and a `url`, and draws it as every
mention of a thing is drawn. A project's row gives its `category`, drawn as
that category's color (`--cat-*`) before the title, its name in `detail`.
A search hit gives its `snippet`, where the record matched, as runs of the
record's own text each with whether it is the match: a line of its own under
the row, two lines at most, the match marked. It is quoted as the record holds
it, so it is the one field not held to words. Search draws every group of
results this way, the count in its filter bar, a way to each group in the
page navigation over them, and the hint before a search as a notice.

A pair whose value is a list of names (`.address-list`, or a `.code-list` of
two or more) takes a row of its own. So it never wraps beside another pair and
leaves that pair's row a gap; the layout gates' `lists of facts` fails one
that does.

A value goes under its label only where the box cannot hold both on one line.
The same holds for a node key beside its machine and a piece of evidence beside
its kind (`.conn-rows` flows each row once it is too narrow for three columns).

A thing's state sits on its title's line (`page_aside`), as words. A page about
a day (the dashboard) names it with `page_dateline`, drawn over the title where
a trail stands. A head with an aside and a line of facts (`page_facts`) stacks
its words in a column of their own (the trail or day, the title, the facts) and
stands the aside beside the whole column, as the dashboard's readings stand
beside its greeting and what needs you. A card holding nothing but a line of
facts is a strip of counts, with a narrower inset and its words a size under
its values. The facts
about it at a glance are a band under the head, as a service, a machine and
HQ's own page open. A line that dates or sources a section goes beside its
heading (`.heading-aside`), not loose under it. So does a card's way to
everything it lists ("All projects", "View Estate"): a quiet link
(`.subtle-link`) in its heading's aside, never a foot under the list: a
section's `more_label` and `more_url` draw it.

A section's heading that follows a form, a table or a list in the same box
starts a section of its own. `.section-head` clears what is above it by the
stack gap, so it is never closer to what came before than to what it heads.
*Gate:* `section heads` in the layout survey.

**Sibling sections flow.** A run of sibling sections or cards (the sections
that close a thing's page, the connection panel's identity, link and route,
a page's run of cards) is one `.section-flow`: as many to a row as keep each
at least `--flow-basis` wide (22rem), one to a row where that is the whole
width, a row's last ones sharing what it leaves and each on a row as tall as
the tallest. Every member grows into what its row leaves and none caps its
width, so every row ends at the flow's end. It is measured against the box it sits in, so the same sections
pair in a dialog and on a page. A part that needs more room before it shares
a row sets a wider basis on itself (the connection panel's route), and a run
of narrower cards a narrower one on the flow. A wide one (a table of four
columns or more, a fold, anything that is not a section or card) goes across
the row. The flow paints its members in the order the markup holds them, so
what is read aloud and where the keyboard goes is what is drawn (WCAG 1.3.2,
2.4.3): nothing reorders them. A page therefore writes its wide ones ahead of
its short ones, which collect after them to share rows rather than one
standing beside the gap a wide one leaves. Registry sections say which they
are (`ServiceSection.spans_row`), and `partials/_page_sections.html` draws one
kind per pass, `spans_row=True` ahead of a page's short sections and `False`
among them. A cell that is a state carries its tone (`Cell(word, tone=...)`)
and is drawn by `{% status %}`; a section whose rows are the latest of more
names the rest as its `more_label` and `more_url`, the heading's quiet link.
A command's page draws what the command says of itself (`CapabilitySpec.about`,
a `CommandAbout` of facts and sections) under its form through the same
parts. *Gate:* `flows in reading order` and `flows share their rows` in
the layout survey. No page
writes an auto-fit grid of its own for this: `lint:css` refuses
`repeat(auto-fit` and `repeat(auto-fill` outside the parts that lay out
something else inside themselves, each of which says so in its disable comment.

**Only what is urgent is framed.** A thing's page draws the problems about it
from `partials/_problems.html`. An urgent one is the queue's row in a box above
the page.

Anything less is one unframed line (how urgent, what is wrong, what it means,
its button). On a page made of parts it sits in the card of the part it is
about (`partials/_problems.html#lines`); one whose part the page does not draw
stays in the problems' place above the page.

The status at the top counts them by urgency ("1 urgent · 1 needs attention")
and links to the first, and a figure in the summary band takes the tone of its
part's problems.

A link to a part of a page lands below the sticky header, from one
`scroll-padding-top`. A table's row, or a control in one, lands below its
sticky heading too (`--heading-height`). The chrome a part sticks under is
only what the page draws: a page without the header has no inset.
A part that covers what scrolls beneath it (a sticky head or bar, a fade)
paints `var(--ground)`, the surface it sits on: the page's, or a frame's
inside one (`tools/lint/stylelint-ground-cover.mjs`, in `lint:css`, holds it).

**An item is open until its owner stops raising it.** Nobody closes one by
looking at it. What a reader can do with one they are not acting on is dismiss
it. It leaves their queue and their counts, stays reachable in a fold below,
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
nothing. With script it happens in place, the row becoming one quiet line with
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
- `AllocationBar`, the parts of a whole, drawn under the figure and its line
  as a bar with its legend set one under another (`partials/_allocation.html`),
  each part linking where it has a page. In a card the line and the parts
  share the line's row, so the figures beside it keep their lines level.

and an `icon`, a mark from `partials/_icon.html` before the label, on figures
only: headings carry none. A drawing repeats what the value and its note
already say, so the words stay, and one that is only decoration is not
announced. Nothing is drawn against a measure the domain does not have: a date
that runs out shows the date, not a bar across an invented span.

In a card, a row of figures shares its lines: labels start level and values
stand level however a label wraps, and a label wraps beside its mark, never
under it.

A figure that is itself a change takes `tone`, a status tone its value is
drawn in, featured or not, so a rise and a fall never read alike; a figure
that is a level says how it moved in its line, which takes `detail_tone` the
same way. `detail_url` links the line where it counts things with a page of
their own ("12 expenses"). It needs a `detail`, since a link is said by its
words, and like dots or legend rows that lead on it moves the figure's own
link from the cell to its value. The three supporting figures of a featured
row size their values to their own column, so amounts never run into each
other on a phone.

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
a service's page and a record's page name which page they are as
`page_visit` (`visit_refresh.page_visit`), and only where a controller will
read what it asks for (`cadence.ControllerStanding.takes_reads`: one whose
heartbeat says it reports to this HQ, and is not silent). Where none will,
the page draws no ask and no note, so it never promises a refresh that cannot
come. `page.html` draws the ask (`partials/_visit_refresh.html`), and its
note says what it waits for at the end of the head's first line. The note
keeps its own room there: an unseen strut of each sentence HQ writes for it
word for word (`asks.REFRESHING`, `REFRESHED`, `NEWER_READING` and
`SHOW_NEWER`, which the script writes from the page) sizes its slot, so those
sentences are always whole, the trail beside it never moves whatever the note
says, and only a failure's sentence is cut, whole in its title. The dashboard names itself only when a reading its
queue is drawn from is due (`visit_refresh.PAGES`), and the connection panel
only when the tailnet, container or posture readings it shows are due. The
panel asks once its dialog is open, and names itself as the part to refresh:
a region that carries its own address (`data-fragment`) is fetched from
there, so the panel fills in place on whatever page the dialog opened over.

Until the reading lands, a value from an older reading is said as last seen,
as of when it was read, never as how things stand now: its age is no fault.
Only a read that fails says so, in the ask's note, with why.

Once the page is showing, it posts that. HQ works out which kinds the page is
assembled from and asks the controller for those whose last attempt is older
than their cadence (`hq/platform/application/freshness.py`).

- Opening a page never logs in to a machine: a kind whose reader opens a shell
  is left to the sweep.
- A page in a background tab asks for nothing, a GET never does, and the
  request cannot name a connection or a kind.
- When the reading lands, the page's content is fetched again and swapped in
  place, the ask's own line kept. Only if that fails does the page load
  again, once; if somebody has touched it or it reloaded a moment ago, it says
  a newer reading is in and leaves showing it to them.

**Asked-for work is an ask.** A button that starts work which outlives the
request is `partials/_ask.html`, from an `Ask`
(`hq/platform/application/asks.py`). It stands where a `PageAction` does, in
a page or panel head. Its note takes a line of its own under the row its
button stands in, reserved before anything is said.

Pressing it posts; the request stores the ask and answers at once, and the
control then says how the work stands.

- The note beside the button is a live region (`role="status"`) that changes
  only when the state does. The elapsed time ticks next to it outside the
  region, so nothing is read out every second.
- While its own work is live the button is `aria-disabled`, not disabled: it
  keeps the focus the operator just gave it and ignores a second press.
- When the work ends, the part of the page named by the ask's `refresh`
  selector is fetched again and swapped in place. Without one the page loads
  again, though not under somebody's hands and never twice running.
- A failure is said in the note, where it was asked.

In a table's row the note's line is as wide as the button, so there it says
the state's word alone ("Waiting", "Failed") and the row keeps its height. The
sentence is the button's title, and the line the row opens to says the word
and the sentence under the button's label, so a reader with no title to hover
still reads why.

Without script the same button is a form post back to the page, which draws
the control from what is stored. So a page loaded while the work is live says
so and resumes following it.

A job's panel (`_job_progress.html`) and the readings a page asks for when it
opens (`_visit_refresh.html`) are the same ask drawn differently; one behavior
in `ask.js` follows all three.

A link to a command (an `ActionLink`) is drawn by
`partials/_action_links.html` through `_page_action.html`. One whose command
would refuse now carries the refusal: it stands as an `aria-disabled` control,
focusable, with the reason as its title, never a link or a form.
*Gates:* `RequestNeverWaitsTests.test_asked_for_work_is_followed_by_one_script_behavior`;
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
| `data-fragment-history="replace"` | an in-place answer replaces the address; a region given another value is not answered in place, since nothing restores its entry on Back |
| `data-fragment-failure`, `data-fragment-fallback` | what the region says when it cannot be had, and the page that answers the same thing |
| `data-fragment-target="<id>"` on a link or form | its answer belongs in that region; empty for the region it is inside |

On the server a part is `{% partialdef name inline %}` in the page's own
template, so the page and the part are one definition. Any `PageMixin` view
answers a request that names a part with the part alone
(`hq.platform.application.fragments`). A view that does not know the name
answers the page and the region is found in it by its id, so a region never
depends on its server having been taught about it. A view may compose only
what the part shows (the dashboard does for its calendar and links). A part
whose inputs are known by revision answers an unchanged question with 304.

A part's root carries `data-fragment` and its name as its placeholder did, so
it is still the region once in place. A region draws no
box: in a section's body, a table it holds reaches the frame's sides as one
the body holds itself does. A fold's part is walked by the browser gate at its
`data-fragment-fallback` address, the page drawn with it open
(`estate.opened`).

Pressing a control moves nothing else: its feedback is drawn in a line already
reserved for it, never inserted beside the button or drawn over the page.

A radio group shows where the keyboard is through its chosen state. Safari
reaches radios only with Option-Tab, and WebKit stops matching
`:focus-visible` once an arrow key moves the choice, though `:focus` still
matches; a ring drawn on `:focus` would also follow a click in Chromium.

A swap keeps what the reader did: the keyboard stays on the control that was
pressed, a disclosure with an id stays as it was, the page does not scroll,
and the region is `aria-busy` while it waits. A newer question for a region
ends the older one. A question that fails falls back to the link or form it
enhanced; a post is never sent twice, and says what went wrong beside the
form. A session that ended says so and offers the way back in. A hidden tab
is not polled, and a region that fails is asked less often.

Script that needs a part calls `hqFragment.swap(region, {url})`; script that
needs to ask again calls `hqEvery(work, {ms})`. After a swap the new content
hears `hq:fragment`. Any part of a page can be swapped (the page's `main` is a
region), so a behavior either listens on the document and asks where an event
landed, or sets up its elements through `hqFragment.each(selector, setup)`,
which runs once for each element on the page as drawn and in every part placed
since.
*Gates:* `lint:js` (no other script fetches, reads a response as text,
parses markup or keeps an interval, and fragment.js does each once; no script
asks the page for an element at load outside `hqFragment.each`);
`FragmentPrimitiveTests` (every named part is defined);
`FragmentBrowserTests` (browser).

**Moving between pages is not a flash.** A same-origin navigation fades the
new page in under a header that does not move (`@view-transition`, with the
header as its own unanimated layer). It does so in browsers that have it and
for readers who have not asked for less motion.

Every page names one set of speculation rules by header: a link to a page of
HQ's is prefetched when the pointer or finger goes down on it, so the document
is arriving while the click completes. Prefetch only, never prerender, and
only on press, so a page is never held fetched and unread while the operator
changes what it would show.

A speculative request has no effect. `hq.platform.core.speculation` refuses
one at every route that answers without a session, at the audit writer and at
the outbound boundary. The rules leave out those routes and any link that is a
download, a dialog or a region answered in place.
*Gate:* `hq/platform/core/tests/test_speculation.py`, which prefetches every
page and fails on one that writes.

**A control is big enough to hit.** A target is hit through at least 24 by 24
CSS px (`--target-min`, WCAG 2.5.8 Target Size Minimum), counting the area its
`::before` or `::after` hangs over the space beside it, unless one of the
criterion's exceptions holds:

- Spacing: "if a 24 CSS pixel diameter circle is centered on the bounding box
  of each, the circles do not intersect another target or the circle for
  another undersized target". An area a target hangs over another target's is
  shared, and counts for neither.
- Equivalent: "The function can be achieved through a different control on
  the same page that meets this criterion". The control a target names in
  `data-target-equivalent` (by its id) is at least 24 by 24 and in the same
  cell or region.
- Inline: "The target is in a sentence or its size is otherwise constrained
  by the line-height of non-target text": set inline among words that are no
  target's.
- User agent control: "The size of the target is determined by the user agent
  and is not modified by the author": a control the browser draws, the size of
  a copy no author style reaches. One HQ draws or sizes, a styled tick box
  included, is held to the size.

A target another element is drawn over at its center (an open menu lying on
the page) is not reachable there, so it is not measured, and neither is a
disabled one or one an open modal makes inert. A panel shown over the page (a
menu's, a popover, a dialog) is a layer of its own. A target in it is never
measured against the page's beneath, as the criterion leaves out a target
"while obscured by content displayed as a result of a user interaction". A
tick box is hit through its labels too. The stylesheet's `--target-min` is
held to the same size.
*Gate:* the survey's `small targets` (`SMALL_TARGETS` in
`hq/platform/core/tests/browser/probes/boxes.py`), on every page and width the
layout checks read.

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
  the dashboard's included. Sections that share a row start their frames
  together too, whatever their heads hold (the survey's `frames in a row`).
- A band of readings or choices lays out as whole even rows, and each of its
  cells keeps its padding; so does any filled box. A band of facts shares
  its width in equal tracks.
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
- A card ends where its content does. Controls in a row are even. A tooltip
  is on something that takes focus. A column seam holds its place from band
  to band. A sentence is never set in a row of chips.
- A block of code stands apart from every surface it lands on, in both
  palettes.
- A current or chosen item is drawn from the ARIA state that names it
  (`aria-current`, `aria-selected`), never from a class beside it, and looks
  unlike the items around it. The browser gate holds that the state is there.

## Adding UI

1. Find the primitive. Most pages are a head, a band or card of facts, and a
   table or list.
2. If one almost fits, extend it in its sheet and its partial, with a comment
   saying what it covers and why.
3. A new container of content is a surface: add its selector to the surface
   list, write only its layout.
4. A new state of a page (empty, failing, waiting, crowded) gets a fixture in
   the browser gate, so the layout rules run over it.
5. A new state of a shared part (a long heading, a short trail step, a value
   that wraps), filled the way an extension's page fills it, is one entry in
   the component catalog (`hq/platform/core/tests/browser/catalog.py`). The
   entry is drawn through the part's own partial or tag, and held by the whole
   survey. An
   entry whose state is not on the page by itself says so as its premise.
6. Run `mise run check` and `mise run browser`, then look at the page at a phone,
   a tablet and a desktop width (`mise run dev:look`), against the rules held
   by eye. The gates catch classes of mistakes; they do
   not tell you it reads well. Review by walking a real workflow end to end, not
   page by page: every entity named is a link, every relationship is visible
   from both ends, and nothing shows a raw kind name, an empty table or a nag.
