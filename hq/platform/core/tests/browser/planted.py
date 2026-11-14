"""Every check the survey runs, proven able to fail: the smallest defect that
makes each one find something, planted on a probe page of its own. A check
with no plant here is one nothing shows can fail, so a new check lands with
its plant (test_browser_probes holds the two lists to each other). A plant
that is a flex row sets its ``align-items``, so the row does not stretch its
items to one height and hide the defect."""

import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override
from unittest import mock

from django.conf import settings

from hq.platform.core.tests.browser import gate
from hq.platform.core.tests.browser.catalog import CATALOG
from hq.platform.core.tests.browser.gate import add_css, with_script
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.shifts import CLS_LIMIT, LAYOUT_SHIFTS
from hq.platform.core.tests.browser.probes.tables import TABLES
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.survey import CHECKS
from hq.platform.core.tests.browser.widths import SPARSE_WIDTHS, WIDE


@dataclass(frozen=True)
class Plant:
    """A defect as markup on a probe page.

    ``css`` is added once the page is drawn, for a rule on a part a partial
    writes; ``then`` runs once it is drawn, for a defect that is a change
    rather than markup, which ``scripted`` runs with script on, or a read
    that changes how the page measures (a fold's content laid out). ``path`` is
    the page the survey reads it as, for a check it runs on one page only.
    ``values`` are what the markup prints (``probe``'s)."""

    markup: str
    css: str = ""
    then: str = ""
    scripted: bool = False
    path: str = ""
    values: dict[str, Any] = field(default_factory=dict)


_LINES = "Example words enough to run over several lines of a narrow box"

# Two readings open, each head tinted as its open panel tints it, beside each
# other at different heights, one's quiet line wrapping: a disclosure's face,
# never a label.
_OPEN_READINGS = (
    '<div style="display: flex; align-items: start; gap: 1rem">'
    + "".join(
        '<div class="reading"><button type="button" class="reading-head" '
        f'style="inline-size: 10rem; background: var(--bg-tint)">{words}</button></div>'
        for words in ("Example", _LINES)
    )
    + "</div>"
)

_TABLE = '<table class="data-table" style="table-layout: fixed; inline-size: 10rem"><tbody><tr><td>{}</td></tr></tbody></table>'

