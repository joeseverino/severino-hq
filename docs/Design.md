# Design

HQ has one look, built from a small set of primitives. A page is assembled
from them; it does not style itself. If a page needs something the primitives
cannot express, the primitive grows (in `static/css/app.css` and its partial),
and every page that uses it gets the improvement. Extensions ship no CSS for
the same reason: they render host classes and inherit every fix.

Each rule names its gate where one enforces it. A lower-case gate without
`test_` is a rule of `LayoutBrowserTests` (`hq/platform/core/browser_tests.py`), held on
every page at every width it applies to, each a subtest named for the rule,
the page and the width.

## Rules

**The owner's words, not HQ's.** One person reads this interface. He knows his
machines and does not know how HQ is built, so a page never says the names HQ
has for its own parts: a thing is called by its own noun ("DNS record",
"certificate"), HQ "reads" and "applies", and what differs was "changed
outside HQ". `hq/platform/core/plain_words.py` holds each word that is never
shown with what to say instead, and each label that was retired for one name
per thing: the queue is "Needs you", search is "Search" and its box "Find
anything", a fresh read from a thing's page is "Read now" and from a problem
card "Check again". Comments and identifiers keep the internal vocabulary.
*Gate:* `PlainWordsTests`, over the paths it lists.

**One frame per thing.** A box that holds content is a *surface*: one border,
one radius, one background, one shadow, drawn by the single surface rule in
`app.css`. A component joins by adding its selector to that rule's list and
declares only its layout. A surface inside a surface draws no frame and no
inset: the outer one already says "these belong together". Nesting is handled
by inheritance, so nobody has to remember it.
*Gate:* `nothing_is_framed_inside_a_frame` (browser).

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
(`OVERFLOW_WIDTHS`); `test_viewport_breakpoints_only_go_down`, a ceiling that
only falls.

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
is marked `narrow-more`. A column with nothing but dashes in every row is not
drawn, and a column's minimum width is rounded up so a short word like "Runbook" is not split. A page says only which columns are key or optional;
the rest is the table primitive's (`app.css`, `tables.js`). A table never
stacks into cards and is not swiped through. A key/value table (row headers,
no `thead`) sizes its label column to its longest label. Rows that belong to
sections share one table as a `tbody` per group with a `.row-group` heading
row, never two tables kept aligned by hand.
*Gates:* `a_table_fits_its_box_at_every_width`,
`test_a_blank_column_is_not_drawn_and_a_short_word_is_not_split`,
`only_a_table_scrolls_sideways`, the row height budget, and the page
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
`a_tables_names_share_one_edge_whether_or_not_a_row_opens`.

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
fixed count lays out as whole even rows (never 3+1): a band counts its own
cells (not a dialog a cell opens, not its note) and takes the tracks for that
count, two to six (`--tracks-2-1` up to `--tracks-6-3-1`), and a lone cell takes
the band, so no page says how many it holds. A band is listed once, in the band rule in `app.css`; its
cells keep their own padding. Too narrow for two cells, a band reads as a
list: label left, value right. A list of short steps (`.step-list`) is read
down its columns, so its columns end within a step of each other. The
dashboard's readings are one row when each has room and a list when not.
*Gates:* `a_band_cell_keeps_its_padding`, `a_band_lays_out_as_whole_rows`.

**A route reads in one direction.** A path from one thing to another
(`control_plane/_path_hops.html`) is one surface, each hop a stop on one
line with its icon on the line. It runs across its box when every hop has
room for its kind and an address with its port, and down it otherwise, as
the connection panel draws its route; it never wraps onto a second row.
*Gate:* `a_route_is_one_row_or_one_column`.

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
narrow screen the lead control stays and the rest go behind the overflow menu.
The lead is chosen by what each control is for, never by where it stands: not
a destructive one while another is offered, then one that can be pressed, then
the page's primary (`lead_action`). A destructive verb (a stop) is a link to
its confirmation, which names what it stops.
*Gate:* `a_narrow_head_keeps_its_lead_control_beside_the_title`.

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

**The header is one row.** When room runs out, the search field collapses to a
button that opens over the navigation, then the sections move behind the
toggle. The navigation never shrinks below its own content, and the bar's
controls all take their size from `--control-size`.

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
Numbers are never chips. An address, a path or a command breaks only where
`|breakable` lets it (after a dot or a slash, before a port), never inside a
hyphenated name; a length of time keeps each count with its unit.

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

