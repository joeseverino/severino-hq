"""Probes of tables: their cells, rows, columns and gutter, and the tables drawn to probe them."""

from hq.platform.core.tests.browser.probes.describe import _DESCRIBE, _PAINTED_PART

# How tall a table row may be, in lines of its own body text. The tallest row
# the tables are designed for is a connection's: its name, endpoint, who holds
# its secret, where it reaches from, and a closed "Can see" disclosure, one
# line each, with room for the owner line to wrap once at a tablet: six. A
# name is clamped to two lines (.table-clamp, `[data-entity]` in a cell), so a
# row past six is a list put in a cell (a machine's every hostname, a
# container's every port) that belongs on the record's own page, with a count
# or the first few in the row. Half a line of slack absorbs the smaller type
# the secondary lines are set in. A row's text can be more than six lines of
# its width hold (a name at its field's full length beside a paragraph of
# notes), so a cell is held to the larger of this and the row at its best:
# its wrapping text shared evenly over the room its one-line cells leave, no
# column narrower than its longest word, and a line of slack for each column
# that wraps. A cell past both is a column starved while another has room to
# give.
ROW_LINES = 6

_CELLS = (
    "() => {"
    + _DESCRIBE
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

_TABLES = (
    "() => {"
    + _DESCRIBE
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

_ROW_HEIGHTS = (
    "(lines) => {"
    + _DESCRIBE
    + """
  // A row is as tall as its tallest cell's content. The cell itself is
  // stretched to the row, so what is measured is the visible text in it: not
  // a closed disclosure's body, not the lines a clamp cuts off.
  const shown = (text, cell) => {
    const range = document.createRange();
    range.selectNodeContents(text);
    let bottom = range.getBoundingClientRect().bottom;
    for (let up = text.parentElement; up && up !== cell; up = up.parentElement) {
      if (getComputedStyle(up).overflowY !== 'visible') bottom = Math.min(bottom, box(up).bottom);
    }
    return bottom;
  };
  const height = (cell) => {
    const top = box(cell).top + parseFloat(getComputedStyle(cell).paddingTop);
    let bottom = top;
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode(); text; text = walker.nextNode()) {
      if (!text.textContent.trim() || !text.parentElement.checkVisibility()) continue;
      bottom = Math.max(bottom, shown(text, cell));
    }
    return bottom - top;
  };
  const lineOf = (style) => (style.lineHeight === 'normal' ? parseFloat(style.fontSize) * 1.2 : parseFloat(style.lineHeight));
  const hold = (el, property, value) => {
    const was = [el.style.getPropertyValue(property), el.style.getPropertyPriority(property)];
    el.style.setProperty(property, value, 'important');
    return () => (was[0] ? el.style.setProperty(property, ...was) : el.style.removeProperty(property));
  };
  // Each cell's narrowest: its column's width with the table laid out at
  // min-content, the page left as it was. A table holding its columns while
  // a row is open (tables.js) cannot lay them out again, so each is as
  // narrow as it is held, and the row is judged at those widths.
  const narrowest = (table, cells) => {
    if (table.hasAttribute('data-rows-held')) return cells.map(({cell}) => box(cell).width);
    const released = ['table-layout:auto', 'width:min-content', 'min-width:0', 'max-width:none']
      .map((pair) => hold(table, ...pair.split(':')));
    const widths = cells.map(({cell}) => box(cell).width);
    released.reverse().forEach((release) => release());
    return widths;
  };
  // The row at its best: the height at which the text of its cells that wrap
  // fits the room the cells that keep one line leave, each cell as tall as
  // the next unless its column is already at its narrowest. A cell's text is
  // the area it covers (its content width by its height), which no share of
  // the room makes smaller. The browser shares the room in proportion to
  // each column's longest line, a word at a time, so a column that wraps can
  // stand a line past its exact share: one line each. Past what any layout of
  // the row can reach, it is no measure, and the row is judged by the budget
  // alone.
  const balanced = (row, cells) => {
    const least = narrowest(row.closest('table'), cells);
    let room = box(row).width;
    const wrapping = [];
    cells.forEach(({cell, style, tall}, i) => {
      const width = box(cell).width;
      if (tall <= 1.5 * lineOf(style)) { room -= width; return; }
      const inset = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
      room -= inset;
      wrapping.push({area: (width - inset) * tall, least: least[i] - inset});
    });
    const needs = (height) => wrapping.reduce((sum, c) => sum + Math.max(c.least, c.area / height), 0);
    let low = 0, high = Math.max(...cells.map((c) => c.tall)) * 4;
    if (needs(high) > room) return {height: 0, columns: 0};
    for (let step = 0; step < 30; step++) {
      const mid = (low + high) / 2;
      if (needs(mid) > room) low = mid; else high = mid;
    }
    return {height: high, columns: wrapping.length};
  };
  const found = [];
  for (const row of document.querySelectorAll('main tbody tr')) {
    if (!row.getClientRects().length || row.cells.length === 1) continue;
    const cells = [...row.cells].filter((cell) => cell.getClientRects().length)
      .map((cell) => ({cell, style: getComputedStyle(cell), tall: height(cell)}));
    let best = null;
    for (const {cell, style, tall} of cells) {
      const line = lineOf(style);
      if (tall <= (lines + 0.5) * line) continue;
      best ??= balanced(row, cells);
      const shared = best.height + best.columns * line;
      if (tall > Math.max(lines * line, shared) + 0.5 * line) {
        found.push(`${describe(row.closest('table'))} row ${row.rowIndex}: ${describe(cell)} is ${(tall / line).toFixed(1)} lines, ${(shared / line).toFixed(1)} at its row's best: ${cell.textContent.trim().replace(/\\s+/g, ' ').slice(0, 60)}`);
      }
    }
  }
  return [...new Set(found)];
}"""
)

_UNPADDED_CELLS = (
    "(selector) => {"
    + _DESCRIBE
    + """
  return [...document.querySelectorAll(selector)]
    .filter((el) => el.getBoundingClientRect().width > 0)
    .filter((el) => {
      const style = getComputedStyle(el);
      return parseFloat(style.paddingLeft) < 4 || parseFloat(style.paddingTop) < 4;
    })
    .map(describe);
}"""
)

# The first table on a page as a phone lays it out: whether it runs past its
# box, how wide each showing column is, how tall each record's row is, and how
# many rows show their toggle. The line an open row adds is made by tables.js,
# so no template renders it and it is found by attribute.
_PHONE_TABLE = """(scroll) => {
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
    details: [...table.querySelectorAll('tr[data-row-detail] dt')].map((term) => term.textContent),
    held: table.hasAttribute('data-rows-held'),
  };
}"""

# Every table that is wider than the box it scrolls in, and for each table
# that has row toggles, how many different left edges its rows' names start on.
_TABLES_THAT_DO_NOT_FIT = """(scroll) => [...document.querySelectorAll('main ' + scroll)]
  .filter((box) => box.checkVisibility() && !box.closest('details:not([open])'))
  .map((box) => [box, box.querySelector(':scope > table')])
  .filter(([box, table]) => table && table.querySelector('tbody td')
    && table.getBoundingClientRect().width > box.clientWidth + 1)
  .map(([box, table]) => (box.getAttribute('aria-label') || table.querySelector('th')?.textContent || '').trim().slice(0, 24)
    + ' +' + Math.round(table.getBoundingClientRect().width - box.clientWidth))"""

_NAME_EDGES = """(scroll) => [...document.querySelectorAll('main ' + scroll + ' > table')]
  .map((table) => [table, [...table.querySelectorAll('tbody button[aria-expanded]')].find((el) => el.checkVisibility())])
  .filter(([table, toggle]) => table.checkVisibility() && toggle)
  .map(([table, toggle]) => {
    const column = toggle.closest('td, th').cellIndex;
    const edges = [...table.querySelectorAll('tbody > tr:not([data-row-detail])')]
      .map((row) => row.children[column])
      // A cell that holds only the toggle has no name: the name starts the next cell.
      .map((cell) => (cell && !cell.textContent.trim() ? cell.nextElementSibling : cell))
      .filter((cell) => cell && !cell.hasAttribute('colspan') && cell.textContent.trim())
      .map((cell) => {
        const range = document.createRange();
        range.selectNodeContents(cell);
        const first = [...range.getClientRects()].find((rect) => rect.width > 1);
        return first ? Math.round(first.left) : null;
      })
      .filter((edge) => edge !== null);
    return new Set(edges).size;
  })"""

_CELLS_OFF_THE_FIRST_LINE = (
    "(scroll) => {"
    + _DESCRIBE
    + """
  // A row reads from its first line: its toggle is placed there, so every
  // cell of the row starts there too, whatever page the table is on.
  const found = [];
  for (const table of document.querySelectorAll('main ' + scroll + ' > table')) {
    for (const cell of table.querySelectorAll(':scope > * > tr > *')) {
      const align = getComputedStyle(cell).verticalAlign;
      if (align !== 'top') found.push(`${describe(cell)} in ${describe(table)} is set ${align}`);
    }
  }
  return [...new Set(found)];
}"""
)

_UNSEEN_COLUMNS = (
    "(scroll) => {"
    + _DESCRIBE
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

_SORT_BY = """([scroll, column]) => {
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
# of its records (counted as _PHONE_TABLE counts them) it opened.
_PRESS_FIRST_ROW_TOGGLE = """(scroll) => {
  const table = document.querySelector('main ' + scroll).querySelector('table');
  const records = [...table.querySelectorAll('tbody > tr:not([data-row-detail]):not(:has(> th[scope="rowgroup"]))')];
  const toggle = [...table.querySelectorAll('tbody button[aria-expanded]')].find((el) => el.checkVisibility());
  toggle.click();
  return records.indexOf(toggle.closest('tr'));
}"""

# Nothing reaches past the content's side gutter: drawn there, it touches the
# edge of the screen. `text` also holds every line of text to the box that
# holds it.
_PAST_THE_GUTTER = (
    "([selector, text]) => {"
    + _DESCRIBE
    + _PAINTED_PART
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

# A column no row has anything in.
_EMPTY_COLUMNS = (
    "() => {"
    + _DESCRIBE
    + """
  const found = [];
  const shown = (cell) => cell.textContent.trim()
    || [...cell.querySelectorAll('img, svg, button, input, select, a')].some((el) => el.checkVisibility());
  for (const table of document.querySelectorAll('main table')) {
    if (!table.checkVisibility()) continue;
    const rows = [...table.querySelectorAll(':scope > tbody > tr:not([data-row-detail])')]
      .filter((row) => row.checkVisibility());
    if (!rows.length) continue;
    const width = Math.max(...rows.map((row) => row.cells.length));
    for (let column = 0; column < width; column++) {
      const cells = rows.map((row) => row.cells[column]).filter((cell) => cell && cell.colSpan === 1 && cell.checkVisibility());
      if (cells.length === rows.length && !cells.some(shown)) {
        const head = table.tHead?.rows[0]?.cells[column];
        found.push(`${describe(table)} column ${column + 1} ("${head ? head.textContent.trim() : ''}") is empty in every row`);
      }
    }
  }
  return found;
}"""
)

# A table with a column of dashes, a column of marks with no words, and a
# column of short words: the server marks the dashes blank and they are not
# drawn, the marks are, and no word is split however the column's width rounds.
_BLANK_TABLE = """{% table_region "Runbooks" %}<table class="data-table">
<thead><tr>{% table_heading "Title" %}{% table_heading "Type" %}{% table_heading "Nothing" %}
{% table_heading "Runs" %}{% table_heading "Note" %}</tr></thead><tbody>
<tr><td>Upgrade a container to a new release</td><td>Runbook</td><td><span class="empty-value">–</span></td>
<td><span class="kpi-dots"><span class="kpi-dot is-filled" title="Succeeded"></span></span></td><td>A sentence long enough to take the room it is given and wrap.</td></tr>
<tr><td>Rotate the signing keys</td><td>Runbook</td><td>–</td>
<td><span class="kpi-dots"><span class="kpi-dot is-filled" title="Succeeded"></span></span></td><td>Another sentence that wraps in its column.</td></tr>
</tbody></table>{% endtable_region %}"""

_COLUMNS_SHOWN = """() => [...document.querySelectorAll('thead th')]
  .filter((th) => getComputedStyle(th).display !== 'none').map((th) => th.textContent.replace(/[^A-Za-z ]/g, '').trim())"""

# Two tables whose every row the server made openable. In the first a narrow
# box drops a column: one row has something in it, one has a dash there. One
# row's only clipped text is for screen readers, beside a decoration hidden
# from them, and one row's value really is cut off. In the second a row folds
# part of a list, so the gutter is kept at every width, and a row's value is
# cut off there too.
_ROW_TOGGLE_TABLES = """
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

_ROW_TOGGLES = """() => [...document.querySelectorAll('tbody tr')].map((row) => ({
  probe: row.dataset.probe,
  toggles: row.querySelectorAll('button[aria-expanded]').length,
  shown: [...row.querySelectorAll('button[aria-expanded]')].some((toggle) => toggle.checkVisibility()),
  beside_name: row.querySelector('button[aria-expanded]')?.parentElement === row.cells[0],
}))"""
