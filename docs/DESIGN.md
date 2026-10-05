# Design language

HQ has one look, built from a small set of primitives. A page is assembled
from them; it does not style itself. If a page needs something the primitives
cannot express, the primitive grows (in `static/css/app.css` and its partial),
and every page that uses it gets the improvement. Extensions ship no CSS for
the same reason: they render host classes and inherit every fix.

The rules below are enforced where they can be. Each names its gate.

## Rules

**One frame per thing.** A box that holds content is a *surface*: one border,
one radius, one background, one shadow, drawn by the single surface rule in
`app.css`. A component joins by adding its selector to that rule's list and
declares only its layout. A surface inside a surface draws no frame and no
inset: the outer one already says "these belong together". Nesting is handled
by inheritance, so nobody has to remember it.
*Gate:* `test_nothing_is_framed_inside_a_frame` (browser).

**Size to content, fit the window.** Layouts are intrinsic: grids fit as many
columns as their content allows (`repeat(auto-fit, minmax(min(100%, X), 1fr))`),
rows wrap (`flex-wrap` with a basis), text wraps by measure. When a component
must change shape, it asks its own container (`@container`), not the screen,
so it reads the same in a narrow card as on a phone. A viewport breakpoint is
for what the viewport decides (the header, a full-screen modal) and says why
in a comment. Content uses the window up to `--content-max`; prose keeps
`--measure`.
One caveat: never make an element holding buttons a size container. Chrome
restyles its contents once the container is measured and runs their
transitions from nothing, so every button fades in on load.
*Gates:* the overflow sweep at 375, 820 and 1360;
`test_viewport_breakpoints_only_go_down`, a ceiling that only falls.

**A table fits its box, at every width, by one mechanism.** No screen width
is asked anywhere in the table primitive. A table takes its box's width and
wraps. `tables.js` gives each column a floor, the least it can be and still be
read (its own one-line width or a few words' worth, whichever is less; more
for the column that names the row), and the browser lays the table out above
those floors, taking room from the columns that have it to give. Only when
the floors themselves do not fit does a column leave: first any marked
`optional-col`, then from the far end; never the column that names the row,
one whose heading is marked `key-col` (`TableColumn(..., css="key-col")` on a
list view), or the closing figure of a table that marks none. What leaves is
in the row's toggle. A table in a narrow box, a phone or half a page beside a
chart, is marked by its box's width and gets roomier rows and holds back what
is marked `narrow-more`. A page says only which columns are key or optional;
the rest is the table primitive's (`app.css`, `tables.js`). A table never
stacks into cards and is not swiped through. A key/value table (row headers,
no `thead`) sizes its label column to its longest label. Rows that belong to
sections share one table as a `tbody` per group with a `.row-group` heading
row, never two tables kept aligned by hand.
*Gates:* `test_a_table_fits_its_box_at_every_width`,
`test_only_a_table_scrolls_sideways`, the row height budget, and the page
crawl, which fails any `.data-table` outside `.table-scroll`.

**Opening a row moves nothing.** What an open row was holding back, the
columns that left and the `narrow-more` parts, is listed in a line of its own
beneath the row, each part under its column's name. It is the row's own
content moved there and moved back, never a copy, so a control in it is still
the one control. The row's own cells stay as they were, and the columns are
held at their widths for as long as any row is open, so every other row keeps
its place. The toggle stands in a gutter that every row of the table keeps,
so names share one edge whether or not a given row opens.
*Gates:* `test_a_wide_table_fits_a_phone_and_opening_a_row_moves_nothing`,
`test_a_tables_names_share_one_edge_whether_or_not_a_row_opens`.

**Every table sorts.** A list its server sorts has links in its headings.
Any other table with headings gets the same control, looking the same, from
`tables.js`, sorting
the rows on the page: ascending, descending, then as they came. Amounts,
dates and lengths of time sort as what they are; a cell may say its own value
with `data-sort`. Sections keep their own rows. Nothing per table.
*Gate:* `test_any_table_sorts_by_a_heading`.

**A row that says more than it shows expands whole.** Truncation (a clamp, an
ellipsis, a list folded behind "+5 more") is fine; a dead end is not.
`tables.js` measures each row and gives any row with cut-off content one toggle
that lifts every clamp and opens every fold in it. Nothing per table.
That toggle is the row's only disclosure: what a row holds back is marked
`.row-more` (hidden until the row is expanded) and its collapsed hint
`.row-less`, never a `<details>` inside a cell. The gate fails a row with both.

