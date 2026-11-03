"""Probes of tables: their cells, rows, columns and gutter, and the tables drawn to probe them."""

from hq.platform.core.tests.browser.probes.describe import CUTS, DESCRIBE, PAINTED_PART

CELLS = (
    "() => {"
    + DESCRIBE
    + """
  // A cell's content stays in the cell. Inside a table that scrolls, running
  // past the edge is not caught by anything else: it lands under the next
  // column, readable in neither. What a box inside the cell cuts (a line cut
  // with an ellipsis) shows only as far as that box, which is held on its own.
  // A popover or a modal dialog shown is drawn in the top layer, over the
  // page, so the cell it sits in does not hold it.
  const found = [];
  const cut = (el, cell) => {
    for (let b = el.parentElement; b && b !== cell; b = b.parentElement) {
      if (getComputedStyle(b).overflowX !== 'visible') return true;
    }
    return false;
  };
  for (const cell of document.querySelectorAll('main td, main th')) {
    const edge = box(cell).right;
    for (const el of cell.querySelectorAll('*')) {
      const r = box(el);
      if (r.width < 2 || r.height < 2 || !el.checkVisibility() || el.closest(':popover-open, :modal')) continue;
      if (r.right > edge + 1 && !cut(el, cell)) {
        found.push(`${describe(el)} runs ${Math.round(r.right - edge)}px past its cell: ${el.textContent.trim().slice(0, 40)}`);
      }
    }
  }
  return [...new Set(found)];
}"""
)

TABLES = (
    "() => {"
    + DESCRIBE
    + """
  const width = document.documentElement.clientWidth;
  const found = [];
  let seen = 0;
  for (const table of document.querySelectorAll('main table')) {
    const r = box(table);
    // A table in a closed disclosure is not drawn, so it has no box to fit
    // until it opens; the rules read with every disclosure open read it then.
    if (!r.width || !table.checkVisibility()) continue;
    seen += 1;
    const name = describe(table);
    if (getComputedStyle(table).display !== 'table') found.push(`${name} is not laid out as a table`);
    const head = table.querySelector('thead');
    if (head && (getComputedStyle(head).display !== 'table-header-group'
                 || getComputedStyle(head).position === 'absolute')) {
      found.push(`${name} hides its column headings`);
    }
    // A row folded away is not drawn at all, which is not drawn as a card.
    for (const row of table.querySelectorAll('tbody tr')) {
      if (!['table-row', 'none'].includes(getComputedStyle(row).display)) {
        found.push(`${name} stacks its rows`); break;
      }
    }
    let scroller = table.parentElement;
    while (scroller && scroller !== document.body && !scrolls(scroller)) scroller = scroller.parentElement;
    if (!scroller || scroller === document.body) {
      const room = box(table.parentElement);
      if (r.right > room.right + 1) found.push(`${name} overflows with no scroll container of its own`);
      continue;
    }
    const s = box(scroller);
    if (s.right > width + 1 || s.left < -1) found.push(`${name} scrolls in a box wider than the page`);
  }
  return {seen, found};
}"""
)


# A row that arrives in a table already fitted, as the parser adds a long
# table's later rows after the first are fitted, then for every record's row
# the places where a cell shows in a column whose heading left, or the other
# way about.
ROW_ARRIVES = """async (scroll) => {
  const table = document.querySelector('main ' + scroll + ' > table');
  const headings = [...table.querySelectorAll(':scope > thead > tr:last-child > th')];
  // A record's row has a cell under every heading; a group's heading row and
  // the line beneath a row span them.
  const records = () => [...table.querySelectorAll(':scope > tbody > tr')]
    .filter((row) => row.children.length === headings.length);
  const record = records()[0];
  const arriving = record.cloneNode(true);
  arriving.querySelectorAll('[data-dropped]').forEach((cell) => cell.removeAttribute('data-dropped'));
  record.parentElement.append(arriving);
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const left = headings.map((heading) => heading.hasAttribute('data-dropped'));
  return records()
    .map((row) => [...row.children].map((cell, place) => cell.hasAttribute('data-dropped') !== left[place]))
    .flatMap((row, at) => row.flatMap((wrong, place) => (wrong ? [[at, place]] : [])));
}"""