**A card says what is wrong and holds what fixes it.** The queue is named
"Needs you" wherever it appears. The dashboard, the queue page and a domain's
own attention list use one projection (`hq/platform/application/decisions.py`)
and one card partial (`partials/_work_queue.html`). A card reads, in order:

- how urgent it is, in one set of words (`decisions.URGENCY`), and the title,
  which states what is wrong and links to the thing it is about;
- one or two sentences of what it means;
- since when, where its owner knows (`Insight(since=...)`), and never a guess;
- what to do: a command or an instruction is a step, one standing alone and
  several numbered, folded under "Steps" only when long;
- the reason it cannot be done from here, as a sentence and never as a step;
- what was read that shows it (`Insight(facts=...)`), each label once with its
  values beside it;
- buttons: the owner's actions, the first step that can be taken now when the
  owner offered none, and the check that reads again. At most one is primary;
- the pages that help look into it (`Insight(links=...)`), quiet links, the
  topology's as icons with HQ's tooltip.

Buttons and links stand right after what the card says (which is as wide as
its words, up to a readable line length), so neither takes a row of its own nor stands at
the far edge of a wide card. The queue, a thing's own page and the findings page
draw this one card; a command is `partials/_command.html`, with its copy
button in its corner. Its urgency is `partials/_urgency_pill.html`: a Cmd- or
Ctrl-click opens what raised it (the finding rule, its severity and the line
its detector is defined at, or the domain and key of anything else), read
from the code rather than written down.

Nothing that explains a card or acts on it sits behind a toggle. A next step
an owner names as a short label with a `url` is a link to that page; one it
writes as a sentence is shown as a sentence. Each action renders once, by
method and URL, through the page-action renderer and the shared POST form,
and the transport keeps the owner's full workflow. A dismissed card is its
title and the way back.

A queue of three or fewer is read on the dashboard, under the greeting, on the
same cards. A longer one is a count and a link to its page.

**A frame is filled.** Facts are `.kv`: each label beside its value, as many
pairs to a row as the width holds, never a two-column table stretched to the
page. A thing's state sits on its title's line (`page_aside`), as words; the
facts about it at a glance are a band under the head, as a service, a machine
and HQ's own page open. A line that dates or sources a section goes beside its
heading (`.heading-aside`), not loose under it. The sections that
close a thing's page are `.section-flow`: each a card holding its own heading,
two to a row at one height, with a wide one (four columns or more, or folded)
across the row and the short ones collected to pair. The browser gate fails a
frame across the page whose content stops short of half of it
(`a_wide_frame_is_filled`).

**Only what is urgent is framed.** A thing's page draws the problems about it
from `partials/_problems.html`. An urgent one is the queue's card in a box
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
then the three supporting facts together. Values, context and links render
once from the KPI collection. Other counts keep their even band. Overview cards fit their available width, placing three domains
side by side when space permits; empty contributors take no space.

**A figure may be drawn as well as said.** `Kpi` takes one `drawing`, or none:

- `Dots`, a part of a whole, one dot each. `Dots.of(filled, whole)` when the
  parts are only a count; `Dots((Dot(...), ...))` when each is something with a
  page of its own, which then names itself when pointed at and links there.
  The figure's own link moves from the cell to its value, since a link cannot
  hold links. Past a dozen, dots are a texture and are not drawn. A history is
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

A swap keeps what the reader did: the keyboard stays on the control that was
pressed, a disclosure with an id stays as it was, the page does not scroll,
and the region is `aria-busy` while it waits. A newer question for a region
ends the older one. A question that fails falls back to the link or form it
enhanced; a post is never sent twice, and says what went wrong beside the
form. A session that ended says so and offers the way back in. A hidden tab
is not polled, and a region that fails is asked less often.

Script that needs a part calls `hqFragment.swap(region, {url})`; script that
needs to ask again calls `hqEvery(work, {ms})`. After a swap the new content
hears `hq:fragment`, which is where a behaviour binds to it.
*Gates:* `FragmentPrimitiveTests` (no other script reads a response as text,
parses markup or keeps an interval; every named part is defined);
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
