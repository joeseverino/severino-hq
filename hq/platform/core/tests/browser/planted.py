"""Every check the survey runs, proven able to fail: the smallest defect that
makes each one find something, planted on a probe page of its own. A check
with no plant here is one nothing shows can fail, so a new check lands with
its plant (test_browser_probes holds the two lists to each other). A plant
that is a flex row sets its ``align-items``, so the row does not stretch its
items to one height and hide the defect."""

import json
from dataclasses import dataclass, field
from typing import Any, override

from hq.platform.application.pages import Page, PageAction
from hq.platform.core.tests.browser.gate import add_css, with_script
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.widths import WIDE


@dataclass(frozen=True)
class Plant:
    """A defect as markup on a probe page.

    ``css`` is added once the page is drawn, for a rule on a part a partial
    writes; ``then`` runs once it is drawn, for a defect that is a change
    rather than markup, which ``scripted`` runs with script on. ``path`` is
    the page the survey reads it as, for a check it runs on one page only.
    ``values`` are what the markup prints (``probe``'s)."""

    markup: str
    css: str = ""
    then: str = ""
    scripted: bool = False
    path: str = ""
    values: dict[str, Any] = field(default_factory=dict)


_LINES = "Example words enough to run over several lines of a narrow box"

_TABLE = '<table class="data-table" style="table-layout: fixed; inline-size: 10rem"><tbody><tr><td>{}</td></tr></tbody></table>'