# The first table on a page as a phone lays it out: whether it runs past its
# box, how wide each showing column is, how tall each record's row is, how
# many rows show their toggle, and what the open rows' lines beneath name.
# The line is the server's (`table_tags._detail_row`), found by attribute.
PHONE_TABLE = """(scroll) => {
  const box = document.querySelector('main ' + scroll);
  const table = box && box.querySelector('table');
  if (!table) return null;
  const showing = (el) => getComputedStyle(el).display !== 'none';
  return {
    sideways: Math.round(box.scrollWidth - box.clientWidth),
    headings: table.querySelectorAll('thead th').length,
    columns: [...table.querySelectorAll('thead th')].filter(showing)
      .map((heading) => Math.round(heading.getBoundingClientRect().width)),
    rows: [...table.querySelectorAll('tbody > tr:not([data-row-detail]):not(:has(> th[scope="rowgroup"]))')]
      .map((row) => Math.round(row.getBoundingClientRect().height)),
    toggles: [...table.querySelectorAll('tbody button[aria-expanded]')].filter((el) => el.checkVisibility()).length,
    details: [...table.querySelectorAll('tr[data-row-detail] dt')].filter((term) => term.checkVisibility()).map((term) => term.textContent),
  };
}"""

# Every table that is wider than the box it scrolls in, and for each table
# that has row toggles, how many different left edges its rows' names start on.
TABLES_THAT_DO_NOT_FIT = """(scroll) => [...document.querySelectorAll('main ' + scroll)]
  .filter((box) => box.checkVisibility() && !box.closest('details:not([open])'))
  .map((box) => [box, box.querySelector(':scope > table')])
  .filter(([box, table]) => table && table.querySelector('tbody td')
    && table.getBoundingClientRect().width > box.clientWidth + 1)
  .map(([box, table]) => (box.getAttribute('aria-label') || table.querySelector('th')?.textContent || '').trim().slice(0, 24)
    + ' +' + Math.round(table.getBoundingClientRect().width - box.clientWidth))"""


# A cell's identifier cut in a table whose columns still give way (no
# `data-narrow`, tables.js): one no longer than its column's hold
# (`--name-hold`, read off a box sized by it in the same cell) had the room to
# be shown whole.
CUT_WITH_ROOM = (
    "([boxes, holders, unseen]) => {"
    + DESCRIBE
    + CUTS
    + """
  const found = [];
  // What a name asks for on one line: its runs end to end, however a clamp
  // wraps them, inside its own padding.
  const asked = (el) => {
    const pad = getComputedStyle(el);
    let width = parseFloat(pad.paddingLeft) + parseFloat(pad.paddingRight);
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!textShown(node) || node.parentElement.closest(`${unseen}, [aria-hidden=true]`)) continue;
      cutRange.selectNodeContents(node);
      for (const r of cutRange.getClientRects()) width += r.width;
    }
    return Math.round(width);
  };
  for (const table of document.querySelectorAll('main table:not([data-narrow])')) {
    if (!table.checkVisibility() || table.closest('details:not([open])')) continue;
    for (const el of table.querySelectorAll(`:scope > tbody > tr:not([data-row-detail], :has(> * > [aria-expanded="true"])) > td ${boxes}`)) {
      if (!el.checkVisibility() || !cutsIn(el, unseen).some((run) => run.boxes.includes(el))) continue;
      const asks = asked(el);
      const cell = el.closest('td');
      const ruler = document.createElement('div');
      ruler.style.cssText = 'position: absolute; visibility: hidden; block-size: 0; inline-size: var(--name-hold)';
      cell.append(ruler);
      const hold = ruler.getBoundingClientRect().width;
      ruler.remove();
      // The hold bounds the name's whole box, an external link's arrow with it.
      let holder = el;
      for (let up = el.parentElement; up && up !== cell; up = up.parentElement) {
        if (up.matches(holders)) holder = up;
      }
      const beside = holder.getBoundingClientRect().width - el.clientWidth;
      if (asks + beside <= hold + 1)
        found.push(`${describe(el)} "${el.textContent.trim().slice(0, 40)}" is cut to ${el.clientWidth}px of ${asks}px though its column holds ${Math.round(hold)}px`);
    }
  }
  return [...new Set(found)];
}"""
)


