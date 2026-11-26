"""Probes of tables: their cells, rows, columns and gutter, and the tables drawn to probe them."""

import json

from hq.platform.application.pages import PageAction
from hq.platform.application.tables import Below, Cell, DataTable, Piece, Row, TableColumn
from hq.platform.core import included_parts
from hq.platform.core.templatetags.table_tags import DATA_TABLE
from hq.platform.core.tests.browser.probes import Check, Example, Plant
from hq.platform.core.tests.browser.probes.describe import CUTS, DESCRIBE, PAINTED_PART
from hq.platform.core.tests.browser.probes.shifts import PROBE_MOVES
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.widths import PHONE_SCREEN, WIDE

CELLS = (
    "() => {"
    + DESCRIBE
    + """
  // A cell's content stays in the cell. Inside a table that scrolls, running
  // past the edge is not caught by anything else: it lands under the next
  // column, readable in neither. What a box cuts (a line cut with an
  // ellipsis, the cell itself cut by its kind) shows only as far as that box,
  // which is held on its own. A popover or a modal dialog shown is drawn in
  // the top layer, over the page, so the cell it sits in does not hold it.
  // What reads across the cells after its own (a month's bar, `data-reach`
  // of them) stays in those.
  const found = [];
  const cut = (el, cell) => {
    for (let b = el.parentElement; b && b !== cell.parentElement; b = b.parentElement) {
      if (getComputedStyle(b).overflowX !== 'visible') return true;
    }
    return false;
  };
  for (const cell of document.querySelectorAll('main td, main th')) {
    const row = [...cell.parentElement.children];
    for (const el of cell.querySelectorAll('*')) {
      const r = box(el);
      if (r.width < 2 || r.height < 2 || !el.checkVisibility() || el.closest(':popover-open, :modal')) continue;
      const across = el.closest('[data-reach]');
      const reach = across && cell.contains(across) ? Number(across.dataset.reach) : 1;
      const edge = box(row[Math.min(row.indexOf(cell) + reach, row.length) - 1]).right;
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
  // Every cell a row shows stands under its column's heading (its span
  // covers the heading's), no row draws a column's label in itself, and the
  // headings are drawn.
  const width = document.documentElement.clientWidth;
  const said = (el) => el.textContent.replace(/\\s+/g, ' ').trim();
  const columns = (row) => {
    const placed = [];
    let at = 0;
    for (const cell of row.cells) {
      placed.push([at, cell]);
      at += cell.colSpan;
    }
    return placed;
  };
  const unplaced = (table, headings) => {
    const over = new Map(columns(headings).filter(([, th]) => th.checkVisibility()).map(([at, th]) => [at, th]));
    const labels = new Set([...over.values()].map(said).filter(Boolean));
    const wrong = [];
    for (const row of table.querySelectorAll(':scope > tbody > tr:not(.row-detail, .row-group)')) {
      if (!row.checkVisibility()) continue;
      for (const [at, cell] of columns(row)) {
        if (!cell.checkVisibility()) continue;
        const drawn = getComputedStyle(cell, '::before').content.replace(/^"|"$/g, '');
        if (labels.has(drawn)) wrong.push(`a row draws the label ${drawn}`);
        const heading = cell.colSpan === 1 && over.get(at);
        if (!heading) continue;
        const c = cell.getBoundingClientRect();
        const h = heading.getBoundingClientRect();
        if (c.left > h.left + 1 || c.right < h.right - 1) wrong.push(`${said(cell).slice(0, 16) || 'a cell'} stands outside its heading ${said(heading).slice(0, 16)}`);
      }
    }
    return wrong;
  };
  const found = [];
  let seen = 0;
  let undrawn = 0;
  for (const table of document.querySelectorAll('main table.data-table')) {
    const r = box(table);
    // A table in a closed disclosure is not drawn, so it has no box to fit
    // until it opens; the rules read with every disclosure open read it then.
    if (!r.width || !table.checkVisibility()) {
      undrawn += 1;
      continue;
    }
    seen += 1;
    const name = describe(table);
    const head = table.querySelector(':scope > thead > tr:last-child');
    const headings = head ? [...head.cells] : [];
    if (headings.length && !headings.some((heading) => heading.checkVisibility())) {
      found.push(`${name} hides its column headings`);
    }
    if (head) for (const wrong of new Set(unplaced(table, head))) found.push(`${name}: ${wrong}`);
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
  return {seen, undrawn, found};
}"""
)

# A table as a finding names it: its region's name, which only a region that
# scrolls carries, else its first heading with words (a tick-box's says none).
TABLE_NAME = """
const tableName = (region, table) => (region.getAttribute('aria-label')
  || [...table.querySelectorAll('th')].map((th) => th.textContent.replace(/\\s+/g, ' ').trim()).find(Boolean)
  || 'a table with no headings').slice(0, 24);
"""


# What a table promises past what `TABLES` reads: a group's heading spans its
# table and the heading row sticks. What breaks one, each named once. A value
# cut in a closed row is `CUT_AND_CLOSED`'s.
TABLE_PROMISES = (
    "(scroll) => {"
    + TABLE_NAME
    + """
  const found = new Set();
  let read = 0;
  for (const region of document.querySelectorAll('main ' + scroll)) {
    const table = region.querySelector(':scope > table');
    if (!table || !table.checkVisibility()) continue;
    read += 1;
    const name = tableName(region, table);
    const width = table.getBoundingClientRect().width;
    for (const cell of table.querySelectorAll(':scope > tbody > tr > [colspan]')) {
      if (cell.checkVisibility() && cell.getBoundingClientRect().width < width - 2) found.add(`${name}: a cell across its row spans less`);
    }
    // The heading row sticks, a box of the table's grid.
    const head = table.querySelector(':scope > thead > tr');
    if (head && getComputedStyle(head).position !== 'sticky') found.add(`${name}: its heading row does not stick`);
  }
  return {read, found: [...found]};
}"""
)

# Every table that is wider than the box it scrolls in, and for each table
# that has row toggles, how many different left edges its rows' names start on.
TABLES_THAT_DO_NOT_FIT = (
    "(scroll) => {"
    + TABLE_NAME
    + """
  const read = [...document.querySelectorAll('main ' + scroll)]
    .filter((box) => box.checkVisibility() && !box.closest('details:not([open])'))
    .map((box) => [box, box.querySelector(':scope > table')])
    .filter(([, table]) => table && table.querySelector('tbody td'));
  const found = read
    .filter(([box, table]) => table.getBoundingClientRect().width > box.clientWidth + 1)
    .map(([box, table]) => `${tableName(box, table)} +${Math.round(table.getBoundingClientRect().width - box.clientWidth)}`);
  return {read: read.length, found};
}"""
)