# Each check of the survey, by name, and its smallest defect.
PLANTS = {
    # A box that hides what it holds past its edge, as clipped-content finds a card.
    "sideways": Plant(
        '<div class="card" style="inline-size: 8rem; overflow: hidden">'
        '<span style="white-space: nowrap">Example value far wider than its box</span></div>'
    ),
    "cells": Plant(_TABLE.format('<div style="inline-size: 20rem">Example</div>')),
    # A row's button set on its cell's top, as a button taller than a line is
    # without the row's rule.
    "actions on the first line": Plant(
        '<table class="data-table"><tbody><tr><td>Example</td><td class="row-actions">'
        '<button type="button" class="btn compact" style="translate: none">Example</button></td></tr></tbody></table>'
    ),
    # A row's two buttons one above the other in a cell of a table with room
    # for them side by side.
    "row actions stacked with room": Plant(
        '<table class="data-table"><tbody><tr><td>Example</td><td class="row-actions">'
        '<button type="button" class="btn compact" style="display: block">Example one</button>'
        '<button type="button" class="btn compact" style="display: block">Example two</button></td></tr></tbody></table>'
    ),
    "broken words": Plant(
        _TABLE.format('<span style="display: block; inline-size: 3ch; word-break: break-all">Example</span>')
    ),
    "words broken in text": Plant('<p style="inline-size: 3ch; word-break: break-all">Example</p>'),
    # A name as wide as its line, the dot after it held to it by a no-break
    # space outside its box, which opens the next line.
    "lines opening on a stray": Plant(
        '<p style="inline-size: 12ch"><span style="display: inline-block; inline-size: 100%">example-name</span>'
        "&nbsp;· Example</p>"
    ),
    # A host name drawn on two lines of a cell, in a table with nothing to let go.
    "broken identifiers": Plant(
        _TABLE.format(
            '<span style="display: block; inline-size: 5ch"><span class="token" data-sole style="white-space: normal">'
            "status.<wbr>example.com</span></span>"
        )
    ),
    "overlaps": Plant(
        '<div style="display: grid"><div style="grid-area: 1 / 1; block-size: 3rem"></div>'
        '<div style="grid-area: 1 / 1; block-size: 3rem"></div></div>'
    ),
    "into a neighbor": Plant(
        '<div style="display: flex"><div style="flex: none; inline-size: 6rem">'
        '<span style="display: inline-block; inline-size: 12rem; background: #888">Example</span></div>'
        '<div style="flex: none; inline-size: 6rem">Example</div></div>'
    ),
    "overlapping text": Plant(
        '<div style="position: relative"><p style="position: absolute; inset: 0; margin: 0">Example one</p>'
        '<p style="margin: 0">Example two</p></div>'
    ),
    # A heading and its status as items of a flex row, the space between them dropped.
    "touching text": Plant('<div style="display: flex"><h2>Example</h2> <span>Example status</span></div>'),
    "chips off their paint": Plant(
        '<p><code style="display: inline-block; inline-size: 4ch; block-size: 1lh; background: #eee">Example overrun</code></p>'
    ),
    "tables": Plant(_TABLE.format("Example").replace('style="', 'style="display: block; ', 1)),
    "tables fit": Plant(
        '<div class="table-scroll" style="inline-size: 10rem"><table class="data-table" style="min-inline-size: 20rem">'
        "<tbody><tr><td>Example</td></tr></tbody></table></div>"
    ),
    # A column hidden while a cell still spans it, so the table keeps its width.
    "unseen columns": Plant(
        '<div class="table-scroll"><table class="data-table" style="inline-size: 100%">'
        '<colgroup><col><col style="inline-size: 50%"></colgroup><thead><tr>'
        '<th>Example</th><th style="display: none">Gone</th></tr></thead><tbody>'
        '<tr><td>Example</td><td style="display: none">Gone</td></tr>'
        '<tr><td colspan="2">Example spanning both</td></tr></tbody></table></div>'
    ),
    "control labels": Plant(
        '<button type="button" style="inline-size: 2rem; white-space: nowrap">Example label</button>'
    ),
    "cell labels": Plant(
        _TABLE.format(
            '<span style="display: inline-block; inline-size: 2rem; white-space: nowrap">Example label</span>'
        )
    ),
    # Two targets far under 24px, side by side.
    "small targets": Plant(
        '<div><a href="#" style="display: inline-block; font-size: 8px; line-height: 8px">x</a>'
        '<a href="#" style="display: inline-block; font-size: 8px; line-height: 8px">y</a></div>'
    ),
    # A control flush with the edge of a box that clips, its ring drawn outside it.
    "cut rings": Plant(
        '<div style="overflow: clip"><a href="#" style="display: block; outline: 2px solid; outline-offset: 2px">'
        "Example</a></div>"
    ),
    # A clamp the box's display leaves inert.
    "past the clamp": Plant(f'<p style="display: block; -webkit-line-clamp: 1; inline-size: 8rem">{_LINES}</p>'),
    "cut decisions": Plant(
        f'<p class="attention-detail" style="max-block-size: 1.5em; overflow: hidden; inline-size: 8rem">{_LINES}</p>'
    ),
    # An item's meaning held to a measure, an empty band between it and its buttons.
    "short of its buttons": Plant(
        '<ul class="attention-list"><li class="attention-row" data-attention-item><div class="attention-body">'
        '<p class="attention-detail" style="max-inline-size: 8rem">Example</p></div>'
        '<div class="attention-end"><a class="btn" href="/">Example</a></div></li></ul>'
    ),
    # A name in a sentence cut at a cap, as a cell cuts its own identifier.
    "cut code in text": Plant(
        '<p>Example <code style="display: inline-block; max-inline-size: 4ch; overflow: hidden; white-space: nowrap">'
        "example_identifier</code> words.</p>"
    ),
    # A short name cut in a table with room to spare.
    "cut with room": Plant(
        '<div class="table-scroll"><table class="data-table"><tbody><tr><td>'
        '<span class="token" data-sole style="display: inline-block; max-inline-size: 3ch; overflow: hidden; '
        'white-space: nowrap">example-name</span></td><td>Example</td></tr></tbody></table></div>'
    ),
    # A value cut short of the room its cell holds, beside a column wider than it asks.
    "cut beside room": Plant(
        '<div class="table-scroll"><table class="data-table"><tbody><tr><td>'
        '<span style="display: block; max-inline-size: 6ch; overflow: hidden; text-overflow: ellipsis; '
        'white-space: nowrap">Example value cut short</span></td><td>Example</td></tr></tbody></table></div>'
    ),
    # A value cut with an ellipsis in a row with no toggle to open it.
    "cut and closed": Plant(
        _TABLE.format(
            '<span style="display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">'
            "an-example-value-far-too-long-for-its-cell</span>"
        )
    ),
    # The same in a row drawn open, held past the stylesheet's lifting of an
    # open row's cuts.
    "cut when open": Plant(
        _TABLE.replace("<tr>", '<tr class="is-expanded">').format(
            '<span style="display: block; overflow: hidden !important; text-overflow: ellipsis !important; '
            'white-space: nowrap !important">an-example-value-far-too-long-for-its-cell</span>'
        )
    ),
    "changes": Plant(
        '<p>Example {% include "partials/_change.html" with from="1.0" to="2.0" %}</p>',
        css=".change-arrow { position: relative; inset-block-start: .2em; }",
    ),
    "chips": Plant('<p>Example words <code style="vertical-align: -.2em">example</code> and more words</p>'),
    # A band's linked count set taller than the zero beside it, as the
    # Agents card's counts were.
    "fact rows": Plant(
        '{% facts layout="band" %}{% fact "Example rules" value=0 %}'
        '{% fact "Example refused" %}<a href="#" style="display: inline-block; vertical-align: baseline; line-height: 3rem">13</a>'
        "{% endfact %}{% endfacts %}"
    ),
    # Two short facts given half the band each, as the Agents card's were.
    "facts together": Plant(
        '{% facts layout="band" %}{% fact "Example rules" value=0 %}{% fact "Example refused" value=13 %}{% endfacts %}',
        css=".facts-band { grid-template-columns: repeat(2, minmax(0, 1fr)); }",
    ),
    # A detail laid out as a grid, which sets each run of its words apart.
    "fact details in words": Plant(
        '{% facts layout="band" %}{% fact "Example host" %}Up{% detail %}on <a href="#">example-host</a> today'
        "{% endfact %}{% endfacts %}",
        css=".fact-detail { display: grid; }",
    ),
    # A detail's link set a target tall by its top, off the words' baseline.
    "links in a fact's detail": Plant(
        '{% facts layout="band" %}{% fact "Example host" %}Up{% detail %}<small>on <a href="#">example-host</a> today</small>'
        "{% endfact %}{% endfacts %}",
        css=".fact-detail a { display: inline-block; vertical-align: top; line-height: var(--target-min); }",
    ),
    # Facts in a frame that keeps no inset, as the container's Upgrade card's were.
    "facts inside their frame": Plant(
        '<div style="border: 1px solid; padding: 0">{% facts %}{% fact "Example" value="One" %}{% endfacts %}</div>'
    ),
    # A name cut where it overruns, set in a line on the bottom edge it clips at.
    "raised text": Plant(
        '<p>Example words <span style="display: inline-block; overflow: hidden">example-name</span> and more</p>'
    ),
    # A head whose lead control is hidden, which the menu never holds.
    "heads": Plant(
        '{% include "partials/_page_head.html" %}',
        css=".page-actions > :first-child { display: none; }",
        values={
            "page": Page(
                title="Example", actions=tuple(PageAction(name, f"/{name}/") for name in ("One", "Two", "Three"))
            )
        },
    ),
    # A section's heading set flush under the paragraph before it.
    "section heads": Plant(
        '<div><p style="margin: 0">Example</p><div class="section-head" style="margin-block-start: 0">'
        "<h2>Example</h2></div><p>Example</p></div>"
    ),
    # What sits beside a short heading held narrower than its one line.
    "section asides": Plant(
        '<header class="section-head"><h2>Example</h2>'
        '<div style="flex: none; inline-size: 4rem">Example aside words</div></header>'
    ),
    # A filled block in a section's frame, held in the frame's padding.
    "inset in the frame": Plant('{% section "Example" %}<div style="background: #eee">Example</div>{% endsection %}'),
    # Two sections side by side, their heads above their frames, one held
    # taller by what stands beside its heading than any control is. A head
    # the stylesheet stands inside its frame leaves the frames together.
    "frames in a row": Plant(
        '<div style="display: flex; align-items: start; gap: 1rem">'
        '{% section "Example one" head="above" %}<p>Example</p>{% aside %}'
        '<span style="display: inline-block; block-size: 4rem">Example</span>{% endsection %}'
        '{% section "Example two" head="above" %}<p>Example</p>{% endsection %}</div>'
    ),
    # A frame stopped at its words' width in the page's column.
    "frames span their column": Plant('<div class="card" style="inline-size: fit-content">Example</div>'),
    # A head that spreads its items, a marker set before its heading.
    "titles at the head's start": Plant(
        '<header class="section-head"><span style="flex: none">x</span><h2>Example</h2></header>'
    ),
    # A select narrower than its chosen value, and a button whose words wrap.
    "control words on one line": Plant(
        '<select aria-label="Example" style="inline-size: 5rem"><option>Example group name</option></select>'
        '<button type="button" class="btn" style="inline-size: 5rem; white-space: normal">Example label words</button>'
    ),
    # A toolbar's control held taller than the one beside it.
    "one height to a row of controls": Plant(
        '<div style="display: flex; align-items: center; gap: 1rem"><button type="button" class="btn">Example</button>'
        '<button type="button" class="btn" style="min-block-size: calc(var(--control-size) + 4px)">Example</button></div>'
    ),
    # A select left to the browser's own arrow.
    "select arrows": Plant(
        '<select aria-label="Example" style="appearance: auto; background-image: none"><option>Example</option></select>'
    ),
    # A link among words whose name is set in a box of its own.
    "link underlines": Plant(
        '<p>Example <a href="#"><span style="display: inline-block; text-decoration: none">example-name</span></a></p>'
    ),
    # A link alone on its line, underlined at rest.
    "links at rest": Plant('<div><a href="#" style="text-decoration: underline">Example</a></div>'),
    # A link whose line runs under the comma after its name.
    "lined past the name": Plant('<p>Runs on <a href="#">example-host,</a> container example.</p>'),
    "dialogs": Plant('{% include "partials/_modal.html" with id="orphan" title="Orphan" %}'),
    # A list of names wrapping beside another pair of a four-column list.
    "lists of facts": Plant(
        '{% facts %}{% fact "Names" %}<span class="code-list"><code>example-one</code><code>example-two</code>'
        '<code>example-three</code></span>{% endfact %}{% fact "Other" value="Example" %}{% endfacts %}',
        css=".facts-list { grid-template-columns: 5rem 6rem 5rem 6rem } .facts-list > .fact { grid-column: span 2 }",
    ),
    # A card of a few facts across the page, a section beside it in the
    # page's column rather than sharing its row.
    "lists across an empty frame": Plant(
        '{% card label="Example" %}{% facts %}{% fact "Example" value="One" %}{% endfacts %}{% endcard %}'
        '{% section "Example" %}<p>Example</p>{% endsection %}'
    ),
    # A folded section whose steps sit bare on the page, closed as most are.
    "bare folds": Plant(
        '<details class="section-fold"><summary><h2>Example</h2></summary><p>Example step</p></details>'
    ),
    # A card's heading dressed as a kicker.
    "headings in capitals": Plant('<div class="card"><h2 style="text-transform: uppercase">Example</h2></div>'),
    # A cell's Change link set in the line of the name before it.
    "quiet links in the line": Plant(
        _TABLE.format(
            '<a href="/example/">Example</a> <a class="subtle-link" href="/example/" style="display: inline">Change</a>'
        )
    ),
    "gutter": Plant('<div style="inline-size: calc(100% + 2rem)">Example</div>'),
    # A box cut to four lines that scrolls the rest, with no cap that says so.
    "audit": Plant(
        '<div style="display: flex; flex-direction: column; block-size: 4rem">'
        f'<div style="overflow: auto">{"<br>".join(["Example"] * 5)}</div></div>'
    ),
    # A block let in above the page once it is drawn: an engine that keeps no
    # record of layout shifts (WebKit) has nothing to say, so Chromium's legs
    # are where this one is proven.
    "layout shifts": Plant(
        "<p>Example</p>",
        then="""() => {
          const block = document.createElement('div');
          block.style.setProperty('block-size', '600px');
          document.querySelector('main').prepend(block);
        }""",
        scripted=True,
    ),
    "axe": Plant('<button type="button"></button>'),
    # A reading's line drawn under its mark, on the one page the survey reads them.
    "readings": Plant(
        '<div class="dash-readings"><div class="glance-panel-head" style="display: block">'
        '<span class="glance-symbol">x</span><span style="display: block">Example reading</span></div></div>',
        path="/",
    ),
    "contrast": Plant('<p style="color: #c8c8c8; background: #fff">Example words too faint to read</p>'),
    "fields": Plant(
        '<input type="text" aria-label="Example" style="border: 1px solid #fafafa; background: transparent">'
    ),
    "button edges": Plant('<button type="button" style="border: 1px solid var(--field-line)">Example</button>'),
    # A fill the light palette shows and the dark one drops.
    "fills": Plant('<p><span style="background: light-dark(#ccc, transparent)">Example</span></p>'),
    # A filter bar's one choice grown over the bar.
    "filter choices as wide as their options": Plant(
        '{% load filter_tags %}{% filter_bar %}<select name="example" aria-label="Example" style="flex-grow: 1">'
        "<option>Example</option></select>{% endfilter_bar %}"
    ),
}