# A value cut short that nothing shows whole: its row does not open at this
# width, and either the cut draws no ellipsis, so nothing tells a reader there
# is more to see, or, outside a narrow box (`--box`, tables.css), where a
# pointer can hover, nothing around it carries its whole text as a title or a
# tip (`data-tip`, app.js). Cut is read as `CUTS` reads it, up to the cell.
CUT_AND_CLOSED = (
    "(unseen) => {"
    + DESCRIBE
    + CUTS
    + """
  const found = [];
  const plain = (text) => (text || '').replace(/[\\s\\u00ad\\u200b\\u2060]+/g, '');
  // Hover text over the cut run that holds the whole of what its innermost
  // cutting box holds.
  const titled = (node, inner, cell) => {
    const whole = plain(inner.textContent);
    for (let up = node.parentElement; up && up !== cell.parentElement; up = up.parentElement) {
      const said = up.getAttribute('title') ?? up.dataset.tip;
      if (said && plain(said).includes(whole)) return true;
    }
    return false;
  };
  const rows = 'main table > tbody > tr:not([data-row-detail], .row-detail, .is-expanded)';
  for (const row of document.querySelectorAll(rows)) {
    if (!row.checkVisibility() || row.closest('details:not([open])')) continue;
    if ([...row.querySelectorAll('[aria-expanded]')].some((toggle) => toggle.checkVisibility())) continue;
    const narrow = getComputedStyle(row.closest('table')).getPropertyValue('--box').trim() === 'narrow';
    for (const cell of row.querySelectorAll(':scope > :is(td, th)')) {
      for (const {node, boxes, marked} of cutsIn(cell, unseen)) {
        const by = boxes.at(-1);
        const why = !marked ? 'with no ellipsis' : narrow ? 'in a narrow box' : titled(node, boxes[0], cell) ? '' : 'with no title';
        if (why) found.push(`${describe(by)} "${by.textContent.trim().slice(0, 40)}" is cut ${why}, in a row that does not open`);
      }
    }
  }
  return [...new Set(found)];
}"""
)


# A value cut short in a row that is open: the row and the line beneath it,
# which is how a narrow box shows a value whole, so nothing there may cut one.
CUT_WHEN_OPEN = (
    "(unseen) => {"
    + DESCRIBE
    + CUTS
    + """
  const found = [];
  const rows = 'main table > tbody > tr.is-expanded:not(.row-detail), main table > tbody > tr.is-expanded + tr.row-detail';
  for (const row of document.querySelectorAll(rows)) {
    if (!row.checkVisibility() || row.closest('details:not([open])')) continue;
    for (const cell of row.querySelectorAll(':scope > :is(td, th)')) {
      for (const {boxes} of cutsIn(cell, unseen)) {
        found.push(`${describe(boxes.at(-1))} "${boxes.at(-1).textContent.trim().slice(0, 40)}" is cut in a row that is open`);
      }
    }
  }
  return [...new Set(found)];
}"""
)


UNSEEN_COLUMNS = (
    "(scroll) => {"
    + DESCRIBE
    + """
  // A table's width belongs to the columns that are showing. A cell left
  // spanning columns that were dropped keeps them in the table, and a table
  // held to its box then shares its width with columns nobody sees.
  const found = [];
  for (const table of document.querySelectorAll('main ' + scroll + ' > table')) {
    if (!table.checkVisibility()) continue;
    const shown = [...table.querySelectorAll(':scope > thead > tr:last-child > th')]
      .filter((heading) => getComputedStyle(heading).display !== 'none');
    if (!shown.length) continue;
    const used = shown.reduce((sum, heading) => sum + box(heading).width, 0);
    const width = box(table).width;
    if (used < width - 2) {
      found.push(`${describe(table)} shows ${shown.length} columns in ${Math.round(used)}px of ${Math.round(width)}px`);
    }
  }
  return found;
}"""
)

SORT_BY = """([scroll, column]) => {
  const table = document.querySelector('main ' + scroll + ' > table');
  const heading = table.querySelectorAll('thead th')[column];
  const control = heading.querySelector('button');
  if (!control) return null;
  control.click();
  return {
    sort: heading.getAttribute('aria-sort'),
    values: [...table.querySelectorAll('tbody')].map((body) =>
      [...body.querySelectorAll(':scope > tr')]
        .filter((row) => row.children.length > column && !row.querySelector('[colspan]'))
        .map((row) => row.children[column].textContent.trim().replace(/\\s+/g, ' '))),
  };
}"""

# Press the first toggle that shows in the page's first table, and say which
# of its records (counted as PHONE_TABLE counts them) it opened.
PRESS_FIRST_ROW_TOGGLE = """(scroll) => {
  const table = document.querySelector('main ' + scroll).querySelector('table');
  const records = [...table.querySelectorAll('tbody > tr:not([data-row-detail]):not(:has(> th[scope="rowgroup"]))')];
  const toggle = [...table.querySelectorAll('tbody button[aria-expanded]')].find((el) => el.checkVisibility());
  toggle.click();
  return records.indexOf(toggle.closest('tr'));
}"""