# A table region's layout at this width, as a value that changes only where the
# layout around it changes shape as the window grows: which viewport queries
# hold, whether the page scrolls, around each region how many tracks each grid
# has and how many lines each wrapping flex box makes, and how far each
# region's table has given way (tables.css: which of its columns show, and
# whether it stands in its narrow box). Each of those moves one way as the
# window grows, so a sweep that finds it changed between two widths knows a
# change of shape lies between them. It is read, with each region's width, a
# frame after the resize (two, so WebKit has laid the page out again). Opening
# a row changes no table's shape, since a region is read by its width alone.
TABLE_LAYOUT = """async ([scroll, queries]) => {
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const lines = (el) => {
    const shown = [...el.children].filter((child) => child.getClientRects().length);
    return 1 + shown.filter((child, at) => at && child.offsetLeft <= shown[at - 1].offsetLeft).length;
  };
  const shape = (el) => {
    const style = getComputedStyle(el);
    if (style.display.endsWith('grid')) return `g${style.gridTemplateColumns.split(' ').length}`;
    if (style.display.endsWith('flex') && style.flexWrap !== 'nowrap') return `f${lines(el)}`;
    return '';
  };
  // Which of its headings show, and whether its narrow-box marks have left.
  const gave = (table) => {
    if (!table) return '';
    const showing = [...table.querySelectorAll(':scope > thead > tr:last-child > th')]
      .map((heading) => getComputedStyle(heading).display === 'none' ? 0 : 1).join('');
    return showing + (table.querySelector('[data-narrow]:not(.row-detail *)')?.checkVisibility() === false ? 'n' : '');
  };
  const regions = [...document.querySelectorAll('main ' + scroll)];
  const around = regions.map((region) => {
    let said = gave(region.querySelector(':scope > table')) + ':';
    for (let el = region.parentElement; el && el !== document.body; el = el.parentElement) said += shape(el);
    return said;
  });
  const layout = [
    queries.map((query) => matchMedia(query).matches ? 1 : 0).join(''),
    window.innerWidth - document.documentElement.clientWidth,
    ...around,
  ].join(' ');
  return [layout, regions.map((region) => region.clientWidth).join(' ')];
}"""

# Each table region's showing columns' widths and its rows' heights, with
# the rows themselves, for `MOVED` to compare within one trip.
_PLACES = """([scroll, records]) => [...document.querySelectorAll('main ' + scroll + ' > table')]
  .filter((table) => table.checkVisibility())
  .map((table) => ({
    table,
    columns: [...table.querySelectorAll(':scope > thead > tr:last-child > th')]
      .filter((heading) => heading.getClientRects().length)
      .map((heading) => Math.round(heading.getBoundingClientRect().width)),
    rows: [...table.querySelectorAll(records)]
      .filter((row) => row.getClientRects().length)
      .map((row) => [row, row.getBoundingClientRect().height]),
  }))"""

