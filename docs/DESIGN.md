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

**Tables size to their content.** No fixed layouts, no percentage column
widths (a row-header table, a description list in table markup, is the one
fixed grid). A long value caps itself (`max-width` and an ellipsis) rather than
owning the table's width. A table too wide for its box scrolls inside it (`.table-scroll`), the
only thing on a page allowed to scroll sideways. Rows that belong to sections
share one table as a `tbody` per group with a `.row-group` heading row, never
two tables kept aligned by hand. An identifier in a cell never breaks.
*Gates:* `test_only_a_table_scrolls_sideways`, the row height budget, and the
page crawl, which fails any `.data-table` outside `.table-scroll`.

**A row that says more than it shows expands whole.** Truncation (a clamp, an
ellipsis, a list folded behind "+5 more") is fine; a dead end is not.
`tables.js` measures each row and gives any row with cut-off content one toggle
that lifts every clamp and opens every fold in it. Nothing per table.

**One band.** A row of facts, readings or choices is a band: one surface,
cells divided by hairlines each cell draws itself, so a wrapped row keeps its
dividers at any width and a short last row ends in plain surface. A band of a
fixed count uses `--tracks-4-2-1` or `--tracks-3-1` to lay out as whole even
rows (never 3+1). A band is listed once, in the band rule in `app.css`; its
cells keep their own padding. Too narrow for two cells, a band reads as a
list: label left, value right.
*Gate:* `test_a_band_cell_keeps_its_padding`.

**One main-and-aside.** Content with a narrower companion (`.split`) shares a
row while both keep a readable width and wraps to a column when they cannot.
Pairs (`.two-col`) are at most two columns and pair like with like: a table
beside a table, a chart beside a chart, so each row ends together.

**Forms are a field grid.** Fields flow into as many 18rem columns as fit, in
reading order; a textarea, a group of choices, errors and the actions take the
row. A group of checkboxes reads across.

**Nothing empty takes space.** A panel with nothing to show is not drawn: the
page closes up rather than carrying a card that says "nothing here". An empty
list says whether its own search or filters emptied it, and offers the way
back.

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

**One disclosure.** Every `details` that is not a menu shows the same mark,
turned when open. A section that folds (`section-fold`) uses its heading as the
toggle, with its count beside it; never a separate "Show all".

**One filter bar.** `.filter-bar`: a search field, selects, Apply, and Clear
when anything is set. No chip walls.

**Values read as what they are.** `|readout` puts identifiers (keys, hosts,
paths, ports) in `<code>` and leaves counts, words and phrases as text.
Numbers are never chips.

**Colour comes from tokens.** No colour literal outside `@layer tokens`, so a
theme is a set of token values and dark mode is not a second stylesheet.

**Buttons.** Primary for the one thing a page is for; default for the rest;
`ghost` for link-like actions that sit inline; `danger` in red text on the same
surface as its neighbours, loud only in its confirmation.

## Adding UI

1. Find the primitive. Most pages are a head, a band or card of facts, and a
   table or list.
2. If one almost fits, extend it in `app.css` and its partial, with a comment
   saying what it now covers and why.
3. A new container of content is a surface: add its selector to the surface
   list, write only its layout.
4. Run `CHECK_BROWSER=1 ./scripts/check.sh`, then look at the page at a phone,
   a tablet and a desktop width. The gates catch classes of mistakes; they do
   not tell you it reads well.