**One band.** A row of facts, readings or choices is a band: one surface,
cells divided by hairlines each cell draws itself, so a wrapped row keeps its
dividers at any width and a short last row ends in plain surface. A band of a
fixed count lays out as whole even rows (never 3+1): a grid band reads its
own count and takes `--tracks-4-2-1` or `--tracks-3-1`, and a lone cell takes
the band, so no page says how many it holds. A band is listed once, in the band rule in `app.css`; its
cells keep their own padding. Too narrow for two cells, a band reads as a
list: label left, value right.
*Gate:* `test_a_band_cell_keeps_its_padding`.

**One main-and-aside.** Content with a narrower companion (`.split`) shares a
row while both keep a readable width and wraps to a column when they cannot.
Pairs (`.two-col`) are at most two columns and pair like with like: a table
beside a table, a chart beside a chart, so each row ends together.

**Forms are two columns, or one.** Fields flow in reading order, two to a row
where there is room and none narrower than 18rem, and a row's fields share its
whole width: pairs sit side by side and the odd one of a run takes its row, so
no row is left with a field and a hole beside it. A textarea, a group of
choices, errors and the actions take the row. A group of checkboxes reads
across.

**Nothing empty takes space.** A panel with nothing to show is not drawn: the
page closes up rather than carrying a card that says "nothing here". An empty
list says whether its own search or filters emptied it, and offers the way
back. A page with nothing yet says what is missing and, where it can, what
would fill it and where (`partials/_empty_state.html`: `message`, `why`, an
action); controls that only make sense over data wait for the data.

**One head.** `partials/_page_head.html`: trail, title, controls, lede, laid
out the same at every width. The title and its controls share a row (the title
wraps before the controls move); the lede runs the full width under both. On a
narrow screen the lead control (the page's primary, else its first
non-destructive) stays and the rest go behind the overflow menu.
*Gate:* `test_a_narrow_head_keeps_its_lead_control_beside_the_title`.

