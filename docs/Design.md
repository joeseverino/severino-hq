# Design

HQ has one look, built from a small set of primitives. A page is assembled from
them; it does not style itself. If a page needs something the primitives cannot
express, the primitive grows (in its sheet under `static/css/app/` and its
partial), and every page that uses it gets the improvement. A page that needs a
different look of a part gets an option on that part. A new part is added only
when the structure differs (what it is, never how it looks), as the command
palette's group labels inside its menu are. Extensions ship no CSS for the same
reason: they render host classes and inherit every fix.

This page is for anyone adding or changing what a page draws, an extension's
pages included. Start with [Adding UI](#adding-ui), the order to work in. It
holds what holds across parts: [Rules](#rules) and [Held by eye](#held-by-eye),
what no gate measures. A part's own rules are on its `Part` declaration
(`rules`), stated under its name in [Shared UI](Shared-UI.md#each-parts-rules).

The stylesheet is the sheets `hq/platform/core/stylesheets.py` lists, each
one responsibility under the file-size ceiling (`SEVERINO_FILE_LINE_LIMIT`), linked in that order. The
first declares the cascade layers (`tokens`, `base`, `components`, `fit`)
and the rest fill them; `fit` holds what a table lets go as its region
narrows, so it outranks any component's own display. Inside a layer a later sheet wins a tie, so a sheet is added
to the list where its rules belong in the cascade, and every check of the
stylesheet reads it through that list.

**Browsers.** HQ's floor is the current and the previous release of each
engine: Safari (on the Mac and on iOS), Chromium and Firefox, read from
browser-compat-data's release dates as the package moves, never written as
numbers (`tools/lint/browser-floor.mjs`). A feature newer than the floor is
written inside `@supports`, or named in the browser floor rule's `degrades`
(`tools/lint/stylelint.config.mjs`) with what an engine without it draws;
`lint:css` refuses one written any other way. `mise run audit:bcd` says what
the floor has, and `mise run audit:supports` what the gates' own engines
answer.

Each rule names its gate where one enforces it. A lower-case gate without
`test_` is a rule of `LayoutBrowserTests` (`hq/platform/core/tests/browser/layout.py`), held on
every page at every width it applies to, each a subtest named for the rule,
the page and the width. The browser gate holds only what a reader would see
break: content past its box or the page's edge, content cut or out of reach,
one thing drawn over another, text touching text, an accessibility or focus
failure, a color too faint to read, a control too small or unlabeled,
something moving once it is drawn. Alignment, evenness, placement and
proportion are taste, held by review and by looking (`mise run dev:look`),
never by a number; [Held by eye](#held-by-eye) collects the ones a gate is
most tempted to measure. So each check of the layout survey (`CHECKS` in
`hq/platform/core/tests/browser/probes/`) says in its `reader_sees` what a
reader sees when it fails, and one whose failure is only uneven does not
belong there (`CheckRegistryTests`).

## Rules

**The owner's words, not HQ's.** One person reads this interface. He knows his
machines and does not know how HQ is built, so a page never says the names HQ
has for its own parts. A thing is called by its own noun ("DNS record",
"certificate", "service", "record"), never kind, resource, provider,
capability, consumer, claim, finding, generation, revision, standing, facet,
projection or lineage. HQ "reads" and "applies", never observes, reconciles,
converges or renders; what HQ expects is never "declared" or "desired"; and
what differs was "changed outside HQ", never drift. A job that stops
heartbeating "stopped reporting", never lost its lease, and the work is HQ's,
or Ansible's where Ansible acts, never the automation worker's. A page never
asks the owner to type a setting's name or a credential's field value: it
says what the setting lets HQ do, and the connection's guide says how. A held
request says the change the agent asked for and its target. Comments and
identifiers keep the internal vocabulary.

**A title is a short noun.** A section heading, a column heading, a label and a
button say the plainest name a stranger would understand at a glance:
"Devices", "Apps", "Rules", "Setup", "Permissions", "Approve". Two things on
one page never share a name. The sentence that explains it, if one is needed,
is the lede or the help text under it, never the title itself. A label (a
heading, a tab, a fold's title, a column, a row group, a dialog's title, a
button, a link to another page) is one to three words in sentence case. It is
the term GitHub, Stripe, Cloudflare, Vercel or Tailscale's dashboards use:
"Access", "History", "Source", "Defaults", "Installed on", "Unmanaged names".
It never opens with a question word and holds no pronoun, no
everything/each/every and no clause saying what the part holds. A kicker over
a heading names the group, never a word the heading says again.

The Vale style `tools/lint/vale/styles/Plain` swaps each retired name for the
one name per thing:

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
explanation of why. A detail is one short sentence the reader needs in order to
act, or nothing, and deleting a line is the first fix; past about twenty words
it is carrying a second thought. No sentence argues with itself, hedges,
explains HQ's machinery, or narrates the page, as "Open one to see" or "Each
connection is a 1Password item" does. No sentence chains "of" phrases ("the
procedures of the commit of its repository"): name the thing once, by what
owns it ("the playbooks you adopted"). The reader is "you" when the reader
acts, and anyone else is named or named by role ("an admin", "the agent"),
never "a person" or "someone". A count agrees with its noun through
`counted`, never "account(s)". No sentence praises ("seamless", "blazing
fast") or knocks down a reading nobody held ("not X, but Y"): say what the
thing does. What an agent or a client's author reads (an API or MCP
description, an error) is held to the same. A comment says what the code does
now, in the present tense; how it came to be is the commit message's.

**No page writes a style.** A page's look comes only from HQ's own sheets.
The policy's `style-src-elem 'self'` refuses a `<style>` element, and HQ
writes no `style` attribute either: one would be refused once
`style-src-attr` is `'self'` too, which waits only on a WebKit defect
([playwright#43617](https://github.com/microsoft/playwright/issues/43617)). A value drawn to scale is
an element that carries it (`<meter>`, `<progress>`, an SVG mark placed in
percent, `partials/_allocation.html`); a script that places a part writes
through the CSSOM (`style.setProperty`), which the policy does not govern. A
browser test is answered under the same policy, and the gate lifts a style
its own fixture writes into a sheet it serves (`gate.lifted`).
*Gates:* `test_style_attributes_are_the_only_relaxation_in_the_policy`;
`RenderedPageTests` over every page and fragment, an extension's included;
the template contract, for an extension's template; every browser test,
which fails on a refusal or a report to `/csp-report/`, and
`ExampleEstateBrowserTests`, which hears a refused page's report after
every navigation page.

**No script builds markup.** What a script adds to a page is a `<template>`
the server drew, in the partial that owns the region (a notice's tone in
`_notice_frames.html`, an ask's pieces in `_ask.html#frames`, the messages'
list in `_messages.html`, the live round trip in `_connection_loading.html#frame`),
cloned and filled through `textContent`. A
frame is drawn only where something can fill it: one that fills a part of a
panel comes with that panel, and a tone no script clones is not drawn (the
demo's info notice only in the saved copy). A script that clones a frame from
anywhere on the page loads only through `templates/scripts/<name>.html`,
which draws those frames beside its tag, so no page, a test's probe
included, has the script without them.
*Gates:* `lint:js`, which refuses `createElement` outside the API reference's
viewer; `ScriptFrameTests`, that every frame a script clones is drawn, and
that a script cloning from the page loads only through its part.

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

Text is cut in one sheet, `css/app/cuts.css`, in one of two ways that each
show the reader there is more: on its line with an ellipsis, or at its last
line by a clamp. A value that may be cut has its whole rule there, so it is
styled in one place. Every other text wraps. A box that
clips something other than text (a frame's corners, a scroll region) says why
beside its declaration.
*Gate:* `lint:css` (`cutText` in `tools/lint/stylelint.config.mjs`), and the
layout survey's `cut and closed`, `cut when open` and `cut beside room`.

A few words that read wrong split (an age, a hyphenated word) are one `.whole`,
the box `|breakable` gives each part of a name, or a `.nowrap`. A line that
must stay one line (a commit's message) is a `.one-line`, cut with an ellipsis.
A key to press is said in a `.keyboard-only`, which a touch screen hides;
`.visually-hidden` is the other way round, read only by a screen reader.

**One band.** A row of readings or choices is a band: one surface, cells
divided by hairlines each cell draws itself. So a wrapped row keeps its
dividers at any width and a short last row ends in plain surface.

A band of a fixed count lays out as whole even rows (never 3+1). A band counts
its own cells (not a dialog a cell opens, not its note) and takes the tracks
for that count, two to six (`--tracks-2-1` up to `--tracks-6-3-1`). A lone
cell takes the band, so no page says how many it holds.

A part that draws a band marks its box `data-band` (the facts and the
insights), and the one band rule in `css/app/figures.css` draws every one;
its cells keep their own padding. Too narrow for two cells, a band reads as a
list: label left, value right.

What names a value (`.fact-label`, a tile's label, a `<dt>`) is sentence case
in the muted voice, and words in that voice anywhere else are `{% muted %}`.
Capitals are for a kicker over a group, never a value's name, a state or a
tag. The kicker is the `eyebrow` class, one voice wherever it heads a group (a
timeline's month, a calendar's weekdays, Command Center's group heads), and
its part's own class says only where it stands; `lint:css` refuses a change of
case in any rule but `.eyebrow`'s.

**One main-and-aside.** Content with a narrower companion (`.split`) shares a
row while both keep a readable width and wraps to a column when they cannot.
A month as the main takes the room, its companion keeping its basis beside
it; a run of sections in a companion is a `.section-flow`, so wrapped under
the main they pair up.
Pairs (`.two-col`) are at most two columns and pair like with like: a table
beside a table, a chart beside a chart, so each row ends together.

**Say what HQ knows, once.** A sentence on a page is derived from the record
that holds its answer, never a generic placeholder or a hardcoded string. The
registrar is the one the zone record names, and a certificate's issuer and
expiry are what its status holds. Each fact appears once, where it belongs: a
card does not repeat the strip beside it.

**A check's title states the fact it checks.** "Tokens hold the permissions HQ
uses, and no more", "Keys are kept in a vault": what was checked, in plain words. Not a
sweeping claim ("every credential is limited to what HQ needs"), and not
"knows", "ensures" or "limited".

**A zero is information.** A count of zero says the thing was checked and there
is none; it stays on the page. Hiding it leaves the reader wondering whether it
was counted.

**Dense, never empty.** Content sits on a surface; when one is too big or too
loud, make it compact (one row, tighter padding, an urgent tone only when
urgent), never bare on the page background. A verdict sits where it belongs (on
the link between two ends), evidence beside its value, one line per check with
detail on demand, like beside like in a row. A card that is mostly empty, or a
row with a gulf in it, is a layout to redesign. Short facts go across the frame
(`{% facts %}` does it), and a short list beside what it describes. A card
much shorter than its peers shares a row with short ones, never with a tall
one.

**A page that is one document extends `document_page.html`.** The API
reference and the Design page's gallery are each one document under the site
header: no page head and no footer, the title spoken alone, the document
filling the view edge to edge and scrolled as the page scrolls
(`[data-document-page]`, page.css), bringing its own gutters. The page fills
`{% block document %}`; `test_document_page.py` fails any other template that
empties the footer and any other rule that fills the view from the column.

**One menu.** `.overflow-menu` for "more actions", a section group's dropdown
in the header, the user menu and a dashboard reading's panel are one primitive:
a `[data-menu]` holding a button and the `popover` it names with
`popovertarget`. The browser opens the panel in the top layer and closes it on
Escape, a press outside or another menu opening, then hands focus back to the
button, with no script. A menu inside another, such as a section's links in the
open sections drawer, leaves the outer one open. The panel hangs from its own
button by anchor positioning (`anchor-scope: --menu`, page.css "Menus"). It
hangs below the button from its right edge, or from its left where that would
cross the screen (`position-try-fallbacks: flip-inline`, decided against the
viewport). A section's links open from its left edge, and in the drawer beside
the list, down or up from their row on the side with more height, held to it
and scrolling when longer (`position-try-order`), so every link can be reached
on any screen. A menu opens on a press. A section group also opens on hover where
the pointer can hover, and a press then keeps it open. WebKit has no
`interestfor`, so a few lines of app.js do that one job, and the browser still
opens, closes and dismisses the popover. Inside a menu every control is a plain
row; a destructive one is only its color.

Every popover open is one floating panel, a menu's, a fact's, the sections
drawer, a filter bar's folded choices and a table's filter alike: its frame,
corners, fill and shadow are base.css's `:popover-open` rule, and its own rule
says only how it is laid out. Closed, a popover is part of the page, so one
drawn in its row (the header's sections, a filter bar's choices) carries no
frame to undo. A select's picker is drawn the same. *Gate:* `lint:css` refuses
a floating panel's shadow anywhere but base.css.

**One disclosure.** Every `details` shows the same mark,
turned when open. A section that folds is `{% section fold=True %}`, its
heading the toggle.

**Values read as what they are.** `|readout` puts identifiers (keys, hosts,
paths, ports) in `<code>` and leaves counts, words and phrases as text.

Numbers are never chips: a figure stands in a band of facts, a numeric column or a
sentence (`hq/platform/core/tests/test_template_chips.py`). One that carries a
state says it in ink, as `.figure` with its tone class
(`<span class="figure tone-attention">`).

An address, a path or a command breaks only where `|breakable` lets it (after
a dot or a slash, before a port); in a table's cell it breaks elsewhere only
where its column lacks room for it. A length of time
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

**A link is underlined only among words.** WCAG 1.4.1 asks that a link set in a
sentence, a definition, help text or a muted line differ from the words around
it, so HQ gives it a quiet underline at rest. No link color reaches 3:1 against
muted text while keeping 4.5:1 against the surface, so color alone cannot do
it. A link that is the whole of its line, row, item or chip is never underlined
at rest; it reads by its color and underlines on hover and focus, as every link
does. One rule in `base.css` draws it. Words are a `p`, a `small`, a `.muted`
line, or a value, item or notice marked `.prose` because it reads as a sentence (a name
followed by a note, a summary with a link in it). A link with a class of its
own is a control, `.external-link` aside. A table cell's `prose` stays on its
part when the column leaves for the row's open line. The survey's `links at
rest` check reads every link on every page from the rendered layout and names
one underlined alone or among words without a cue of its own (an underline, or
a weight, size or face as axe accepts).

**A link's line runs under its name alone.** The mark a sentence puts after
a name (`breakable`'s `mark`, `{% entity link mark="," %}`) keeps to the
name's last line inside the link, and `base.css` draws it clear of the
underline. A separator between items (a dot list's dot, the meta line's) is
drawn in front of an item that holds the link, never on the link itself, and
a status tag sits beside a link, never inside it.

An external link's arrow (`.external-link`) is joined to its last word by a
no-break space. A box that wraps is as wide as its line, so outside a table a
link's name (its cut token) and its chip run inline, and the arrow follows
the last word on its line. A chip in a table is a box, so its link lays the
chip and the arrow out as one row, since a line may break after any box.

**A dot is a swatch.** A dot for a state or a kind, and a key for a series
or a category (`swatch-key`, square), is the `swatch` class, colored by its
`series-N` slot or what its part sets, one size wherever it stands: a legend's
key is the mark it keys, and no part writes a dot's size or shape of its own.
A mark drawn as generated content joins the swatch's rule in figures.css. A
smaller one in a dense calendar day, or the dots figure's larger one, sets
`--mark-size`.

**A mark drawn by CSS is not read.** Generated content (`content:`) says what
a reader hears after a slash: `"›" / ""` for a chevron, a separator or an
arrow, the words themselves for text that means something. *Gate:* `lint:css`
refuses a generated string with no alternative, or a space before a mark.

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
| `when:"dateline"` | "Wednesday, Nov 18" | the day a page opens on, over its title |
| `when:"length"` | "5 minutes", either way from now | a wait |
| `when:"ahead"` | "in 3 days" | a wait |
| `{{ at\|when_text:"ago" }}` | the words of any form, as plain text | an attribute, or a table cell's line, which sorts on the first `<time>` it holds |
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
itself. It uses `{{ n|counted:"check" }}` for a count, and `{{
miles|measured:"mi,2" }}` for a unit or a word the count leaves alone
(`measured:"more"`), never `floatformat` before it: the count after the comma
rounds the figure as `floatformat` would, and `ui.measured` writes the same
in Python. A measured zero is 0; the missing mark is for no value. The value tags that say a figure in words (`bytes`,
`seconds`, `when`) join theirs. *Gate:* the `hand-drawn-split-figure` rule,
over the host's templates and every extension's.

A bare figure counting what a heading heads (`.count`) stands in that
heading after its words: `<h2>Readings <span class="count">7</span></h2>`,
and the same in a summary, a group row and a tab. Never in parentheses, and
never alone at the far end of a `.section-head`, cut off from the words it
counts; what stands there is a line about the section, a count with its
unit ("3 events", "2 left") among them.
*Gate:* `test_every_bare_count_stands_in_its_heading`.

**Color comes from tokens.** No color literal outside `@layer tokens`, so a
theme is a set of token values and dark mode is not a second stylesheet.
`color-scheme` picks the palette, no rule asks for `prefers-color-scheme`,
and none but the tokens reads `data-theme`, so an operator's chosen theme
and the system's paint one page, and the survey, which paints each palette
the same way, reads both.

A state's color is its tone's. A part drawn in one carries the tone's class
(`tone-good`, through `tone_class` or `{% status %}`) and reads a role the
tone map in tokens.css sets for it: `--tone-mark` for a mark or words that
stand alone, `--tone-ink` for words on a fill, `--tone-bg`, `--tone-tint` and
`--tone-wash` for fills, `--tone-line` for an edge. No other rule reads a
tone's palette (`--ok`, `--tone-caution-ink`), so a state looks one way
wherever it stands; the button's destructive look, which is no state, is the
one exception.
*Gate:* `lint:css`.

**Text never fades.** An opacity under 1 is for an inactive control
(disabled, `aria-disabled`, `aria-busy`) or a mark that is not text. Text a
reader still uses takes an ink token that clears 4.5:1: `--ink-3` on a tinted
fill, `--muted` on a surface, `--code-muted` beside code.
*Gate:* the survey checks contrast, measuring faded text as it is drawn.

**An action says its command's words.** What a button, link, remedy or toast
says is declared on the command it runs ([Extensions](Plugins.md), "Capabilities")
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
it goes live with `asks.APPLIES_SHORTLY`, beside the
save button, so a field's note says only what the change does.
*Gate:* `hq/platform/application/tests/test_action_words.py`, and the
lint:ast-grep rules it names.

A link to a command (an `ActionLink`) is drawn by
`partials/_action_links.html` through `_page_action.html`. One whose command
would refuse now carries the refusal: it stands as an `aria-disabled` control,
focusable, with the reason as its title, never a link or a form.
*Gate:* `hq/platform/application/tests/test_refused_links.py`.

**A frame is filled.** A frame across the page is filled by what it holds,
or narrowed to it. *Gate:* `frames filled` in the layout survey.

A value goes under its label only where the box cannot hold both on one line.
The same holds for a node key beside its machine and a piece of evidence beside
its kind.

**Sibling sections flow.** A run of sibling sections or cards (the sections
that close a thing's page, the connection panel's identity, link and route, a
page's run of cards) is one `.section-flow`. It lays as many to a row as keep
each at least `--flow-basis` wide (22rem), and one to a row where that is the
whole width. A row's last ones share what it leaves, and each stands on a row
as tall as the tallest. Every member grows into what its row leaves and none
caps its width, so every row ends at the flow's end. It is measured against the
box it sits in, so the same sections pair in a dialog and on a page. A part
that needs more room before it shares a row sets a wider basis on itself (the
connection panel's route), and a run of narrower cards a narrower one on the
flow. A wide one (a table of four columns or more, a fold, anything that is not
a section or card) goes across the row. The flow paints its members in the
order the markup holds them, so what is read aloud and where the keyboard goes
is what is drawn (WCAG 1.3.2, 2.4.3): nothing reorders them. A page therefore
writes its wide ones ahead of its short ones, which collect after them to share
rows rather than one standing beside the gap a wide one leaves. Registry
sections say which they are (`ServiceSection.spans_row`), and
`partials/_page_sections.html` draws one kind per pass, `spans_row=True` ahead
of a page's short sections and `False` among them. A command's page draws what the
command says of itself (`CapabilitySpec.about`, a `CommandAbout` of facts and
sections) under its form through the same parts. *Gate:* `flows in reading
order` in the layout survey. No page writes an
auto-fit grid of its own for this: `lint:css` refuses `repeat(auto-fit` and
`repeat(auto-fill` outside the parts that lay out something else inside
themselves, each of which says so in its disable comment.

A link to a part of a page lands below the sticky header, from one
`scroll-padding-top`. A table's row, or a control in one, lands below its
sticky heading too (`--heading-height`). The chrome a part sticks under is
only what the page draws: a page without the header has no inset.
A part that covers what scrolls beneath it (a sticky head or bar, a fade)
paints `var(--ground)`, the surface it sits on: the page's, or a frame's
inside one (`tools/lint/stylelint-ground-cover.mjs`, in `lint:css`, holds it).

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
region). So a behavior either listens on the document and asks where an event
landed, or sets up its elements through `hqFragment.each(selector, setup)`.
That runs once for each element on the page as drawn and in every part placed
since. *Gates:* `lint:js` (no other script fetches, reads a response as text,
parses markup or keeps an interval, and fragment.js does each once; no script
asks the page for an element at load outside `hqFragment.each`);
`FragmentPrimitiveTests` (every named part is defined); `FragmentBrowserTests`
(browser).

**Moving between pages is not a flash.** A same-origin navigation fades the
new page in under a header that does not move (`@view-transition`, with the
header as its own unanimated layer). It does so in browsers that have it and
for readers who have not asked for less motion. For a reader who has, one rule
in `base.css` stops every transition and animation, so no part writes its own
(`lint:css`).

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

A GET to a route that answers only POST is refused and writes nothing, from
an operator, from somebody signed out, and from an agent's token where the
route takes one.
*Gate:* `GetHasNoEffectTests` in
`hq/platform/application/tests/test_rendered.py`, which sends that GET to
every route, an installed extension's included.

**A control is big enough to hit.** A target is hit through at least 24 by 24
CSS px (`--target-min`, WCAG 2.5.8 Target Size Minimum). That counts the area
its `::before` or `::after` hangs over the space beside it. A control drawn
smaller asks for that area in its own partial with `data-hit` (`hit=True` on
`{% button %}`), or with `data-hit="coarse"` where it is packed too close
to its neighbors for one under a mouse; page.css hangs it, and the quiet link
(`.subtle-link`) and a pill's link always have one. A run of names that links
them (a `codes` list) wraps onto lines a target tall, as a value holding a
pill's link does. A smaller target passes only through one of the criterion's
exceptions:

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

A printed page's provenance belongs in its frame's `print_footer` block;
the page frame owns its visibility and typography.

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
- Sections and cards that share a row start their frames together, whatever
  their heads hold, and in a flow they end together too, the dashboard's
  included. Each holds its contents at its top and its foot at its bottom.
- A chip, a change's two values and a band's facts sit on the baseline of the
  line they share, and a box set in a line that cuts what it holds is set by
  its top.
- A band of insights or choices lays out as whole even rows, and each of its
  cells keeps its padding; so does any filled box. A band of facts sizes its
  cells by what they hold.
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
- A band's values are one size, and a drawing beside one never sets its
  cell's width.

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
   table or list. Its rules are on its `Part` declaration and its row in
   [Shared UI](Shared-UI.md#each-parts-rules).
2. If one almost fits, extend it in its sheet and its partial, with a comment
   saying what it covers and why, and its rules on its `Part`. A page writes only the public classes
   (`template_contract.public_classes`: the layout classes and each part's
   `public_classes`), as an extension does. Every other class is one part's
   own, declared on its `Part` (`classes`), and reaches a page only through
   that part; a part drawn with no tag is a `Part` in
   `hq/platform/core/included_parts.py`, a widget drawn once included.
3. A new container of content is a surface: add its selector to the surface
   list, write only its layout.
4. A new state of a page (empty, failing, waiting, crowded) gets a fixture in
   the browser gate, so the layout rules run over it.
5. A new state of a shared part (a long heading, a short trail step, a value
   that wraps), filled the way an extension's page fills it, is one of its
   `Part`'s `states`, an `Example` the component catalog
   (`hq/platform/core/tests/browser/catalog/`) draws as an entry of its own,
   `part/` and the part's name and the state's. An example given records sits
   in `hq/platform/core/part_examples/`, which the `Part` names. An entry that
   draws several parts together goes in the module of its family under
   `families/`, the prefix of its name (`table/`, `form/`); a new family is a
   new module there, imported by the catalog's `__init__.py` and listed in
   `test_browser_probes.py`'s `MODULES`.
   The entry is drawn through the part's own partial or tag, and held by the
   whole survey. An entry whose state is not on the page by itself says so as
   its premise, a finding of the survey wherever the entry is read. An
   entry's page loads HQ's scripts, and the gate holds it
   as the server drew it and once they have run, with every row a reader
   can open opened and its tables read at every width. Without script a
   table entry is held by every check but those declared `scripted`, and
   may scroll in its region.
6. Run `mise run check` and `mise run browser`, then look at the page at a phone,
   a tablet and a desktop width (`mise run dev:look`), against the rules held
   by eye. The gates catch classes of mistakes; they do
   not tell you it reads well. Review by walking a real workflow end to end, not
   page by page: every entity named is a link, every relationship is visible
   from both ends, and nothing shows a raw kind name, an empty table or a nag.