# What opening rows moved that the reader did not open: a column's width, or
# the height of a row that stayed closed (the component tests' "moves
# nothing", over every table).
_MOVED = (
    "([before, after, opened]) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const {table, columns, rows} of before) {
    const now = after.find((places) => places.table === table);
    if (!now) continue;
    if (now.columns.join() !== columns.join()) {
      found.push(`${describe(table)}: opening its rows moved its columns from ${columns.join(' ')} to ${now.columns.join(' ')}px`);
    }
    const heights = new Map(now.rows);
    const moved = rows.filter(([row, height]) => !opened.includes(row) && heights.has(row)
      && Math.abs(heights.get(row) - height) > 1).length;
    if (moved) found.push(`${describe(table)}: opening its rows moved ${moved} rows it did not open`);
  }
  return found;
}"""
)


# Every toggle ``selector`` finds showing and closed, pressed, as a list of
# those it pressed, so `UNPRESS` can close them again in reverse.
PRESS = """(selector) => [...document.querySelectorAll(selector)]
  .filter((toggle) => toggle.checkVisibility() && toggle.getAttribute('aria-expanded') !== 'true')
  .map((toggle) => { toggle.click(); return toggle; })"""

UNPRESS = "(pressed) => [...pressed].reverse().forEach((toggle) => toggle.click())"


def tables_read(trip):
    """One width's read of the tables in one trip: ``trip`` run on the page
    as it stands (said to be in the state ``state`` names) and, if ``rows``,
    again with every row a reader can open opened, and what opening them
    moved, the rows closed again last, so the next width is the page as a
    reader resized it. A page with no row to open has no second state to
    read."""

    return f"""async ([scroll, state, rows, records, args]) => {{
  const frame = () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const places = {_PLACES};
  await frame();
  const states = [[state, await ({trip})(args)]];
  if (!rows) return [states, []];
  const before = places([scroll, records]);
  const opened = ({PRESS})(rows);
  if (!opened.length) return [states, []];
  await frame();
  states.push(['its rows open', await ({trip})(args)]);
  const moved = ({_MOVED})([before, places([scroll, records]), opened.map((toggle) => toggle.closest('tr'))]);
  ({UNPRESS})(opened);
  await frame();
  return [states, moved];
}}"""


# A value cut in a closed row while the row leaves room that would show it
# whole: room in its own cell past what shares its line (a box the value
# fills keeps its padding, and a box beside it, a button or a pill, holds all
# of its own), and the room each other column showing holds
# past what its widest cell asks (each cell's table laid out again at its
# max-content, unseen, beside it so its rules still reach it). A column
# holding a cut gives none, and a fixed layout's columns none at all. A value
# longer than its row's room is cut wherever it goes, so that room is none.
# A box that asks its column for nothing (a flex item grown from no basis, as
# the rest of a `line_list` is) never widens a column, so only its own cell's
# room is its, and it is given that room as a box of its own size.
CUT_BESIDE_ROOM = (
    "(unseen) => {"
    + DESCRIBE
    + CUTS
    + """
  const found = [];
  const inner = (cell) => {
    const s = getComputedStyle(cell);
    const left = cell.getBoundingClientRect().left + cell.clientLeft + parseFloat(s.paddingLeft);
    return {left, right: left + cell.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight)};
  };
  const records = ':scope > tbody > tr:not(.row-detail, [data-row-detail], .is-expanded)';
  for (const table of document.querySelectorAll('main table')) {
    if (!table.checkVisibility() || table.closest('details:not([open])')) continue;
    const rows = [...table.querySelectorAll(records)].filter((row) => row.checkVisibility());
    const cells = rows.flatMap((row) => [...row.querySelectorAll(':scope > :is(td, th)')])
      .filter((cell) => cell.colSpan === 1 && cell.checkVisibility());
    const cuts = cells.flatMap((cell) => cutsIn(cell, unseen).map((cut) => ({cell, ...cut})));
    if (!cuts.length) continue;
    // The same table at its max-content width, where each column is as wide
    // as its widest cell asks.
    const ruler = table.cloneNode(true);
    ruler.setAttribute('aria-hidden', 'true');
    ruler.querySelectorAll('colgroup[data-held]').forEach((group) => group.remove());
    ruler.style.cssText += '; position: absolute; visibility: hidden; inset: 0 auto auto 0; inline-size: max-content; max-inline-size: none';
    table.after(ruler);
    const asks = new Map();
    for (const cell of [...ruler.querySelectorAll(records)].flatMap((row) => [...row.querySelectorAll(':scope > :is(td, th)')])) {
      if (cell.colSpan === 1 && cell.checkVisibility()) asks.set(cell.cellIndex, cell.getBoundingClientRect().width);
    }
    ruler.remove();
    // A cell that stands beneath its row (its controls on a line of their own) is beside no other column, so its room is none of theirs.
    const beneath = (cell) => getComputedStyle(cell).gridColumnEnd === '-1';
    const slack = new Map();
    for (const cell of cells) {
      const place = cell.cellIndex;
      const held = cuts.some((cut) => cut.cell.cellIndex === place) || beneath(cell) ? 0 : cell.getBoundingClientRect().width - (asks.get(place) ?? Infinity);
      slack.set(place, Math.max(0, Math.min(slack.get(place) ?? Infinity, held)));
    }
    // A fixed layout's columns are set by its first row and never share.
    if (getComputedStyle(table).tableLayout === 'fixed') slack.clear();
    const range = document.createRange();
    for (const {cell, node, boxes} of cuts) {
      const by = boxes.at(-1);
      const r = by.getBoundingClientRect();
      // What shares the cut box's lines in its cell, which the box's growing would push.
      let end = r.right;
      // A box around it on the line (a link with its arrow, a tag's padding)
      // grows with it; a box it fills gives it none of its own padding.
      for (let up = by.parentElement; up && up !== cell; up = up.parentElement) {
        const around = getComputedStyle(up);
        if (/inline-/.test(around.display)) end = Math.max(end, up.getBoundingClientRect().right);
        else if (!['inline', 'contents'].includes(around.display)) end += parseFloat(around.paddingRight) + parseFloat(around.borderRightWidth);
      }
      // A box of its own beside the cut (a button, a pill, an icon) holds its
      // whole border box, so the padding past its words is never room; words
      // set in a box that also holds the cut hold only their lines.
      const beside = (line) => line.width && Math.min(line.bottom, r.bottom) - Math.max(line.top, r.top) > line.height / 2;
      const ownBox = (node) => {
        for (let up = node.parentElement; up && up !== cell; up = up.parentElement) {
          if (up.contains(by)) return null;
          if (!['inline', 'contents'].includes(getComputedStyle(up).display)) return up;
        }
        return null;
      };
      const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
      for (let text = walker.nextNode(); text; text = walker.nextNode()) {
        if (!text.data.trim() || by.contains(text) || !textShown(text)) continue;
        const box = ownBox(text);
        range.selectNodeContents(text);
        for (const line of box ? [box.getBoundingClientRect()] : range.getClientRects()) {
          if (beside(line)) end = Math.max(end, line.right);
        }
      }
      for (const control of cell.querySelectorAll('img, svg, input, select, textarea, button, canvas, video')) {
        if (control.contains(by) || by.contains(control) || !control.checkVisibility()) continue;
        const line = control.getBoundingClientRect();
        if (beside(line)) end = Math.max(end, line.right);
      }
      const own = Math.max(0, inner(cell).right - end);
      const asksNothing = boxes.some((box) => /flex/.test(getComputedStyle(box.parentElement).display)
        && getComputedStyle(box).flexGrow !== '0' && parseFloat(getComputedStyle(box).flexBasis) === 0);
      const others = asksNothing || beneath(cell) ? 0 : [...slack].reduce((sum, [place, room]) => sum + (place === cell.cellIndex ? 0 : room), 0);
      const room = own + others;
      if (room < 1) continue;
      // Each box that cuts it given the room, to see whether it then shows
      // the value whole; room it would still cut the value in cures nothing.
      // A pixel less than the room, since a cut is read past a pixel's slack.
      const kept = boxes.map((box) => [box, box.style.cssText, box.getBoundingClientRect().width]);
      for (const [box, was, wide] of kept) {
        box.style.cssText = `${was}; box-sizing: border-box !important; inline-size: ${wide + room - 1}px !important; `
          + `min-inline-size: 0 !important; max-inline-size: none !important${asksNothing ? '; flex: none !important' : ''}`;
      }
      const cured = !cutsIn(cell, unseen).some((cut) => cut.node === node);
      for (const [box, was] of kept) box.style.cssText = was;
      if (cured) {
        found.push(`${describe(by)} "${node.data.trim().slice(0, 40)}" is cut beside ${Math.round(room)}px of room `
          + `(${Math.round(own)}px in its cell, ${Math.round(others)}px in the other columns)`);
      }
    }
  }
  return [...new Set(found)];
}"""
)


# A cell whose words wrap in less than their line while another column of
# its table holds room past what its own cells need: words a few to a line
# beside an empty column. A heading is one of its column's cells. A value's line is as much of it as fits a line of
# `--row-words` in its own font, and no more than its column's share of the
# table's region (`--share`, tables.css), so a table too narrow for every
# column's line is no finding. What a cell needs and how many lines it takes on its
# line are read from the table laid out again at its max-content, unseen,
# beside it, and a column needs the most its cells do; a cell drawn across
# the row or stacked beneath it asks no column for anything. A short value
# (`cell-short`) and a heading need their line whole, past any share: a
# column of longer values or words gives way first.
# A band (`data-band`) is held to the same: its cells stacked one to a row
# while as many of them as its count puts on a row, each as wide as its
# words, fit side by side, a cell drawn across the band (a band read as a
# list) asking for no column.
ROOM_UNSHARED = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  const records = ':scope > tbody > tr:not(.row-detail, [data-row-detail], .is-expanded)';
  const placed = (table) => [...table.querySelectorAll(`:scope > thead > tr:last-child, ${records}`)]
    .filter((row) => row.checkVisibility())
    .flatMap((row) => [...row.querySelectorAll(':scope > :is(td, th)')])
    .filter((cell) => cell.colSpan === 1 && cell.checkVisibility());
  const padded = (cell) => {
    const style = getComputedStyle(cell);
    return parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  };
  // A column's share is counted here, never read from the sheet it judges:
  // the region over one more than the columns it shows, less a cell's padding.
  const lineOf = (cell, share) => {
    const ruler = document.createElement('span');
    ruler.style.cssText = `position: absolute; visibility: hidden; inline-size: min(var(--row-words), ${share - padded(cell)}px)`;
    cell.append(ruler);
    const width = ruler.getBoundingClientRect().width;
    ruler.remove();
    return width;
  };
  for (const table of document.querySelectorAll('main table.data-table')) {
    if (!table.checkVisibility()) continue;
    const cells = placed(table);
    const region = table.closest('.table-scroll');
    const heads = [...table.querySelectorAll(':scope > thead > tr:last-child > th')];
    if (!cells.length || !region || !heads.length) continue;
    const share = region.clientWidth / (heads.filter((head) => head.checkVisibility()).length + 1);
    const ruler = table.cloneNode(true);
    ruler.setAttribute('aria-hidden', 'true');
    ruler.style.cssText += '; position: absolute; visibility: hidden; inset: 0 auto auto 0; inline-size: max-content; max-inline-size: none';
    // A short value whole is the value past any cap on its box.
    ruler.querySelectorAll('.cell-short, :scope > thead th > *').forEach((box) => { box.style.maxInlineSize = 'none'; });
    table.after(ruler);
    // A cell is as tall as its row, so its words' own box says how many lines they take.
    const range = document.createRange();
    const said = (cell) => {
      range.selectNodeContents(cell);
      const box = range.getBoundingClientRect();
      return {width: cell.getBoundingClientRect().width, height: box.height};
    };
    const whole = new Map(placed(ruler).map((cell, at) => [cells[at], said(cell)]));
    ruler.remove();
    const columns = new Map();
    const squeezed = [];
    for (const cell of cells) {
      const at = whole.get(cell);
      if (!at) continue;
      const r = said(cell);
      const held = cell.querySelector(':scope > .cell-short') || cell.closest('thead');
      const asks = held ? at.width - padded(cell) : Math.min(at.width - padded(cell), lineOf(cell, share));
      // A value that keeps its line (a state, a row's controls) needs it whole.
      const keeps = getComputedStyle(cell).whiteSpace === 'nowrap';
      const column = columns.get(cell.cellIndex) ?? {width: r.width, needs: 0, name: heads[cell.cellIndex]?.textContent.trim()};
      column.needs = Math.max(column.needs, (keeps ? at.width - padded(cell) : asks) + padded(cell));
      columns.set(cell.cellIndex, column);
      if (r.height > at.height + 2 && r.width - padded(cell) + 8 < asks && /\\S/.test(cell.innerText)) {
        squeezed.push({cell, short: asks - (r.width - padded(cell)), asks});
      }
    }
    for (const {cell, short, asks} of squeezed) {
      const room = [...columns].filter(([place]) => place !== cell.cellIndex)
        .map(([, column]) => [column.name, column.width - column.needs]).sort((a, b) => b[1] - a[1])[0];
      if (!room || room[1] < short) continue;
      const words = cell.parentElement.parentElement === table.tHead ? 'its heading' : `"${cell.innerText.trim().replace(/\\s+/g, ' ').slice(0, 24)}"`;
      found.push(`${describe(table)}: ${columns.get(cell.cellIndex).name || 'a column'} wraps ${words} in ${Math.round(asks - short)}px of its ${Math.round(asks)}px line while ${room[0] || 'a column'} holds ${Math.round(room[1])}px past its needs`);
    }
  }
  for (const band of document.querySelectorAll('main [data-band]')) {
    if (!band.checkVisibility() || getComputedStyle(band).display !== 'grid') continue;
    const cells = [...band.children].filter((cell) => cell.checkVisibility() && getComputedStyle(cell).gridColumnEnd !== '-1');
    if (cells.length < 2 || new Set(cells.map((cell) => Math.round(cell.getBoundingClientRect().left))).size > 1) continue;
    // Each cell as wide as its words, laid out again unseen in the band: a
    // band and a headline cell contain their width (`container`), which a
    // copy of the whole would measure as nothing.
    const widths = cells.map((cell) => {
      const ruler = cell.cloneNode(true);
      ruler.setAttribute('aria-hidden', 'true');
      ruler.style.cssText += '; position: absolute; visibility: hidden; inset: 0 auto auto 0; inline-size: max-content; container-type: normal';
      band.append(ruler);
      const width = ruler.getBoundingClientRect().width;
      ruler.remove();
      return width;
    }).sort((a, b) => b - a);
    // The fewest a row holds past one, as the counted rows give them
    // (figures.css): two of an even count or of seven and more, else three,
    // since three and five never leave one alone on a row.
    const fewest = cells.length % 2 === 0 || cells.length >= 7 ? 2 : 3;
    const gap = parseFloat(getComputedStyle(band).columnGap) || 0;
    const room = band.getBoundingClientRect().width;
    const takes = widths.slice(0, fewest).reduce((sum, width) => sum + width, 0) + (fewest - 1) * gap;
    if (widths.length >= fewest && takes < room) {
      found.push(`${describe(band)} stacks its ${cells.length} cells one to a row in ${Math.round(room)}px, where ${fewest} take ${Math.round(takes)}px`);
    }
  }
  return [...new Set(found)];
}"""
)