**A lede earns its line.** Most pages have none. A lede says what the page
cannot show: a fact that changes how you read it ("Lookups run from outside
this network"), or the record's own details. Never a table of contents of the
page, a pointer to another page (link to it instead), or how HQ works inside
(a file path, a background mechanism).

**One menu.** `.overflow-menu` for "more actions", the nav dropdown for
navigation. Every dropdown carries `data-menu`, which gives it the shared
dismissal (outside click, Escape). Inside a menu every control is a plain row;
a destructive one is only its colour.

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
Numbers are never chips.

A moment, a date and an age are each written one way, by `hq/platform/application/moments.py`:
`when` ("Oct 3, 9:29 AM"), `when_day` ("Oct 3"), `ago` ("5 days ago", one
unit) and `when_exact` ("Oct 3, 2026, 9:29:15 AM CDT"). The year is said only
when it is not this one. A page shows them through the built-in `|when`
filter, which wraps the words in `<time datetime title>`: `{{ at|when }}` on a
detail page, `{{ at|when:"ago" }}` in a list (the exact moment on hover),
`"day"` and `"exact"` for the other two. The `datetime` is what a table sorts
on, so a date column needs nothing else. No template carries a `|date:` clock
format or prints a datetime bare; `|ago` is the same age as plain words, for a
sentence or an attribute. A run of days is `when_range` ("Oct 3 – Oct 9"), a
year both ends share said once. Extensions get all five from `hq_sdk.ui`.

A count's plural has one owner too: `counted`, which spells a single noun
through `labels.plural` ("2 entries"). An audit type's plural is
`AuditLog.type_plural`: the model's `verbose_name_plural` where declared.

**Colour comes from tokens.** No colour literal outside `@layer tokens`, so a
theme is a set of token values and dark mode is not a second stylesheet.

**Buttons.** Primary for the one thing a page is for; default for the rest;
`ghost` for link-like actions that sit inline; `danger` in red text on the same
surface as its neighbours, loud only in its confirmation.

**A decision carries its evidence.** The action queue
and domain attention lists use one queue projection and one row partial.
A row leads with what is wrong, beside how bad it is and what it is about;
the next step is the toggle for the evidence and remaining steps behind it;
what can be done follows, outside the disclosure.
An owner's first available workflow step supplies the actions only when it
emits no quick actions. Promoted actions are omitted from the disclosure by
method and URL, without changing the transport's full workflow. All emitted
actions use the page-action renderer and the shared POST form.

The dashboard leads with domain overviews, keeping attention in its linked
count rather than duplicating the queue.

**An item is open until its owner stops raising it.** Nobody closes one by
looking at it. What a reader can do with one they are not acting on is dismiss
it: it leaves their queue and their counts, stays reachable in a fold below,
and comes back on its own when it gets worse or grows. The header and the
dashboard count what waits; a dashboard with only dismissed items says so and
links to them, and never claims all is clear.
**A family folds.** An owner names what kind of matter an item is
(`Insight(family="Image advisories")`), in the words it would head a list
with. Several open items of one family from one domain are one line in the
queue (how bad, what kind, how many) until opened, and can be dismissed whole.
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
then presents the three supporting facts together. Values, context and links
still render once from the original KPI collection. Other counts retain their
even band. Overview cards fit their available width, placing three domains
side by side when space permits; empty contributors take no space.

**A figure may be drawn as well as said.** `Kpi` takes one `drawing`, or none:

- `Dots`, a part of a whole, one dot each. `Dots.of(filled, whole)` when the
  parts are only a count; `Dots((Dot(...), ...))` when each is something with a
  page of its own, which then names itself when pointed at and links there.
  The figure's own link moves from the cell to its value, since a link cannot
  hold links. Past a dozen, dots are a texture and are not drawn.
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

**The dashboard says how you reached it.** Beside the at-a-glance readings
sits one for the request itself: the channel, the path, the encryption and
whether the admission checks hold. It is the same reading the connection
panel opens on and a link to that panel, so it adds no second account of a
request and works without script. It is rendered with the page rather than
fetched after it: a line that arrives late is a line that visibly changes.
Beside it, whether the controller is still arriving: every reading on the
page is the controller's, so one that has stopped leaves them all as they
were. Nothing on the dashboard is fetched after it has rendered unless
somebody opens it.

**A page about one thing asks for that thing's readings.** A machine's page
and a service's page include `partials/_visit_refresh.html`, naming which page
they are. Once the page is showing, it posts that, and HQ works out which kinds
the page is assembled from and asks the controller for those whose last attempt
is older than their cadence (`hq/platform/application/freshness.py`). Opening a page never
logs in to a machine: a kind whose reader opens a shell is left to the sweep.
A page in a background tab asks for nothing, a GET never does, and the request
cannot name a connection or a kind. When the reading lands the page loads
again, once; or, if somebody has touched it or it reloaded a moment ago, it
says a newer reading is in and leaves showing it to them.

**Asked-for work is an ask.** A button that starts work which outlives the
request is `partials/_ask.html`, from an `Ask`
(`hq/platform/application/asks.py`), and may stand wherever a `PageAction`
does. Pressing it posts; the request stores the ask and answers at once; the
control then says how the work stands. The note beside the button is a live
region (`role="status"`) that changes only when the state does, and the elapsed
time ticks next to it outside the region, so nothing is read out every second.
While its own work is live the button is `aria-disabled`, not disabled: it
keeps the focus the operator just gave it and ignores a second press. When the
work ends the part of the page named by the ask's `refresh` selector is fetched
again and swapped in place; without one the page loads again, though not under
somebody's hands and never twice running. A failure is said in the note, where
it was asked. Without script the same button is a form post back to the page,
which draws the control from what is stored, so a page loaded while the work is
live says so and resumes following it. A job's panel (`_job_progress.html`) and
the readings a page asks for when it opens (`_visit_refresh.html`) are the same
ask drawn differently; one behaviour in `app.js` follows all three.
*Gates:* `RequestNeverWaitsTests.test_asked_for_work_is_followed_by_one_script_behaviour`;
`hq/platform/core/tests/test_action_budgets.py`.

## Adding UI

1. Find the primitive. Most pages are a head, a band or card of facts, and a
   table or list.
2. If one almost fits, extend it in `app.css` and its partial, with a comment
   saying what it now covers and why.
3. A new container of content is a surface: add its selector to the surface
   list, write only its layout.
4. Run `mise run check` and `mise run browser`, then look at the page at a phone,
   a tablet and a desktop width. The gates catch classes of mistakes; they do
   not tell you it reads well.