# What a check passes that is close to what it finds: the case its plant
# differs from, so a check that finds everything is caught as surely as one
# that finds nothing.
PASSES = {
    # The dot held inside the name's box, a count's noun held to its figure,
    # a run of parts whose dot before a part that opens a row is clipped, and
    # a filter bar whose search fills its row, a field for words and no mark.
    "lines opening on a stray": Plant(
        "{% load filter_tags %}"
        '<p style="inline-size: 12ch"><span style="display: inline-block; inline-size: 100%">example-name&nbsp;·</span>'
        ' Example</p><p style="inline-size: 6ch">Example 12&nbsp;days</p>'
        '<ul class="dot-list" style="inline-size: 12ch"><li>Example one</li><li>Example two</li></ul>'
        '<div style="inline-size: 16rem">{% filter_bar %}<input type="search" name="q" aria-label="Example"'
        ' placeholder="Example names, vendors and notes">{% endfilter_bar %}</div>'
    ),
    # Three short facts of a band, together at its start however wide it is.
    "facts together": Plant(
        '{% facts layout="band" %}{% fact "Example rules" value=0 %}{% fact "Example held" value=2 %}'
        '{% fact "Example refused" value=13 %}{% endfacts %}'
    ),
    # A frame across its column, and frames that share a row.
    "frames span their column": Plant(
        '<div class="card">Example</div><div style="display: flex; gap: 1rem">'
        '<div class="card">Example</div><div class="card">Example</div></div>'
    ),
    # The same card and section sharing a row in the flow, then the card
    # alone in a flow of its own, as narrow as what it holds.
    "lists across an empty frame": Plant(
        '<div class="section-flow">{% card label="Example" %}{% facts %}{% fact "Example" value="One" %}'
        '{% endfacts %}{% endcard %}{% section "Example" %}<p>Example</p>{% endsection %}</div>'
        '<div class="section-flow">{% card label="Example" %}{% facts %}{% fact "Example" value="One" %}'
        "{% endfacts %}{% endcard %}</div>"
    ),
    "titles at the head's start": Plant(
        '{% section "Example" %}<p>Example</p>{% aside %}<a href="#">Example</a>{% endsection %}'
    ),
    # A row's controls one above the other where its table has no room for
    # them side by side.
    "row actions stacked with room": Plant(
        '<div style="inline-size: 12rem"><table class="data-table"><tbody><tr><td>Example</td><td class="row-actions">'
        '<button type="button" class="btn compact" style="display: block">Example one</button>'
        '<button type="button" class="btn compact" style="display: block">Example two</button></td></tr></tbody></table></div>'
    ),
    "control words on one line": Plant(
        '<select aria-label="Example"><option>Example group name</option></select>'
        '<button type="button" class="btn">Example label words</button>'
    ),
    # A value cut at its cell's edge beside a sentence that wraps: its row has
    # no room left. The second fills a padded box, whose padding is no room;
    # the third is cut beside room too narrow to show it whole.
    "cut beside room": Plant(
        '<div class="table-scroll" style="inline-size: 20rem"><table class="data-table"><tbody><tr><td>'
        '<span style="display: block; contain: inline-size; min-inline-size: 6ch; overflow: hidden; '
        'text-overflow: ellipsis; white-space: nowrap">Example value cut short</span></td>'
        f"<td>{_LINES}</td></tr><tr><td>"
        '<span style="display: block; padding-inline: 1.5rem"><span style="display: block; contain: inline-size; '
        'min-inline-size: 6ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">'
        "Example value cut short</span></span></td>"
        f"<td>{_LINES}</td></tr><tr><td>"
        '<span style="display: block; max-inline-size: 6ch; overflow: hidden; text-overflow: ellipsis; '
        'white-space: nowrap">an-example-value-far-longer-than-the-room-its-cell-leaves-beside-it</span></td>'
        f"<td>{_LINES}</td></tr></tbody></table></div>"
    ),
    # A button, a field and a select in one row, each the control size.
    "one height to a row of controls": Plant(
        '<div style="display: flex; gap: 1rem"><button type="button" class="btn">Example</button>'
        '<input type="search" aria-label="Example"><select aria-label="Example"><option>Example</option></select></div>'
    ),
    # A filter bar's choice in a bar too narrow for its option, shrunk to it.
    "filter choices as wide as their options": Plant(
        '<div style="inline-size: 10rem">{% load filter_tags %}{% filter_bar %}'
        '<select name="example" aria-label="Example"><option>Example option far wider than its bar</option></select>'
        "{% endfilter_bar %}</div>"
    ),
}