# A box in a table that paints a panel (a fill of its own) with nothing in
# it: no words, no mark, no control. A reader takes it for a missing control
# or something still loading, as an open line's panel of entries that each
# wait for their column to leave would read.
EMPTY_PANELS = (
    "() => {"
    + DESCRIBE
    + """
  // A cell's fill is its row's, which a blank cell shares.
  const held = 'img, svg, canvas, video, iframe, input, select, textarea, button, progress, meter';
  const table = 'tr, td, th, thead, tbody, tfoot';
  const filled = (style) => {
    const fill = style.backgroundColor.match(/[\\d.]+/g);
    return style.backgroundImage !== 'none' || (fill && (fill.length < 4 || parseFloat(fill[3]) > 0));
  };
  return [...document.querySelectorAll('main table.data-table *')]
    .filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width >= 40 && r.height >= 16 && el.checkVisibility({visibilityProperty: true}) && filled(getComputedStyle(el))
        && !el.matches(held) && !el.matches(table) && !el.querySelector(held) && !el.innerText.trim();
    })
    .map((el) => `${describe(el)} draws a ${Math.round(el.getBoundingClientRect().width)}px panel with nothing in it`);
}"""
)


# Every row of a table is ruled off from the row after it across the whole
# table, as one reads where a row ends: a rule along the row's bottom edge,
# drawn by the row or by whichever of its cells stand on that edge. A row's
# own open line joins it with no rule, and a group's heading row is set apart
# by its fill. A closed fold's rows are hidden, so no reader reads them.
ROWS_UNRULED = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  const ruled = (el) => parseFloat(getComputedStyle(el).borderBottomWidth) >= 1;
  for (const table of document.querySelectorAll('main table.data-table')) {
    if (!table.checkVisibility()) continue;
    const rows = [...table.querySelectorAll(':scope > tbody > tr')].filter((row) => row.checkVisibility({visibilityProperty: true}));
    rows.forEach((row, at) => {
      const next = rows[at + 1];
      if (!next || next.matches('.row-group') || next.matches('[data-row-detail]') && next.previousElementSibling === row) return;
      if (ruled(row)) return;
      const edge = row.getBoundingClientRect();
      // The stretches of the row's bottom edge its ruled cells draw, joined.
      const drawn = [...row.children]
        .filter((cell) => cell.checkVisibility() && ruled(cell))
        .map((cell) => cell.getBoundingClientRect())
        .filter((r) => r.width >= 1 && Math.abs(r.bottom - edge.bottom) <= 1)
        .map((r) => [r.left, r.right])
        .sort((a, b) => a[0] - b[0]);
      let reach = edge.left;
      for (const [left, right] of drawn) if (left <= reach + 1) reach = Math.max(reach, right);
      if (reach < edge.right - 1) {
        const said = (row.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 24);
        found.push(`${describe(table)}: "${said}" has no rule along ${Math.round(edge.right - reach)}px of its bottom edge`);
      }
    });
  }
  return [...new Set(found)];
}"""
)


# What a reader changes in a row (a field) and what a row is read by (a
# `key-col`, or the closing figure of a table that marks none) never leave
# with a column a narrow box lets go, and what a row is (its state, a cell
# its pills say) leaves only once no other column that can leave still
# stands: a row without them no longer says what it is or does what it is
# for while a column it could spare stands in their place. A column whose
# heading declares a status with a rank leaves by its rank.
STATE_LEFT = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  for (const cell of document.querySelectorAll('main table.data-table > tbody > tr:not(.is-expanded) > [data-drop]:not([data-blank])')) {
    if (!cell.closest('table').checkVisibility() || cell.checkVisibility()) continue;
    // A pill beside a value is a note on it; a cell its pills alone say is the row's state, unless its heading declares a status with a rank, which it leaves by.
    const pills = [...cell.querySelectorAll('.pill')];
    const stated = pills.length && cell.dataset.kind !== 'status' && pills.map((pill) => pill.textContent).join('').replace(/\\s+/g, '') === cell.textContent.replace(/\\s+/g, '');
    const spared = [...cell.parentElement.children].some((other) => other.matches('[data-drop]') && other.checkVisibility());
    if (stated && spared) found.push(`${describe(cell)} leaves with its column, holding ${describe(pills[0])}`);
  }
  // A field a row fills in stands wherever its row does, at every width.
  for (const field of document.querySelectorAll('main table.data-table > tbody > tr:not(.row-detail) > td :is(select, textarea, input:not([type="hidden"]))')) {
    if (field.closest('tr').checkVisibility() && !field.checkVisibility()) found.push(`${describe(field.closest('td'))} "${(field.closest('td').dataset.label || '').slice(0, 30)}" hides ${describe(field)}, which its row fills in`);
  }
  for (const head of document.querySelectorAll('main table.data-table > thead > tr:last-child')) {
    if (!head.closest('table').checkVisibility()) continue;
    const cells = [...head.cells].filter((th) => !th.matches('[data-blank-column], .wide-only'));
    const keys = cells.filter((th) => th.matches('.key-col'));
    const closing = cells.findLast((th) => !th.matches('[data-kind="control"]'));
    const read = keys.length ? keys : closing && closing.matches('[data-kind="numeric"]') ? [closing] : [];
    for (const key of read) {
      if (!key.checkVisibility()) found.push(`${describe(key)} "${key.textContent.trim().slice(0, 30)}" leaves, though its rows are read by it`);
    }
  }
  return found;
}"""
)