# Nothing reaches past the content's side gutter: drawn there, it touches the
# edge of the screen. `text` also holds every line of text to the box that
# holds it.
PAST_THE_GUTTER = (
    "([selector, text]) => {"
    + DESCRIBE
    + PAINTED_PART
    + """
  const main = document.querySelector('main');
  const pad = getComputedStyle(main);
  const edge = main.getBoundingClientRect();
  const left = edge.left + parseFloat(pad.paddingLeft);
  const right = edge.right - parseFloat(pad.paddingRight);
  const found = [];
  const inScroller = (el) => {
    for (let up = el.parentElement; up && up !== main; up = up.parentElement) {
      if (scrolls(up)) return true;
    }
    return false;
  };
  for (const el of main.querySelectorAll(selector)) {
    if (!el.checkVisibility() || inScroller(el)) continue;
    // A box's own overflow clips what it holds, never the box.
    const r = painted(el.parentElement, box(el));
    if (!r || r.width < 2 || r.height < 2) continue;
    if (r.right > right + 1 || r.left < left - 1) {
      found.push(`${describe(el)} spans ${Math.round(r.left)}..${Math.round(r.right)}px, gutter ${Math.round(left)}..${Math.round(right)}px`);
    }
  }
  if (!text) return [...new Set(found)];
  const range = document.createRange();
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const el = node.parentElement;
    if (!node.data.trim() || inScroller(el)) continue;
    let block = el;
    while (block !== main && /^(inline|contents)/.test(getComputedStyle(block).display)) block = block.parentElement;
    const style = getComputedStyle(block);
    if (style.overflowX !== 'visible') continue;
    const b = box(block);
    const inner = b.right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    range.selectNodeContents(node);
    const lines = [...range.getClientRects()].map((r) => painted(el, r)).filter(Boolean);
    if (!lines.length) continue;
    const far = Math.max(...lines.map((r) => r.right));
    if (far > inner + 1) found.push(`${describe(block)}: "${node.data.trim().slice(0, 40)}" runs ${Math.round(far - inner)}px past its padding`);
  }
  return [...new Set(found)];
}"""
)


# A table with a column of dashes, a column of marks with no words, and a
# column of short words: the server marks the dashes blank and they are not
# drawn, the marks are, and no word is split however the column's width rounds.
BLANK_TABLE = """{% table_region "Runbooks" %}<table class="data-table">
<thead><tr>{% table_heading "Title" %}{% table_heading "Type" %}{% table_heading "Nothing" %}
{% table_heading "Runs" %}{% table_heading "Note" %}</tr></thead><tbody>
<tr><td>Upgrade a container to a new release</td><td>Runbook</td><td><span class="empty-value">–</span></td>
<td><span class="kpi-dots"><span class="kpi-dot is-filled" title="Succeeded"></span></span></td><td>A sentence long enough to take the room it is given and wrap.</td></tr>
<tr><td>Rotate the signing keys</td><td>Runbook</td><td>–</td>
<td><span class="kpi-dots"><span class="kpi-dot is-filled" title="Succeeded"></span></span></td><td>Another sentence that wraps in its column.</td></tr>
</tbody></table>{% endtable_region %}"""

COLUMNS_SHOWN = """() => [...document.querySelectorAll('thead th')]
  .filter((th) => getComputedStyle(th).display !== 'none').map((th) => th.textContent.replace(/[^A-Za-z ]/g, '').trim())"""