# Each check of the survey, by name, and its smallest defect.
PLANTS = {
    # A box that hides what it holds past its edge, as clipped-content finds a card.
    "sideways": Plant(
        '<div class="card" style="inline-size: 8rem; overflow: hidden">'
        '<span style="white-space: nowrap">Example value far wider than its box</span></div>'
    ),
    "cells": Plant(_TABLE.format('<div style="inline-size: 20rem">Example</div>')),
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
    # Held only once script has run, which alone fits a table to its box.
    "tables fit": Plant(
        '<div class="table-scroll" style="inline-size: 10rem"><table class="data-table" style="min-inline-size: 20rem">'
        "<tbody><tr><td>Example</td></tr></tbody></table></div>",
        scripted=True,
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
        f'<p data-attention-detail style="max-block-size: 1.5em; overflow: hidden; inline-size: 8rem">{_LINES}</p>'
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
    # A value cut with an ellipsis in a row with no toggle to open it, and no
    # title, which only script gives a cut.
    "cut and closed": Plant(
        _TABLE.format(
            '<span style="display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">'
            "an-example-value-far-too-long-for-its-cell</span>"
        ),
        scripted=True,
    ),
    # The same in a row drawn open, held past the stylesheet's lifting of an
    # open row's cuts.
    "cut when open": Plant(
        _TABLE.replace("<tr>", '<tr class="is-expanded">').format(
            '<span style="display: block; overflow: hidden !important; text-overflow: ellipsis !important; '
            'white-space: nowrap !important">an-example-value-far-too-long-for-its-cell</span>'
        )
    ),
    # A flow whose first member the stylesheet paints after the second.
    "flows in reading order": Plant(
        '<div class="section-flow"><section style="order: 1"><p>Example</p></section>'
        "<section><p>Example two</p></section></div>"
    ),
    # A select narrower than its chosen value, and a button whose words wrap.
    "control words on one line": Plant(
        '<select aria-label="Example" style="inline-size: 5rem"><option>Example group name</option></select>'
        '<button type="button" class="btn" style="inline-size: 5rem; white-space: normal">Example label words</button>'
    ),
    # A link alone on its line, underlined at rest.
    "links at rest": Plant('<div><a href="#" style="text-decoration: underline">Example</a></div>'),
    "dialogs": Plant('{% include "partials/_modal.html" with id="orphan" title="Orphan" %}'),
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
    # A reading's line drawn under its mark.
    "readings": Plant(
        '<div class="readings"><div class="reading-head" style="display: block">'
        '<span class="reading-mark">x</span><span style="display: block">Example reading</span></div></div>',
        path="/",
    ),
    # A row's two buttons in a column beside its words, which wrap a few to a
    # line in a table too narrow for both.
    "row actions beside squeezed words": Plant(
        '<table class="data-table" style="inline-size: 16rem"><tbody><tr>'
        f"<td>{_LINES}</td>"
        '<td class="row-actions" style="white-space: nowrap"><button type="button" class="btn compact">Example one</button> '
        '<button type="button" class="btn compact">Example two</button></td></tr></tbody></table>'
    ),
    # A row in a narrow box whose button shows only in a whole box, so in the
    # narrow one it waits behind the row's toggle.
    "row actions behind the toggle": Plant(
        '<table class="data-table" style="--box: narrow"><tbody><tr>'
        '<td>Example</td><td class="row-actions">'
        '<button type="button" class="btn compact example-held">Example</button></td></tr></tbody></table>',
        css="@container style(--box: narrow) { .example-held { display: none; } }",
    ),
    # A button's word drawn at no size, a glyph standing in its place.
    "words swapped for a glyph": Plant(
        '<button type="button" class="btn compact example-glyph" style="font-size: 0">Example</button>',
        css='.example-glyph::before { content: "\\00d7" / ""; font-size: 1rem; }',
    ),
    # A button's words drawn by `breakable` straight into its flex box.
    "control words in one box": Plant(
        '{% load value_tags %}<button type="button" class="btn compact">{{ words|breakable }}</button>',
        values={"words": "Open example-host"},
    ),
    # A row's words balanced, every line ending short of the room beside it,
    # and a row's words held to a column narrower than its button leaves them.
    "row words to the end of their line": Plant(
        '<table class="data-table"><tbody><tr><td><p style="inline-size: 20rem; text-wrap: balance">'
        "Example words enough to run over two lines of a row with room to spare</p></td></tr>"
        '<tr><td style="inline-size: 12rem"><p>Example words enough to run over lines of a row with room</p></td>'
        '<td style="inline-size: 30rem; text-align: right"><button type="button" class="btn compact">Example</button></td></tr>'
        "</tbody></table>"
    ),
    # A folded row a rule draws as a line of its own.
    "folded rows stay folded": Plant(
        '<table class="data-table"><tbody><tr id="example-family" data-folds><td>Example family</td></tr>'
        '<tr data-folded="example-family" style="display: flex"><td>Example</td><td>Example</td></tr></tbody></table>',
        scripted=True,
    ),
    "contrast": Plant('<p style="color: #c8c8c8; background: #fff">Example words too faint to read</p>'),
    "fields": Plant(
        '<input type="text" aria-label="Example" style="border: 1px solid #fafafa; background: transparent">'
    ),
    "button edges": Plant('<button type="button" style="border: 1px solid var(--field-line)">Example</button>'),
    # A fill the light palette shows and the dark one drops.
    "fills": Plant('<p><span style="background: light-dark(#ccc, transparent)">Example</span></p>'),
}

_FLOWS = (
    '<div class="section-flow"><p>Example across the row</p>'
    + "".join(f"<section><p>Example {n}</p></section>" for n in range(5))
    + '</div><div class="section-flow"><section><p>Example</p></section><section><p>Example two</p></section>'
    "<p>Example across the row</p></div>"
)

# What a check passes that is close to what it finds: the case its plant
# differs from, so a check that finds everything is caught as surely as one
# that finds nothing.
PASSES = {
    # A row in a narrow box whose button leaves its cell and shows in the
    # words' cell instead, as the region copies it beneath the row's words.
    "row actions behind the toggle": Plant(
        '<table class="data-table" style="--box: narrow"><tbody><tr>'
        '<td>Example<div class="example-beneath">'
        '<button type="button" class="btn compact">Example</button></div></td><td class="row-actions">'
        '<button type="button" class="btn compact example-held">Example</button></td></tr></tbody></table>',
        css=(
            "@container style(--box: narrow) { .example-held { display: none; } }"
            " @container style(--box: whole) { .example-beneath { display: none; } }"
        ),
    ),
    # A link's word drawn at no size with no glyph in its place (a calendar's
    # sliver), and a glyph button named by no word of its own.
    "words swapped for a glyph": Plant(
        '<a class="btn compact" href="#example" style="font-size: 0">Example</a>'
        '<button type="button" class="btn compact example-glyph" aria-label="Example"></button>',
        css='.example-glyph::before { content: "\\00d7" / ""; font-size: 1rem; }',
    ),
    # The same words held in one box, and a button of one word box alone.
    "control words in one box": Plant(
        '{% load value_tags %}<button type="button" class="btn compact">{{ words|button_words }}</button>'
        '<button type="button" class="btn compact">{{ name|breakable }}</button>',
        values={"words": "Open example-host", "name": "example-host"},
    ),
    # A folded row and one its opened family draws.
    "folded rows stay folded": Plant(
        '<table class="data-table"><tbody><tr id="example-family" data-folds><td>Example family</td></tr>'
        '<tr data-folded="example-family"><td>Example</td><td>Example</td></tr>'
        '<tr data-folded="example-family" data-open><td>Example</td><td>Example</td></tr></tbody></table>',
        scripted=True,
    ),
    # The same words filling each line, and at a line's end what will not fit
    # there: a name, words a no-break space holds to their count, a code
    # chip whose padding takes the room its letters leave, a chip set as an
    # inline block that moves whole, and a line the markup ends.
    "row words to the end of their line": Plant(
        '<table class="data-table"><tbody><tr><td><p style="inline-size: 20rem">'
        "Example words enough to run over two lines of a row with room to spare</p>"
        '<p style="inline-size: 20rem">Example words then an-example-name-too-long-for-the-end</p>'
        '<p style="inline-size: 20rem">Example words then 1&nbsp;example&nbsp;held&nbsp;whole&nbsp;to&nbsp;its&nbsp;count</p>'
        '<p style="inline-size: 20rem">Example words then <code style="padding-inline: 6rem">name</code></p>'
        '<p style="inline-size: 20rem">Example words then <code style="display: inline-block">'
        "example chip of words held whole</code></p>"
        '<p style="inline-size: 20rem">Example words <br>then more</p>'
        "</td></tr></tbody></table>"
    ),
    "row actions beside squeezed words": Plant(
        '<table class="data-table" style="inline-size: 60rem"><tbody><tr>'
        f"<td>{_LINES * 3}</td>"
        '<td class="row-actions" style="white-space: nowrap"><button type="button" class="btn compact">Example one</button> '
        '<button type="button" class="btn compact">Example two</button></td></tr></tbody></table>'
    ),
    # A primary button caught as its fill fades in, its ink already drawn:
    # it is read as it settles.
    "contrast": Plant(
        '<button type="button" class="btn" data-example-fading>Example lookup</button>',
        css="[data-example-fading] { transition: background-color 1000s linear }",
        then="() => document.querySelector('[data-example-fading]').setAttribute('class', 'btn primary')",
    ),
    # The dot held inside the name's box, the dot after a clamped link that
    # opens its line, a count's noun held to its figure, a run of parts whose
    # dot before a part that opens a row is clipped, a filter bar whose
    # search fills its row, a field for words and no mark, a word a `br` puts
    # under a count, a box set whole whose first text is a path's code, and a
    # cell's list of codes wrapping between its values.
    "lines opening on a stray": Plant(
        "{% load filter_tags %}"
        + '<table class="data-table" style="inline-size: 28rem"><tbody><tr><td><code>{{ user|breakable }}</code> '
        f"<code>{{{{ account|breakable }}}}</code></td><td>{_LINES}</td></tr></tbody></table>"
        + '<p>Example 12<br>Example</p><p style="inline-size: 16ch">Example words <span style="display: inline-flex">'
        "<code>/etc/example-path</code></span></p>"
        '<p style="inline-size: 12ch"><span style="display: inline-block; inline-size: 100%">example-name&nbsp;·</span>'
        ' Example</p><p style="inline-size: 16ch">Example read through <a href="#" style="display: -webkit-inline-box;'
        ' -webkit-box-orient: vertical; -webkit-line-clamp: 2">Example Home</a>&nbsp;· Example</p>'
        '<p style="inline-size: 6ch">Example 12&nbsp;days</p>'
        '<ul class="dot-list" style="inline-size: 12ch"><li>Example one</li><li>Example two</li></ul>'
        '<div style="inline-size: 16rem">{% filter_bar %}<input type="search" name="q" aria-label="Example"'
        ' placeholder="Example names, vendors and notes">{% endfilter_bar %}</div>',
        values={"user": "root", "account": "an-example-service-account"},
    ),
    # A box that scrolls what it holds, inside a closed fold: no scrollbar is
    # drawn there.
    "audit": Plant(
        '<details><summary>Example</summary><div style="overflow: auto; inline-size: 4rem">'
        '<span style="white-space: nowrap">Example words wider than their box</span></div></details>'
    ),
    # A label wider than its box in a cell, in content the browser skips
    # until it is found.
    "cell labels": Plant(
        _TABLE.format(
            '<span hidden="until-found" style="display: inline-block; inline-size: 2rem; white-space: nowrap">'
            "Example label</span>"
        )
    ),
    # A wide member ahead of five short ones that wrap onto a second row, and
    # two short ones sharing a row ahead of a wide one, the last short one of
    # each alone with nothing after it to share its row.
    "flows in reading order": Plant(_FLOWS),
    "control words on one line": Plant(
        '<select aria-label="Example"><option>Example group name</option></select>'
        '<button type="button" class="btn">Example label words</button>' + _OPEN_READINGS
    ),
    # A value cut at its cell's edge beside a sentence that wraps: its row has
    # no room left. The second fills a padded box, whose padding is no room;
    # the third is cut beside room too narrow to show it whole. Last, a tight
    # table's status pill, a header's name too long for its column at its
    # narrowest, beside a short address whose column has room past it: the
    # pill's column takes that room and shows the name.
    "cut beside room": Plant(
        "{% load value_tags status_tags %}"
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
        '<div style="inline-size: 15rem">{% table_region "Example" %}'
        '<table class="data-table" data-narrow data-tight><thead><tr>'
        '{% table_heading "Value" "identifier" %}{% table_heading "Type" "status" %}</tr></thead><tbody>'
        '<tr><td><code class="cell-whole">{{ address|breakable }}</code></td>'
        '<td>{% status header tone="neutral" %}</td></tr></tbody></table>{% endtable_region %}</div>',
        values={"address": "192.0.2.10", "header": "X-Forwarded-Scheme"},
    ),
    # An address that breaks only at its seams in the muted line under a
    # row's name, and a file name among words in a muted cell, each in a whole
    # table too narrow for it: the line's address is cut on one line, and the
    # cell's name holds its length as any cell's does.
    "broken identifiers": Plant(
        '{% load value_tags %}<div style="inline-size: 4rem">{% table_region "Example" %}'
        '<table class="data-table"><thead><tr>{% table_heading "Name" "identifier" %}</tr></thead><tbody><tr>'
        '<td>Example<br><span class="muted">{{ address|breakable }}</span></td></tr></tbody></table>'
        '{% endtable_region %}</div><div style="inline-size: 4rem">{% table_region "Files" %}'
        '<table class="data-table"><thead><tr>{% table_heading "File" %}</tr></thead><tbody><tr>'
        '<td class="muted">{{ file|breakable }}</td></tr></tbody></table>{% endtable_region %}</div>',
        values={"address": "http://192.0.2.10:8000", "file": "Example receipt.png"},
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


# A word a name set whole leaves alone on its line in a cell, where no `br`
# stands between them.
_LEFT = "planted/left-behind"
_LEFT_BEHIND = _TABLE.format('on <span style="display: inline-block">example-name-wider-than-its-cell</span>')


# A run of parts whose rest asks its column for nothing and shows one line
# (`line_list`, written out so it stands without the stylesheet): one held
# short of the room its cell has by a box around it, which is cut beside
# room, and one in a cell it fills, which is cut for want of room.
_RESTS = "planted/rests"
_REST = (
    '<span style="display: flex; flex-wrap: wrap; align-items: baseline; {}">'
    '<span class="tag">{}-01</span><span style="display: flex; flex-wrap: wrap; align-items: baseline; align-self: start; '
    'flex: 1 1 0; inline-size: 0; min-inline-size: 0; max-block-size: 1lh; overflow: clip">'
    '<span style="align-self: start; block-size: 1lh"></span>'
    + "".join(f'<span class="tag">{{}}-{at:02}</span>' for at in range(2, 7))
    + "</span></span>"
)
_RESTED = (
    '<div class="table-scroll"><table class="data-table"><tbody><tr><td>Example</td><td>'
    + _REST.format("max-inline-size: 9rem", *["example-capped"] * 6)
    + '</td></tr></tbody></table></div><div class="table-scroll" style="inline-size: 14rem"><table class="data-table">'
    "<tbody><tr><td>" + _REST.format("", *["example-full"] * 6) + "</td></tr></tbody></table></div>"
)

# Who a layout shift is charged to, each page a part that one shift moves
# (`example-moved`) by its margin before it is removed or kept: a vendored
# part moved from inside its box to above it, as a viewer's skeleton is; one
# drawn across the box's edge and moved off it, as the viewer's header
# skeleton is; a part of HQ's outside the box; and a part of HQ's laid over the box in one
# grid cell, as the API reference's loading line shares the mount's. A box
# that is a flow root keeps the part's margin from moving the box itself.
_SHIFTED_OUT = "planted/shifted-out-of-the-box"
_SHIFTED_ACROSS = "planted/shifted-off-the-box-s-edge"
_SHIFTED_BESIDE = "planted/shifted-beside-the-box"
_SHIFTED_OVER = "planted/shifted-over-the-box"
_SHIFTS = {
    _SHIFTED_OUT: '<div data-vendored style="display: flow-root; block-size: 400px; margin-block-start: 420px">'
    '<div id="example-moved" style="block-size: 300px; margin-block-start: 20px">Example</div></div>',
    _SHIFTED_ACROSS: '<div data-vendored style="display: flow-root; block-size: 400px; margin-block-start: 420px">'
    '<div id="example-moved" style="block-size: 300px; margin-block-start: -50px">Example</div></div>',
    _SHIFTED_BESIDE: '<div style="display: flow-root; block-size: 800px"><div id="example-moved" '
    'style="block-size: 300px; margin-block-start: 420px">Example</div></div>'
    '<div data-vendored style="block-size: 100px"></div>',
    _SHIFTED_OVER: '<div style="display: grid"><div id="example-moved" style="grid-area: 1 / 1; block-size: 300px; '
    'margin-block-start: 420px">Example</div><div data-vendored style="grid-area: 1 / 1; block-size: 800px"></div></div>',
}

# A page whose one table waits in a closed disclosure, as a chart's table of
# figures does: drawn by nothing until it opens.
_FOLDED = "planted/a-table-in-a-closed-disclosure"
_FOLDED_TABLE = (
    '<details><summary>Example figures</summary><div class="table-scroll"><table class="data-table">'
    "<thead><tr><th>Example</th></tr></thead><tbody><tr><td>Example</td></tr></tbody></table></div></details>"
)


# A catalog entry `dev:look` draws by name, and a defect laid over it as CSS,
# which the look lays as well (`--css`): what a failure's look is proven on.
_LOOKED = "part/section-bare"
_LOOKED_DEFECT = "main p { inline-size: 3ch; word-break: break-all }"
_LOOKED_CHECK = "words broken in text"
# What a failed subtest says: its findings, then the look that finds them again.
_REPORTED = re.compile(r"AssertionError: (\{.*?\n\})\n\nFound again by: (.+)", re.DOTALL)
# What the look prints of the report it keeps.
_LOOK_REPORT = re.compile(r"Page by page: (\S+)")
# How long the look may take: a fresh checkout migrates its database first.
_LOOK_WITHIN = 900
# What a test run sets that a look run inside it must not inherit: its task's
# arguments, the gate's record of failed tests, the engine and database the
# look sets for itself.
_RUN_OWN = ("usage_", "HQ_", "SEVERINO_TEST_", "MISE_TASK_", "DJANGO_")


def _said(finding):
    """A finding as a value a set holds: the audit's are dicts."""

    return json.dumps(finding, sort_keys=True)


def page_of(check, kind="planted"):
    return f"{kind}/{check.replace(' ', '-').replace("'", '')}"


class PlantedBrowserTests(LayoutBrowserTests):
    """Each check of the survey finds in its plant what it does not find on
    the bare probe page, so what it finds is the plant's."""

    # A plant's page is the test's own, which `dev:look` cannot draw.
    looked_at = False

    @classmethod
    @override
    def render(cls):
        pages = {page_of(check): probe(plant.markup, **plant.values) for check, plant in PLANTS.items()}
        passes = {page_of(check, "passes"): probe(plant.markup, **plant.values) for check, plant in PASSES.items()}
        return {
            **pages,
            **passes,
            _LOOKED: CATALOG[_LOOKED].page(_LOOKED),
            _BARE: probe(""),
            _LIFTED: probe(_OUTRANKED),
            _LEFT: probe(_LEFT_BEHIND),
            _RESTS: probe(_RESTED),
            _FOLDED: probe(_FOLDED_TABLE),
            **{name: probe(markup) for name, markup in _SHIFTS.items()},
        }

    def found(self, name, plant):
        self.open(name, WIDE)
        if plant.css:
            add_css(self.page, plant.css)
        if plant.then:
            # With script off a page runs no frame callback to settle on.
            if plant.scripted:
                self.settle()
            self.page.evaluate(plant.then)
            if plant.scripted:
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

    def test_a_word_a_name_leaves_behind_opens_a_line_on_a_stray(self):
        found = self.found(_LEFT, Plant("")).get("lines opening on a stray", [])
        self.assertTrue([said for said in found if 'cuts "on" from "example-name' in said], found)

    def test_a_rest_that_asks_for_nothing_is_cut_beside_its_own_cell_s_room_only(self):
        found = self.found(_RESTS, Plant("")).get("cut beside room", [])
        self.assertTrue([said for said in found if '"example-capped' in said], found)
        self.assertFalse([said for said in found if '"example-full' in said], found)

    def test_a_page_whose_only_table_is_folded_away_holds_the_table_rule(self):
        """A table in a closed disclosure is one the table probe skips as
        undrawn, never one it failed to find."""

        self.open(_FOLDED, WIDE)
        self.tables_scroll_inside_their_own_container(_FOLDED, self.page.evaluate(TABLES))

    def shifted(self, name, *, removed, given=(SELECTORS["vendored"], CLS_LIMIT)):
        """What the layout shift check finds once ``name``'s part has moved up
        420px by itself, and then been removed or kept."""

        self.open(name, WIDE)
        self.settle()
        if not self.records_layout_shifts():
            self.skipTest("this engine keeps no record of layout shifts")
        self.page.evaluate(
            """() => {
              const moved = document.getElementById('example-moved');
              moved.style.setProperty('margin-block-start', `${parseFloat(getComputedStyle(moved).marginBlockStart) - 420}px`);
            }"""
        )
        self.settle()
        if removed:
            self.page.evaluate("() => document.getElementById('example-moved').remove()")
            self.settle()
        return self.page.evaluate(LAYOUT_SHIFTS, given and list(given))

    @with_script
    def test_a_vendored_part_moved_out_of_its_box_and_removed_is_the_script_s(self):
        """A removed part is the script's when it was in the box before the
        shift, though the shift carried it out."""

        self.assertEqual(self.shifted(_SHIFTED_OUT, removed=True), [])

    @with_script
    def test_a_vendored_part_drawn_across_its_box_s_edge_and_removed_is_the_script_s(self):
        """A removed part the box held is the script's though the script drew
        it past the box's edge before the shift and wholly outside after."""

        self.assertEqual(self.shifted(_SHIFTED_ACROSS, removed=True), [])

    @with_script
    def test_a_part_of_hq_s_removed_after_it_shifted_is_hq_s(self):
        self.assertTrue(self.shifted(_SHIFTED_BESIDE, removed=True))

    @with_script
    def test_a_part_of_hq_s_still_in_the_page_within_the_box_s_bounds_is_hq_s(self):
        """A part still in the page is judged by what holds it, so HQ's laid
        over the box is HQ's wherever it is drawn."""

        self.assertTrue(self.shifted(_SHIFTED_OVER, removed=False))

    @with_script
    def test_the_layout_shift_check_run_alone_lists_every_shift_and_whose_parts_moved(self):
        """Given no argument, as `dev:look --probe` runs it alone, the check
        lists each shift whatever its score, each part marked as the
        script's or HQ's."""

        for name, removed, whose in ((_SHIFTED_OUT, True, "(the script's)"), (_SHIFTED_OVER, False, "(HQ's)")):
            with self.subTest(page=name):
                listed = self.shifted(name, removed=removed, given=None)
                self.assertTrue([line for line in listed if line.endswith(whose)], listed)

    def records_layout_shifts(self):
        return self.page.evaluate("() => PerformanceObserver.supportedEntryTypes.includes('layout-shift')")

    def test_each_check_finds_its_plant_with_script_off(self):
        self.assert_each_found(scripted=False)

    @with_script
    def test_each_check_finds_its_plant_with_script_on(self):
        self.assert_each_found(scripted=True)

    @with_script
    def test_the_ledger_has_a_row_for_every_check(self):
        """A survey's read times each check the survey holds, and nothing that
        is no check (the stand-in put back around the reads)."""

        from hq.platform.core.tests.browser.gate import CHECK_COSTS

        self.open(_BARE, WIDE)
        self.settle()
        self.findings(_BARE, WIDE)
        self.assertEqual(set(self.costs.get(CHECK_COSTS, {})), {*CHECKS, "fills"})

    def a_defect_laid_over_a_page_at_two_widths(self):
        """What the proof below runs as a test of its own: the entry as drawn,
        which holds, then with the defect laid over it at two widths."""

        self.looked_at = True
        for width, defect in (
            (SPARSE_WIDTHS[-1], ""),
            (SPARSE_WIDTHS[1], _LOOKED_DEFECT),
            (SPARSE_WIDTHS[2], _LOOKED_DEFECT),
        ):
            if not self.loaded(_LOOKED, width):
                continue
            if defect:
                self.lay_css(defect)
            self.hold(_LOOKED, width, [self.every_layout_check_holds])

    def test_a_failure_says_once_the_look_that_finds_it_again_with_each_place_and_a_trace_of_its_own(self):
        """A failed subtest says its findings whole and the `dev:look` that,
        run as said, finds them again; a finding found again at another width
        is said once, the place it was found again in the test's closing
        lines; and its trace holds only its own steps. One engine proves it:
        the command is built alike in each."""

        if self.browser.browser_type.name != "webkit":
            self.skipTest("proven in WebKit, the engine dev:look draws in by default")
        result = unittest.TestResult()
        said = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(gate, "browser_failures", lambda: Path(folder)),
            redirect_stdout(said),
        ):
            type(self)("a_defect_laid_over_a_page_at_two_widths").run(result)
            traces = list(Path(folder).rglob("*.zip"))
            steps = {
                json.loads(line).get("method")
                for trace in traces
                for line in zipfile.ZipFile(trace).read("trace.trace").decode().splitlines()
            }
        self.assertEqual(result.errors, [])
        [(_subtest, printed)] = result.failures
        if (reported := _REPORTED.search(printed)) is None:
            self.fail(printed)
        findings, command = json.loads(reported[1])[_LOOKED_CHECK], reported[2]
        self.assertIn(f"{_LOOKED} at {SPARSE_WIDTHS[2]}px", said.getvalue())
        self.assertEqual(len(traces), 1, traces)
        self.assertFalse(steps & {"goto", "setViewportSize"}, steps)

        argv = shlex.split(command)
        self.assertEqual(argv[:3], ["mise", "run", "dev:look"])
        env = {name: value for name, value in os.environ.items() if not name.startswith(_RUN_OWN)}
        ran = subprocess.run(
            argv, cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=_LOOK_WITHIN, check=False
        )
        out = ran.stdout + ran.stderr
        if (kept := _LOOK_REPORT.search(out)) is None:
            self.fail(out)
        drawn = Path(settings.BASE_DIR, kept[1]).read_text(encoding="utf-8")
        self.assertTrue(findings)
        for finding in findings:
            self.assertIn(json.dumps(finding), drawn)

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