# A value cut short that nothing shows whole: its row does not open at this
# width, and either the cut draws no ellipsis, so nothing tells a reader there
# is more to see, or nothing around it carries its whole text as a title or a
# tip (`data-tip`, app.js), or its box is narrow (`--whole`: 0, tables.css),
# where no pointer hovers. A chip (`data-chip`, table_layout) is read whole
# on the record's page its row links. Cut is read as `CUTS` reads it, up to
# the cell.
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
    const narrow = narrowBox(row.closest('table'));
    for (const cell of row.querySelectorAll(':scope > :is(td, th)')) {
      for (const {node, boxes, marked} of cutsIn(cell, unseen)) {
        const by = boxes.at(-1);
        if (marked && cell.matches('[data-chip]') && node.parentElement.closest('code')) continue;
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
  // An open row keeps its cells as they were, a clamp included; the line
  // beneath it is where its values stand whole.
  const rows = 'main table > tbody > tr.is-expanded + tr.row-detail';
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


# A row offers its toggle only where opening it shows something: an open row
# whose toggle stands over a line with nothing drawn is a chevron that does
# nothing, unless in a narrow box it holds a clamp, which opening releases in
# place (cuts.css), and whose toggle tables.css shows whether or not the
# value overran. A group's toggle opens
# the rows folded under it, so its line may hold nothing.
OPENS_NOTHING = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  for (const row of document.querySelectorAll('main table > tbody > tr.is-expanded:not(:has([data-fold-toggle]))')) {
    const line = row.nextElementSibling;
    if (!row.querySelector('[data-row-toggle]')?.checkVisibility() || row.closest('details:not([open])') || !line?.matches('.row-detail')) continue;
    if (row.querySelector('.cell-clamp') && narrowBox(row.closest('table'))) continue;
    const drawn = [...line.querySelectorAll(':scope > td *')]
      .some((part) => part.checkVisibility() && part.getClientRects().length && box(part).height > 0);
    if (!drawn) found.push(`${describe(row)} opens a line with nothing drawn`);
  }
  return found;
}"""
)


UNSEEN_COLUMNS = (
    "(scroll) => {"
    + DESCRIBE
    + """
  // A table's width belongs to the columns that are showing. A cell left
  // spanning columns that were dropped keeps them in the table, and a table
  // held to its box then shares its width with columns nobody sees. A
  // stacked table has no columns to share it.
  const found = [];
  let read = 0;
  for (const table of document.querySelectorAll('main ' + scroll + ' > table')) {
    if (!table.checkVisibility()) continue;
    read += 1;
    if (getComputedStyle(table).display !== 'table') continue;
    const shown = [...table.querySelectorAll(':scope > thead > tr:last-child > th')]
      .filter((heading) => getComputedStyle(heading).display !== 'none');
    if (!shown.length) continue;
    const used = shown.reduce((sum, heading) => sum + box(heading).width, 0);
    const width = box(table).width;
    if (used < width - 2) {
      found.push(`${describe(table)} shows ${shown.length} columns in ${Math.round(used)}px of ${Math.round(width)}px`);
    }
  }
  return {read, found};
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

# In every table on the page with a row toggle, opens its first row and reads
# each part of that row's cells that showed whole before it opened, a box
# holding text that neither it nor anything up to its cell cuts: each that
# moved or changed width once the row opened, as text. Opening a row only
# lifts cuts, so a part that was whole keeps its box. The row is closed again,
# and the time it stood open kept as the check's own move (`PROBE_MOVES`).
WHOLE_PARTS_MOVED_BY_OPENING = (
    "async (scroll) => {"
    + f"const moves = {json.dumps(PROBE_MOVES)};"
    + """
  const frame = () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const cuts = (el) => el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
  const whole = (part, cell) => {
    for (let el = part; el; el = el.parentElement) {
      if (getComputedStyle(el).overflow !== 'visible' && cuts(el)) return false;
      if (el === cell) return true;
    }
    return true;
  };
  // Where its words are drawn: a part with no box of its own (`display: contents`) still draws them.
  const words = document.createRange();
  const edges = (part) => {
    words.selectNodeContents(part);
    const drawn = words.getBoundingClientRect();
    return [drawn.left, drawn.width].map(Math.round).join(' ');
  };
  const moved = [];
  for (const table of document.querySelectorAll('main ' + scroll + ' > table')) {
    const toggle = [...table.querySelectorAll('tbody button[aria-expanded="false"]:not([data-fold-toggle])')].find((el) => el.checkVisibility());
    if (!toggle) continue;
    const row = toggle.closest('tr');
    const parts = [...row.children].filter((cell) => cell.checkVisibility()).flatMap((cell) =>
      [...cell.querySelectorAll('*')].filter((part) => part.checkVisibility()
        && [...part.childNodes].some((node) => node.nodeType === 3 && node.textContent.trim())
        && whole(part, cell)));
    const before = parts.map(edges);
    const began = performance.now();
    toggle.click();
    await frame();
    parts.forEach((part, at) => {
      const after = edges(part);
      if (after !== before[at]) moved.push(`${part.tagName.toLowerCase()} "${part.textContent.trim().slice(0, 24)}": ${before[at]} became ${after}`);
    });
    toggle.click();
    await frame();
    (window[moves] ??= []).push([began, performance.now()]);
  }
  return moved;
}"""
)

# A row folded under its family (`data-folded`) is drawn only once its family
# is opened (`data-open`), at every width: a rule that sets a row's look (a row
# stacked in a narrow box) must leave a folded row hidden. With no script
# every row shows, so it holds once script runs.
FOLDED_ROWS_SHOWN = (
    "() => {"
    + DESCRIBE
    + """
  if (!matchMedia('(scripting: enabled)').matches) return [];
  return [...document.querySelectorAll('.data-table tr[data-folded]:not([data-open])')]
    .filter((row) => row.checkVisibility({visibilityProperty: true}))
    .map((row) => `${describe(row)} is drawn while its family is folded: ${row.textContent.trim().replace(/\\s+/g, ' ').slice(0, 40)}`);
}"""
)

# What a row's open line holds starts where the row's words do, never left of
# them under its toggle: a row set in (in a group, under its family) keeps its
# open line in its own column.
OPEN_LINES_OUTDENTED = (
    "() => {"
    + DESCRIBE
    + """
  const found = new Set();
  for (const line of document.querySelectorAll('main table.data-table > tbody > tr.row-detail')) {
    const row = line.previousElementSibling;
    const words = row?.querySelector(':scope > td:has(> [data-row-toggle])');
    if (!words || !line.checkVisibility()) continue;
    const column = words.getBoundingClientRect().left + parseFloat(getComputedStyle(words).paddingInlineStart);
    for (const part of line.querySelectorAll(':scope > td > *')) {
      const left = part.getBoundingClientRect().left;
      if (part.checkVisibility() && left < column - 1) {
        found.add(`${describe(part)} starts ${Math.round(column - left)}px left of its row's words: ${row.innerText.trim().replace(/\\s+/g, ' ').slice(0, 40)}`);
      }
    }
  }
  return [...found];
}"""
)


# A row's toggle stands centered on the first line of the words it heads: the
# first line of text its cell shows after it, however that line is set (a
# taller mark beside it, the cell's padding moved by a control's rise).
TOGGLES_OFF_THEIR_LINE = (
    "() => {"
    + DESCRIBE
    + """
  // The first line box is read from the box that sets the words (their
  // nearest block): it starts at that box's content edge and is one of its
  // lines tall. A glyph's own box is no measure of it, as a face's ascent
  // and descent differ.
  const found = new Set();
  const range = document.createRange();
  for (const toggle of document.querySelectorAll('main tr > :is(td, th) > [data-row-toggle]')) {
    const cell = toggle.parentElement;
    const at = toggle.getBoundingClientRect();
    if (!toggle.checkVisibility() || at.height < 2) continue;
    // Words are shown where the box that sets them is: a box of words kept
    // for a screen reader is a pixel, and an element drawn as its contents
    // has no box of its own to be visible by. A box that stands in a line as
    // one piece (an inline-flex, an inline-block), and what it holds, sit in
    // the line of the box around it, which is the line read.
    const flows = (el) => /^(inline|contents)$|^inline-/.test(getComputedStyle(el).display);
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    let block = null;
    for (let node = walker.nextNode(); node && !block; node = walker.nextNode()) {
      if (!node.data.trim() || toggle.contains(node)) continue;
      let up = node.parentElement;
      while (up !== cell && (flows(up) || /^inline-/.test(getComputedStyle(up.parentElement).display))) up = up.parentElement;
      const r = up.getBoundingClientRect();
      range.selectNodeContents(node);
      if (up.checkVisibility({visibilityProperty: true}) && r.width >= 2 && r.height >= 2 && [...range.getClientRects()].some((each) => each.width >= 2)) block = up;
    }
    if (!block) continue;
    const style = getComputedStyle(block);
    const top = block.getBoundingClientRect().top + parseFloat(style.borderTopWidth) + parseFloat(style.paddingTop);
    // A line `normal` tall is as tall as its face says, which the block's
    // own height is while it holds the one line.
    const line = parseFloat(style.lineHeight) || block.getBoundingClientRect().height - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    const middle = top + line / 2;
    const off = (at.top + at.height / 2) - middle;
    if (Math.abs(off) > 1) {
      found.add(`${describe(toggle)} stands ${Math.abs(off).toFixed(1)}px ${off > 0 ? 'below' : 'above'} the middle of its row's first line: ${cell.innerText.trim().replace(/\\s+/g, ' ').slice(0, 40)}`);
    }
  }
  return [...found];
}"""
)


# A table with no headings whose rows open and hold controls, as the
# dashboard's queue draws them: its first row's words are moved down by the
# controls' rise, which the toggle follows.
def _toggles_beside_controls() -> DataTable:
    rows = tuple(
        Row(
            (Cell(f"Example finding {at}", below=(Below("", (Cell("Example note under the finding."),)),)),),
            actions=(PageAction("Open example", f"/example/{at}/"), PageAction("Restart", f"/example/{at}/restart/")),
        )
        for at in (1, 2)
    )
    return DataTable("Example findings", (TableColumn("Finding"),), rows, headed=False, controls_per_row=True)


_TOGGLES_BESIDE_CONTROLS = Example("{% data_table findings %}", values={"findings": _toggles_beside_controls()})
# Every toggle moved off its line.
_LIFT_THE_TOGGLES = "() => document.querySelectorAll('main [data-row-toggle]').forEach((toggle) => { toggle.style.translate = '0 -80%'; })"


# A row whose words hold a block for its open line.
def _open_lines() -> DataTable:
    item = Cell("Example item", below=(Below("", (Cell("Example note under the item."),)),))
    columns = (TableColumn("Item"), TableColumn("State", kind="status"))
    return DataTable("Open lines", columns, (Row((item, Cell("Up", tone="good"))),))


# The row's words set in further than its open line.
_INSET_THE_WORDS = "() => { document.querySelector('main tbody td.row-gutter').style.paddingInlineStart = '60px'; }"

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
  // A mark the stylesheet writes (a fold's chevron, a ::before or ::after
  // with words) is no element and has no box to read, so each is read as an
  // element drawn in its place with its style, the mark itself left out
  // while it stands there. Every stand-in goes in at once and is read in
  // one layout, which draws the page as its marks did.
  const quoted = /^"((?:[^"\\\\]|\\\\.)*)"/;
  const marks = [];
  for (const el of main.querySelectorAll('*')) {
    for (const side of ['before', 'after']) {
      const mark = getComputedStyle(el, `::${side}`);
      const words = quoted.exec(mark.content)?.[1];
      if (!words?.trim() || mark.display === 'none') continue;
      if (el.matches('input, img, select, textarea, br, hr, iframe, svg, svg *') || !el.checkVisibility() || inScroller(el)) break;
      const drawn = document.createElement('span');
      for (const name of mark) {
        if (name !== 'content') drawn.style.setProperty(name, mark.getPropertyValue(name));
      }
      drawn.style.setProperty('transition', 'none');
      drawn.textContent = words;
      marks.push({el, side, words, drawn});
    }
  }
  // A constructed sheet, which the page's policy does not govern.
  const stand = new CSSStyleSheet();
  stand.replaceSync('[data-probe-mark~="before"]::before, [data-probe-mark~="after"]::after { content: none !important; }');
  const sheets = document.adoptedStyleSheets;
  document.adoptedStyleSheets = [...sheets, stand];
  try {
    for (const {el, side, drawn} of marks) {
      el.dataset.probeMark = `${el.dataset.probeMark ?? ''} ${side}`.trim();
      if (side === 'before') el.prepend(drawn); else el.append(drawn);
    }
    for (const {el, side, words, drawn} of marks) {
      const r = painted(drawn, box(drawn), true);
      if (r && r.width >= 1 && (r.right > right + 1 || r.left < left - 1)) {
        found.push(`${describe(el)}::${side} "${words}" spans ${Math.round(r.left)}..${Math.round(r.right)}px, gutter ${Math.round(left)}..${Math.round(right)}px`);
      }
    }
  } finally {
    for (const {el, drawn} of marks) {
      drawn.remove();
      delete el.dataset.probeMark;
    }
    document.adoptedStyleSheets = sheets;
  }
  return [...new Set(found)];
}"""
)


COLUMNS_SHOWN = """() => [...document.querySelectorAll('thead th')]
  .filter((th) => getComputedStyle(th).display !== 'none').map((th) => th.textContent.replace(/[^A-Za-z ]/g, '').trim())"""

# Three tables whose every row the server made openable, each keeping its
# value's column on the row as a key (a narrow box sends any other column to
# the open line), so a row holds back only what the probe plants. In the
# first a narrow box drops a column: one row has something in it, one has a
# dash there. One row's only clipped text is for screen readers, beside a
# decoration hidden from them, and one row's value is longer than a line of
# its column (`data-long`). In the second a row folds part of a list, and a
# row's value is that long too. In the third a run of parts (`line_list`)
# holds only the parts that size its column, and another holds a rest past
# them.
ROW_TOGGLE_TABLES = "{% data_table dropping %}{% data_table folding %}{% data_table lining %}"
_LONG = "a value far too long to show whole on one line of its column"


def _probed(probe: str, *cells: Cell | str) -> Row:
    return Row(tuple(each if isinstance(each, Cell) else Cell(each) for each in cells), data={"probe": probe})


def row_toggle_values() -> dict[str, DataTable]:
    """The three tables ``ROW_TOGGLE_TABLES`` draws."""

    said = {"hit": True, "count": 1, "said": "One session in the week of Aug 10, said in full"}
    mark = Cell(piece=Piece(included_parts.CADENCE_MARK, {"week": said}, words=""))
    kept = TableColumn("Value", css="key-col")
    return {
        "dropping": DataTable(
            "Dropping",
            (TableColumn("Name"), kept, TableColumn("Note", drop=1)),
            (
                _probed("dropped", "Dropped", "Short", "Something too wide for a phone to keep"),
                _probed("dropped-blank", "Blank", "Short", "–"),
                _probed("screen-reader", "Chest", mark, "–"),
                _probed("cut", "Name", _LONG, "–"),
            ),
        ),
        "folding": DataTable(
            "Folding",
            (TableColumn("Name"), kept),
            (
                _probed("folded", "Folded", Cell("One", parts=(Cell("Two", shows="more"),))),
                _probed("cut-in-a-kept-gutter", "Name", _LONG),
            ),
        ),
        "lining": DataTable(
            "Lining",
            (TableColumn("Name"), TableColumn("Parts", css="key-col")),
            (
                _probed(
                    "line-fits", "Fits", Cell(fit=True, parts=tuple(Cell(part) for part in ("One", "Two", "Three")))
                ),
                _probed(
                    "line-cut",
                    "Cut",
                    Cell(
                        fit=True,
                        parts=tuple(
                            Cell(part) for part in ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight")
                        ),
                    ),
                ),
            ),
        ),
    }


# A table of cut values, their column kept on the row as a key, each the
# word its row is probed by then more: one with no title, one carrying its
# whole text as a title, two past the line a clamp ends with an ellipsis (a
# block and a link set in its line, as a cell's name is), one cut where no
# ellipsis can land, and a name's chip in the muted line under a row's name,
# as a unit's page drew one, which draws its own. The first table's columns
# all stay, so its rows hold nothing back and cannot open; the second's row
# opens for its long value, offering its toggle. A note wider than a phone
# puts each table in a narrow box there, where a row that cannot open breaks
# the chip whole; a wide box shows each titled value whole on hover, and a
# cut with no ellipsis tells no one to hover. Each cut is the style
# ``_CUT_THEM`` gives the value its name marks.
_CHIP = "chip-" + "-".join(["example-unit-name-long-enough-to-be-cut"] * 4) + ".timer"
_CUT_TABLE = "{% data_table cuts %}{% data_table opening %}"


def _cut_values() -> dict[str, DataTable]:
    def cut(name: str, **options) -> Cell:
        return Cell(f"{name} value far too long to fit", data={"cut": name}, **options)

    note = Cell("A note wider than a phone", data={"note": "wide"})
    columns = (TableColumn("Name"), TableColumn("Value", css="key-col"), TableColumn("Note", css="key-col"))
    rows = (
        ("Untitled", cut("untitled")),
        ("Titled", cut("titled", title="titled value far too long to fit")),
        ("Clamped", cut("clamped")),
        ("Inline", cut("inline", url="#")),
        ("Unmarked", cut("unmarked")),
        (Cell("Linked", url="#"), Cell(_CHIP, readout=True)),
        (Cell("Chip", lines=(Cell(_CHIP, readout=True, muted=True),)), Cell("Short")),
    )
    opening = (TableColumn("Name"), TableColumn("Value", css="key-col"), TableColumn("Note"))
    return {
        "cuts": DataTable(
            "Cuts",
            columns,
            tuple(Row((name if isinstance(name, Cell) else Cell(name), value, note)) for name, value in rows),
        ),
        "opening": DataTable(
            "Opening",
            opening,
            (Row((Cell("Opens"), Cell("opens value far too long to show whole on one line of its column"), note)),),
        ),
    }


_CUT_THEM = """() => {
  const ellipsis = 'max-width: 40px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap';
  const clamp = '-webkit-box-orient: vertical; -webkit-line-clamp: 1; overflow: hidden; max-width: 80px';
  const styles = {
    untitled: `display: block; ${ellipsis}`,
    titled: `display: block; ${ellipsis}`,
    clamped: `display: -webkit-box; ${clamp}`,
    inline: `display: -webkit-inline-box; ${clamp}`,
    unmarked: `display: block; ${ellipsis}`,
  };
  for (const value of document.querySelectorAll('main [data-cut]')) {
    value.style.cssText = styles[value.dataset.cut];
    if (value.dataset.cut !== 'unmarked') continue;
    const words = document.createElement('span');
    words.style.display = 'inline-block';
    words.append(...value.childNodes);
    value.append(words);
  }
  for (const note of document.querySelectorAll('main [data-note]')) note.style.cssText = 'display: inline-block; inline-size: 30rem';
}"""

# Two rows whose open line draws nothing (their state has not left): one the
# table clamps, which opening releases in place, and one with a short value.
# The ranked column is empty in both, so the state stays a state, which
# leaves only below every rank's width.
_CLAMP_TABLE = "{% data_table clamps %}"


def _clamp_values() -> dict[str, DataTable]:
    up = Cell("Up", tone="good")
    columns = (
        TableColumn("Name"),
        TableColumn("Value", css="key-col"),
        TableColumn("Note", drop=1),
        TableColumn("State", kind="status"),
    )
    clamped = " ".join(["a value that runs well past the three lines its clamp holds"] * 6)
    rows = (
        Row((Cell("Clamped"), Cell(clamped), Cell(""), up)),
        Row((Cell("Plain"), Cell("Short"), Cell(""), up), data={"probe": "plain"}),
    )
    return {"clamps": DataTable("Clamps", columns, rows)}


# A heading longer than its figures beside a column of room: sorted where two
# rows are, plain where one is, and squeezed by a width of its own.
_HEADED = "{% data_table two %}{% data_table one %}"


def _headed_values() -> dict[str, DataTable]:
    columns = (TableColumn("Name", kind="identifier"), TableColumn("Average heart rate", kind="numeric"))
    figures = Row((Cell("example-01"), Cell("160 bpm")))
    return {
        "two": DataTable("Headings", columns, (figures, figures)),
        "one": DataTable("Headings", columns, (figures,)),
    }


_SQUEEZE_THE_HEADING = """() => {
  const heading = document.querySelectorAll('main thead th')[1];
  const words = document.createElement('span');
  words.style.cssText = 'display: inline-block; max-inline-size: 4em; white-space: normal';
  words.textContent = 'Average heart rate';
  heading.replaceChildren(words);
}"""

# A band held to one column whose two short cells fit side by side.
_STACKED = (
    '<dl data-band style="display: grid; grid-template-columns: 100%; inline-size: 600px">'
    "<div><dt>Example</dt><dd>3</dd></div><div><dt>Example</dt><dd>4</dd></div></dl>"
)
# Five facts in a band as wide as the shared dialog holds one (forms.css),
# as a saved copy's About dialog draws its pages to try.
_FIVE_IN_A_DIALOG = (
    '<div style="inline-size: 440px">{% facts layout="band" %}'
    + "".join(f'{{% fact "Example {n}" value="An example page" url="/example/" icon="search" %}}' for n in range(1, 6))
    + "{% endfacts %}</div>"
)

# A panel shown from a cell, as an item's origin is: a key and a source too
# long for the cell, which the panel has the room to show whole.
_POPOVER_IN_A_CELL = """{% load value_tags %}{% data_table origins %}
<div class="modal origin-panel" id="example-origin" popover><div class="modal-panel">{% facts %}
{% fact "From" value=source %}{% fact "Key" %}<code>{{ key|breakable }}</code>{% endfact %}{% endfacts %}</div></div>"""


def _popover_values() -> dict[str, object]:
    columns = (TableColumn("Item"), TableColumn("Urgency"))
    row = Row((Cell("Example item"), Cell("Urgent", tone="attention", data={"origin": "example-origin"})))
    return {
        "origins": DataTable("Origins", columns, (row,)),
        "key": "example.approvals:approval:d7f2b3ed-186d-402a-9c5e-1f0b2a3c4d5e",
        "source": "an-example-source-with-a-deliberately-long-name",
    }


# The panel moved into the cell it is shown from, then shown, as a reader shows one.
_SHOW_POPOVER = """() => {
  const panel = document.getElementById('example-origin');
  document.querySelector('main [data-origin]').closest('td').append(panel);
  panel.showPopover();
}"""

# Every row's toggle pressed.
_OPEN_EVERY_ROW = "() => document.querySelectorAll('main [data-row-toggle]').forEach((toggle) => toggle.click())"
# Every row's toggle pressed, and then the plain row saying it holds a part below.
_OPEN_EVERY_ROW_AND_SAY_BELOW = """() => {
  document.querySelectorAll('main [data-row-toggle]').forEach((toggle) => toggle.click());
  document.querySelector('main tr[data-probe="plain"]').setAttribute('data-below', '');
}"""

# How many rows stand open, against how many should.
_EXPANDED = """(open) => {
  const opened = document.querySelectorAll('main tr.is-expanded').length;
  return opened === open ? [] : [`${opened} rows open, not ${open}`];
}"""

ROW_TOGGLES = """() => [...document.querySelectorAll('tbody tr:not([data-row-detail])')].map((row) => ({
  probe: row.dataset.probe,
  toggles: row.querySelectorAll('button[aria-expanded]').length,
  shown: [...row.querySelectorAll('button[aria-expanded]')].some((toggle) => toggle.checkVisibility()),
  beside_name: row.querySelector('button[aria-expanded]')?.parentElement === row.cells[0],
}))"""

# The survey checks these probes answer (`probes/__init__.py`); the table
# checks (`table=True`) are read again by the sweep of every width
# (`BrowserGate.table_defects_at_every_width`) at the first width of each shape
# of the layout and of its tables (`TABLE_LAYOUT`), in each state a reader can
# draw the rows in: as drawn, with their folds open, and opened.
CHECKS = {
    "cells": Check(
        CELLS,
        table=True,
        reader_sees="a cell's content running out of its cell",
        # A shown popover is drawn in the top layer, over the page, wherever
        # its markup sits; a box drawn in the cell still runs past it.
        plants=(
            Plant(
                '<table style="width: 200px; table-layout: fixed"><tr><td>'
                '<div class="example-popover" popover="manual" style="inline-size: 600px">example-popover</div>'
                '<div class="example-box" style="inline-size: 600px">example-box</div></td></tr></table>',
                setup="() => document.querySelector('main [popover]').showPopover()",
                finds=("div.example-box",),
            ),
        ),
    ),
    "tables": Check(
        TABLES,
        table=True,
        findings=lambda report: report["found"],
        reader_sees="a table wider than its box, a cell away from its heading or a heading drawn in a row",
    ),
    "tables fit": Check(
        (TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]),
        table=True,
        reader_sees="a table running past its box",
        part=DATA_TABLE,
    ),
    "unseen columns": Check(
        (UNSEEN_COLUMNS, SELECTORS["table_scroll"]),
        table=True,
        reader_sees="a column cut off where nothing scrolls to it",
        part=DATA_TABLE,
    ),
    "cut beside room": Check(
        (CUT_BESIDE_ROOM, SELECTORS["unseen"]), table=True, reader_sees="a value cut while its row has room to show it"
    ),
    "room unshared": Check(
        ROOM_UNSHARED,
        table=True,
        reader_sees="words a few to a line beside a column holding room it does not need, or a band's cells one to a row",
        plants=(
            Plant(_HEADED, values=_headed_values(), setup=_SQUEEZE_THE_HEADING, finds=("wraps its heading",)),
            Plant(_STACKED, finds=("one to a row",)),
        ),
        clear=(Example(_HEADED, values=_headed_values()), Example(_FIVE_IN_A_DIALOG)),
    ),
    "empty panels": Check(EMPTY_PANELS, table=True, reader_sees="a panel in a table with nothing in it"),
    "rows unruled": Check(ROWS_UNRULED, table=True, reader_sees="two rows with no rule between them"),
    "row state kept": Check(
        STATE_LEFT, table=True, reader_sees="a row's state, key or field gone with a column a narrow box let go"
    ),
    "cut and closed": Check(
        (CUT_AND_CLOSED, SELECTORS["unseen"]),
        table=True,
        reader_sees="a cut value with no way to read it whole",
        # Touch has no hover, so in a narrow box only an opening row shows a
        # cut value whole, where a wide box also shows a titled one on hover;
        # a cut that draws no ellipsis is found at any width. A chip is read
        # whole on the record's page its row links.
        plants=(
            Plant(
                _CUT_TABLE,
                width=PHONE_SCREEN[0],
                scripted=True,
                values=_cut_values(),
                setup=_CUT_THEM,
                finds=('"untitled', '"titled', '"clamped', '"inline', '"unmarked'),
            ),
            Plant(
                _CUT_TABLE,
                width=1280,
                scripted=True,
                values=_cut_values(),
                setup=_CUT_THEM,
                finds=('"untitled', '"clamped', '"inline', '"unmarked'),
            ),
        ),
        # A panel shown from a cell is no part of the column, so what it
        # holds reads whole however narrow the cell.
        clear=tuple(
            Example(_POPOVER_IN_A_CELL, width=width, scripted=True, values=_popover_values(), setup=_SHOW_POPOVER)
            for width in (PHONE_SCREEN[0], WIDE)
        ),
    ),
    "cut when open": Check(
        (CUT_WHEN_OPEN, SELECTORS["unseen"]), table=True, reader_sees="a value still cut in its opened row"
    ),
    "opens nothing": Check(
        OPENS_NOTHING,
        table=True,
        reader_sees="a row's toggle that opens nothing",
        # In a narrow box an open row that holds a clamp shows its value whole
        # in place, so its toggle opens something though its line draws
        # nothing; a row that says it holds a part below shows its toggle at
        # any width, and over an empty line it opens nothing.
        plants=(
            Plant(
                _CLAMP_TABLE,
                width=PHONE_SCREEN[0],
                scripted=True,
                values=_clamp_values(),
                setup=_OPEN_EVERY_ROW_AND_SAY_BELOW,
                premise=(_EXPANDED, 2),
                finds=("opens a line with nothing drawn",),
            ),
        ),
        clear=(
            Example(
                _CLAMP_TABLE,
                width=PHONE_SCREEN[0],
                scripted=True,
                values=_clamp_values(),
                setup=_OPEN_EVERY_ROW,
                premise=(_EXPANDED, 2),
            ),
        ),
    ),
    "table promises": Check(
        (TABLE_PROMISES, SELECTORS["table_scroll"]),
        reader_sees="a table's heading row scrolling away, or a cell across its row stopping short",
        part=DATA_TABLE,
    ),
    "whole parts moved by opening": Check(
        (WHOLE_PARTS_MOVED_BY_OPENING, SELECTORS["table_scroll"]),
        scripted=True,
        reader_sees="a row's words moving when the row is opened",
    ),
    "folded rows stay folded": Check(
        FOLDED_ROWS_SHOWN, table=True, scripted=True, reader_sees="a folded row drawn while its group is closed"
    ),
    "open lines outdented": Check(
        OPEN_LINES_OUTDENTED,
        table=True,
        reader_sees="what an opened row holds starting left of the row's own words",
        plants=(
            Plant(
                "{% data_table lines %}",
                scripted=True,
                values={"lines": _open_lines()},
                setup=f"() => {{ ({_INSET_THE_WORDS})(); ({_OPEN_EVERY_ROW})(); }}",
                finds=("left of its row's words",),
            ),
        ),
        clear=(
            Example("{% data_table lines %}", scripted=True, values={"lines": _open_lines()}, setup=_OPEN_EVERY_ROW),
        ),
    ),
    "toggles off their line": Check(
        TOGGLES_OFF_THEIR_LINE,
        table=True,
        reader_sees="a row's toggle above or below the line it opens",
        plants=(
            Plant(
                _TOGGLES_BESIDE_CONTROLS.markup,
                values=_TOGGLES_BESIDE_CONTROLS.values,
                setup=_LIFT_THE_TOGGLES,
                finds=("Example finding 1", "Example finding 2"),
            ),
        ),
        clear=(_TOGGLES_BESIDE_CONTROLS,),
    ),
    # Every box and every line of text, not only a card's or code's.
    "gutter": Check((PAST_THE_GUTTER, ["*", True]), reader_sees="content past the page's gutter"),
}