_BARE = "planted/bare"

# A style a page a test puts together writes, against a rule of HQ's that
# names its element more closely, and against one of HQ's !important ones:
# the open row's lift of every cut.
_LIFTED = "planted/lifted"
_OUTRANKED = (
    '<div class="table-scroll"><table class="data-table"><tbody><tr class="is-expanded">'
    '<td id="example-specific" style="text-align: right; padding: 7px">'
    '<span id="example-important" style="display: block; overflow: hidden !important">Example</span>'
    "</td></tr></tbody></table></div>"
)


def _said(finding):
    """A finding as a value a set holds: the audit's are dicts."""

    return json.dumps(finding, sort_keys=True)


def page_of(check, kind="planted"):
    return f"{kind}/{check.replace(' ', '-').replace("'", '')}"


class PlantedBrowserTests(LayoutBrowserTests):
    """Each check of the survey finds in its plant what it does not find on
    the bare probe page, so what it finds is the plant's."""

    @classmethod
    @override
    def render(cls):
        pages = {page_of(check): probe(plant.markup, **plant.values) for check, plant in PLANTS.items()}
        passes = {page_of(check, "passes"): probe(plant.markup, **plant.values) for check, plant in PASSES.items()}
        return {**pages, **passes, _BARE: probe(""), _LIFTED: probe(_OUTRANKED)}

    def found(self, name, plant):
        self.open(name, WIDE)
        if plant.css:
            add_css(self.page, plant.css)
        if plant.then:
            self.settle()
            self.page.evaluate(plant.then)
            self.settle()
        return self.findings(plant.path or name, WIDE)

    def assert_each_found(self, scripted):
        bare: dict[str, dict] = {}
        for check, plant in PLANTS.items():
            if plant.scripted != scripted:
                continue
            with self.subTest(check=check):
                if plant.path not in bare:
                    bare[plant.path] = self.found(_BARE, Plant("", path=plant.path))
                found = self.found(page_of(check), plant)
                if check == "layout shifts" and not self.records_layout_shifts():
                    continue
                planted = set(map(_said, found.get(check, []))) - set(map(_said, bare[plant.path].get(check, [])))
                self.assertTrue(planted, f"{check} finds nothing of its plant: {found}")

    def test_each_check_passes_its_near_miss(self):
        for check, plant in PASSES.items():
            with self.subTest(check=check):
                self.assertEqual(self.found(page_of(check, "passes"), plant).get(check, []), [])

    def records_layout_shifts(self):
        return self.page.evaluate("() => PerformanceObserver.supportedEntryTypes.includes('layout-shift')")

    def test_each_check_finds_its_plant_with_script_off(self):
        self.assert_each_found(scripted=False)

    @with_script
    def test_each_check_finds_its_plant_with_script_on(self):
        self.assert_each_found(scripted=True)

    def test_a_style_a_test_writes_outranks_hq_s_rules_as_written_inline(self):
        """The gate serves a test's styles from a sheet, the only kind the
        policy admits, and they still win where an inline style would."""

        self.open(_LIFTED, WIDE)
        drawn = self.page.evaluate(
            """() => {
              const [cell, span] = ['example-specific', 'example-important'].map((id) => getComputedStyle(document.getElementById(id)));
              return [cell.textAlign, cell.paddingTop, span.overflow];
            }"""
        )
        self.assertEqual(drawn, ["right", "7px", "hidden"])