# Two tables whose every row the server made openable. In the first a narrow
# box drops a column: one row has something in it, one has a dash there. One
# row's only clipped text is for screen readers, beside a decoration hidden
# from them, and one row's value really is cut off. In the second a row folds
# part of a list, so the gutter is kept at every width, and a row's value is
# cut off there too.
ROW_TOGGLE_TABLES = """
{% table_region "Dropping" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Value" %}{% table_heading "Note" drop=1 %}</tr></thead><tbody>
<tr data-probe="dropped"><td class="row-gutter">Dropped{% row_toggle %}</td><td>Short</td>
<td><span style="display: inline-block; inline-size: 30rem">Something too wide for a phone to keep</span></td></tr>
<tr data-probe="dropped-blank"><td class="row-gutter">Blank{% row_toggle %}</td><td>Short</td><td>–</td></tr>
<tr data-probe="screen-reader"><td class="row-gutter">Chest{% row_toggle %}</td>
<td><span class="cadence-mark is-hit" aria-hidden="true"></span>
<span class="visually-hidden">One session in the week of Aug 10, said in full</span></td><td>–</td></tr>
<tr data-probe="cut"><td class="row-gutter">Name{% row_toggle %}</td>
<td><div style="max-width: 40px; overflow: hidden; white-space: nowrap">a value far too long to fit</div></td><td>–</td></tr>
</tbody></table>{% endtable_region %}
{% table_region "Folding" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Value" %}</tr></thead><tbody>
<tr data-probe="folded"><td class="row-gutter">Folded{% row_toggle %}</td>
<td><span class="tag">One</span><span class="muted row-less">+1</span><span class="row-more"><span class="tag">Two</span></span></td></tr>
<tr data-probe="cut-in-a-kept-gutter"><td class="row-gutter">Name{% row_toggle %}</td>
<td><div style="max-width: 40px; overflow: hidden; white-space: nowrap">a value far too long to fit</div></td></tr>
</tbody></table>{% endtable_region %}"""

# A table of cut values, each the word its row is probed by then more: one
# with no title, one carrying its whole text as a title, one titled in a row
# that opens, one past the line a clamp ends with an ellipsis, and two cut
# where no ellipsis can land: a box of words around a
# box of its own, and a name's chip in the muted line under a row's name, as
# a unit's page drew one. A note wider than a phone puts the table in a
# narrow box there, which keeps the gutter of a table whose rows open, so the
# third opens; a wide box shows each titled value whole on hover, and a cut
# with no ellipsis tells no one to hover.
_CUT = (
    '<div style="max-width: 40px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap"{}>'
    "{} value far too long to fit</div>"
)
_CHIP = "chip-" + "-".join(["example-unit-name-long-enough-to-be-cut"] * 4) + ".timer"
_NOTE = '<td><span style="display: inline-block; inline-size: 30rem">A note wider than a phone</span></td>'
CUT_TABLE = (
    '{% load value_tags %}{% table_region "Cuts" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Name" gutter=True %}{% table_heading "Value" %}{% table_heading "Note" %}</tr></thead><tbody>'
    + "".join(
        f'<tr><td class="row-gutter">{name.title()}{toggle}</td><td>{_CUT.format(title.format(name), name)}</td>{_NOTE}</tr>'
        for name, title, toggle in (
            ("untitled", "", ""),
            ("titled", ' title="{} value far too long to fit"', ""),
            ("opens", ' title="{} value far too long to fit"', "{% row_toggle %}"),
        )
    )
    + '<tr><td class="row-gutter">Clamped</td><td><div style="display: -webkit-box; -webkit-box-orient: vertical; '
    '-webkit-line-clamp: 1; overflow: hidden; max-width: 80px">clamped value far too long to fit on its one line'
    f"</div></td>{_NOTE}</tr>"
    + '<tr><td class="row-gutter">Unmarked</td><td><div style="max-width: 40px; overflow: hidden; '
    'text-overflow: ellipsis; white-space: nowrap"><span style="display: inline-block">unmarked value far too long '
    f"to fit</span></div></td>{_NOTE}</tr>"
    + f'<tr><td class="row-gutter">Chip<br><span class="muted"><code>{{{{ "{_CHIP}"|breakable }}}}</code></span></td>'
    f"<td>Short</td>{_NOTE}</tr>" + "</tbody></table>{% endtable_region %}"
)

# Rows that open, as an event's log draws them: a host name in a column a
# phone lets go, so an opened row shows it in the line beneath, and a value
# its own cell cuts.
OPEN_ROW_TABLE = """{% load value_tags %}
{% table_region "Opened" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Object" drop=1 %}{% table_heading "Note" %}</tr></thead><tbody>
<tr><td class="row-gutter">Updated{% row_toggle %}</td><td>{{ host|breakable }}</td>
<td><span style="display: inline-block; inline-size: 14rem">A note that keeps its line</span></td></tr>
<tr><td class="row-gutter">Created{% row_toggle %}</td><td>{{ host|breakable }}</td>
<td><div style="max-width: 40px; overflow: hidden; white-space: nowrap">a value far too long to fit</div></td></tr>
</tbody></table>{% endtable_region %}"""

OPEN_ROW_VALUES = {"host": "example-hostname-with-a-long-descriptive-name-01.example.com"}

# A value cut at half the window's width, whole on a wide one, in a row that
# cannot open, and one always cut in a row that can, whose toggle a wide box
# does not show; tables.js titles each while it is cut and its row is closed.
TITLED_CUTS = """{% table_region "Titled" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Value" %}</tr></thead><tbody>
<tr data-probe="narrows"><td class="row-gutter">Narrows</td>
<td><div style="max-inline-size: 50vw; overflow: hidden; white-space: nowrap; text-overflow: ellipsis">
a value that is whole on a wide screen and cut at half of a phone's</div></td></tr>
<tr data-probe="opens"><td class="row-gutter">Opens{% row_toggle %}</td>
<td><div style="max-inline-size: 40px; overflow: hidden; white-space: nowrap">a value far too long to fit</div></td></tr>
</tbody></table>{% endtable_region %}"""

# A panel shown from a cell, as an item's origin is: a key and a source too
# long for the cell, which the panel has the room to show whole.
POPOVER_IN_A_CELL = """{% load value_tags %}
{% table_region "Origins" %}<table class="data-table"><thead><tr>
{% table_heading "Item" gutter=True %}{% table_heading "Urgency" %}</tr></thead><tbody>
<tr><td class="row-gutter">Example item</td><td><span class="pill">Urgent</span>
<div class="modal origin-panel" id="example-origin" popover><div class="modal-panel"><dl class="kv compact">
<dt>From</dt><dd>{{ source|breakable }}</dd><dt>Key</dt><dd><code>{{ key|breakable }}</code></dd></dl></div></div></td></tr>
</tbody></table>{% endtable_region %}"""

POPOVER_VALUES = {
    "key": "example.approvals:approval:d7f2b3ed-186d-402a-9c5e-1f0b2a3c4d5e",
    "source": "an-example-source-with-a-deliberately-long-name",
}

# The rows whose values tables.js titled for being cut, by `data-probe`.
TITLED_ROWS = (
    "() => [...document.querySelectorAll('[data-cut-title]')].map((el) => el.closest('tr').dataset.probe).sort()"
)

ROW_TOGGLES = """() => [...document.querySelectorAll('tbody tr:not([data-row-detail])')].map((row) => ({
  probe: row.dataset.probe,
  toggles: row.querySelectorAll('button[aria-expanded]').length,
  shown: [...row.querySelectorAll('button[aria-expanded]')].some((toggle) => toggle.checkVisibility()),
  beside_name: row.querySelector('button[aria-expanded]')?.parentElement === row.cells[0],
}))"""

# A path's steps, as the service page draws them: an address with a port, a
# host name and a list of networks beside sentences, in tables with ranked
# columns to let go; what each prints is in IDENTIFIER_VALUES.
IDENTIFIER_TABLES = """{% load value_tags %}
{% table_region "Steps" %}<table class="data-table"><thead><tr>
{% table_heading "Step" gutter=True %}{% table_heading "Name" "identifier" %}{% table_heading "State" "status" %}
{% table_heading "Since" drop=3 %}{% table_heading "If it breaks" drop=2 %}{% table_heading "Read from" drop=1 %}</tr></thead><tbody>
<tr><td>Proxy host</td><td><a href="/proxy/" data-entity="Proxy host">{{ address|breakable }}</a></td>
<td><span class="pill">Up</span></td><td>1 day</td><td>If it is removed, the name stops reaching the app it serves.</td>
<td>Read from the proxy's own list of hosts, a minute ago</td></tr>
<tr><td>Forwards to</td><td>{{ address|breakable }}</td><td><span class="pill">Up</span></td><td>1 day</td>
<td>If nothing listens there, a reader gets a gateway error.</td><td>Read from the container's ports</td></tr>
<tr><td>Service</td><td><span data-entity="Service">{{ host|breakable }}</span></td><td></td><td>1 day</td>
<td>The service stops answering at this name.</td><td>Declared in HQ</td></tr>
</tbody></table>{% endtable_region %}
{% table_region "Who is allowed" %}<table class="data-table"><thead><tr>
{% table_heading "Access list" gutter=True %}{% table_heading "Admits by" drop=3 %}
{% table_heading "Addresses" drop=2 %}{% table_heading "Logins" drop=1 %}</tr></thead><tbody>
<tr><td>office only</td><td>Address rules</td><td>{{ networks|breakable }}</td><td>{{ logins|breakable }}</td></tr>
</tbody></table>{% endtable_region %}"""

IDENTIFIER_VALUES = {
    "address": "203.0.113.214:8000",
    "host": "status.example.com",
    "networks": "allow 198.51.100.0/24; allow 2001:db8:a1e0::/48",
    "logins": "none",
}
