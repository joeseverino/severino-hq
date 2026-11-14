"""Probes of tables: their cells, rows, columns and gutter, and the tables drawn to probe them."""

from hq.platform.core.tests.browser.probes import Check
from hq.platform.core.tests.browser.probes.describe import CUTS, DESCRIBE, PAINTED_PART
from hq.platform.core.tests.browser.selectors import SELECTORS

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
  const found = [];
  const cut = (el, cell) => {
    for (let b = el.parentElement; b && b !== cell.parentElement; b = b.parentElement) {
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
  // A table is laid out by the browser's table layout (tables.css), which
  // keeps every cell under its column's heading, and its headings are drawn.
  const width = document.documentElement.clientWidth;
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
    if (getComputedStyle(table).display !== 'table') found.push(`${name} is not laid out as a table`);
    const head = table.querySelector('thead');
    const headings = head ? [...head.querySelectorAll(':scope > tr:last-child > th')] : [];
    if (headings.length && !headings.some((heading) => heading.checkVisibility())) {
      found.push(`${name} hides its column headings`);
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
  return {seen, undrawn, found};
}"""
)


# What a grid table promises past what `TABLES` reads: a group's heading
# spans its table, the heading
# row sticks, a value its cell cuts on a closed row carries its whole text as
# its cell's title, and the row's controls drawn beneath its words (a narrow
# box) stand below every cell of its first line, in the order the row holds
# them. What breaks one, each named once.
TABLE_PROMISES = """(scroll) => {
  const found = new Set();
  const text = (el) => el.textContent.replace(/\\s+/g, ' ').trim();
  for (const region of document.querySelectorAll('main ' + scroll)) {
    const table = region.querySelector(':scope > table');
    if (!table || !table.checkVisibility()) continue;
    const name = (region.getAttribute('aria-label') || text(table.querySelector('th') || table)).slice(0, 24);
    const width = table.getBoundingClientRect().width;
    for (const cell of table.querySelectorAll(':scope > tbody > tr > [colspan]')) {
      if (cell.checkVisibility() && cell.getBoundingClientRect().width < width - 2) found.add(`${name}: a cell across its row spans less`);
    }
    const head = table.querySelector(':scope > thead > tr');
    if (head && getComputedStyle(head).position !== 'sticky') found.add(`${name}: its heading row does not stick`);
    for (const row of table.querySelectorAll(':scope > tbody > tr:not(.is-expanded):not([data-row-detail])')) {
      for (const cell of row.querySelectorAll(':scope > [data-kind]')) {
        if (!cell.checkVisibility() || getComputedStyle(cell).gridRowStart !== '1') continue;
        const parts = [cell, ...cell.querySelectorAll('*')].filter((el) => el.checkVisibility());
        const cut = parts.some((el) => el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1);
        const said = text(cell);
        if (cut && said && !(cell.title || '').replace(/\\s+/g, ' ').includes(said.slice(0, 24))) {
          found.add(`${name}: ${said.slice(0, 24)} is cut with no title holding it whole`);
        }
      }
      const cells = [...row.children].filter((cell) => cell.checkVisibility());
      const line = cells.filter((cell) => getComputedStyle(cell).gridRowStart === '1');
      const beneath = cells.filter((cell) => cell.hasAttribute('data-beneath') && !line.includes(cell));
      const floor = Math.max(...line.map((cell) => cell.getBoundingClientRect().bottom));
      let last = -Infinity;
      for (const cell of beneath) {
        const top = cell.getBoundingClientRect().top;
        if (top < floor - 1) found.add(`${name}: its controls stand beside the row's first line`);
        if (top < last - 1) found.add(`${name}: its controls stand out of the row's order`);
        last = top;
      }
    }
  }
  return [...found];
}"""

# The first table on a page as a phone lays it out: whether it runs past its
# box, where each showing column starts and how wide it is, how tall each record's row is, how
# many rows show their toggle, and the columns whose cells the open rows
# draw beneath them, each under its column's name (`data-label`).
PHONE_TABLE = """(scroll) => {
  const box = document.querySelector('main ' + scroll);
  const table = box && box.querySelector('table');
  if (!table) return null;
  const showing = (el) => getComputedStyle(el).display !== 'none';
  return {
    sideways: Math.round(box.scrollWidth - box.clientWidth),
    headings: table.querySelectorAll('thead th').length,
    columns: [...table.querySelectorAll('thead th')].filter(showing)
      .map((heading) => [heading.getBoundingClientRect().left, heading.getBoundingClientRect().width].map(Math.round)),
    rows: [...table.querySelectorAll('tbody > tr:not([data-row-detail]):not(:has(> th[scope="rowgroup"]))')]
      .map((row) => Math.round(row.getBoundingClientRect().height)),
    toggles: [...table.querySelectorAll('tbody button[aria-expanded]:not([data-fold-toggle])')].filter((el) => el.checkVisibility()).length,
    details: [...table.querySelectorAll('tbody > tr > [data-drop][data-label]')].filter((cell) => cell.checkVisibility() && getComputedStyle(cell).gridRowStart !== '1').map((cell) => cell.dataset.label),
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


# A table region's layout at this width, as a value that changes only where the
# layout around it changes shape as the window grows: which viewport queries
# hold, whether the page scrolls, around each region how many tracks each grid
# has and how many lines each wrapping flex box makes, and how far each
# region's table has given way (tables.css: which ranks it keeps, and whether
# its controls stand beneath its words). Each of those moves one way as the
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
  const gave = (table) => {
    if (!table) return '';
    const style = getComputedStyle(table);
    return ['--whole', '--keep-1', '--keep-2', '--keep-3', '--keep-4', '--keep-5', '--keep-6']
      .map((name) => style.getPropertyValue(name).trim()).join('');
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
# fills keeps its padding), and the room each other column showing holds
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
    const slack = new Map();
    for (const cell of cells) {
      const place = cell.cellIndex;
      const held = cuts.some((cut) => cut.cell.cellIndex === place) ? 0 : cell.getBoundingClientRect().width - (asks.get(place) ?? Infinity);
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
      const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
      for (let text = walker.nextNode(); text; text = walker.nextNode()) {
        if (!text.data.trim() || by.contains(text) || !textShown(text)) continue;
        range.selectNodeContents(text);
        for (const line of range.getClientRects()) {
          if (line.width && Math.min(line.bottom, r.bottom) - Math.max(line.top, r.top) > line.height / 2) end = Math.max(end, line.right);
        }
      }
      const own = Math.max(0, inner(cell).right - end);
      const asksNothing = boxes.some((box) => /flex/.test(getComputedStyle(box.parentElement).display)
        && getComputedStyle(box).flexGrow !== '0' && parseFloat(getComputedStyle(box).flexBasis) === 0);
      // A cell that stands beneath its row spans it, beside no other column.
      const spans = getComputedStyle(cell).gridColumnEnd === '-1';
      const others = asksNothing || spans ? 0 : [...slack].reduce((sum, [place, room]) => sum + (place === cell.cellIndex ? 0 : room), 0);
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


# The room a table has past what its columns ask goes to the columns that
# share it alike (`--tracks`, table_cells), never to one while another goes
# short: a column drawn wider than its widest cell asks while a column that
# shares (a name, prose, an identifier, unless it can stand beneath the row's
# words, which holds) is drawn narrower than its own asks,
# or, every such column given what it asks, one holding the room the others
# the table keeps go without (a column that may leave takes no room past its
# asks, since a track that grows into room grows while its column is gone
# too). What a column asks is read from the table laid out again at its
# max-content, unseen, beside it; a cell drawn across the row asks no column
# for anything, so it counts for neither.
ROOM_UNSHARED = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  const records = ':scope > tbody > tr:not(.row-detail, [data-row-detail], .is-expanded)';
  const placed = (table) => [...table.querySelectorAll(records)].filter((row) => row.checkVisibility())
    .flatMap((row) => [...row.querySelectorAll(':scope > :is(td, th)')])
    .filter((cell) => cell.colSpan === 1 && cell.checkVisibility() && getComputedStyle(cell).gridColumnEnd !== '-1');
  for (const table of document.querySelectorAll('main table.data-table')) {
    if (!table.checkVisibility() || getComputedStyle(table).display !== 'grid') continue;
    const cells = placed(table);
    if (!cells.length) continue;
    const ruler = table.cloneNode(true);
    ruler.setAttribute('aria-hidden', 'true');
    ruler.style.cssText += '; position: absolute; visibility: hidden; inset: 0 auto auto 0; inline-size: max-content; max-inline-size: none';
    table.after(ruler);
    const asks = new Map();
    for (const cell of placed(ruler)) asks.set(cell.cellIndex, Math.max(asks.get(cell.cellIndex) ?? 0, cell.getBoundingClientRect().width));
    ruler.remove();
    const columns = new Map();
    for (const cell of cells) {
      if (columns.has(cell.cellIndex) || !asks.has(cell.cellIndex)) continue;
      columns.set(cell.cellIndex, {
        slack: cell.getBoundingClientRect().width - asks.get(cell.cellIndex),
        shares: ['name', 'prose', 'identifier'].includes(cell.dataset.kind) && !('beneath' in cell.dataset),
        kept: !cell.dataset.drop,
      });
    }
    const heads = [...table.querySelectorAll(':scope > thead > tr:last-child > th')];
    const named = (place) => heads[place]?.textContent.trim() || `column ${place + 1}`;
    const all = [...columns];
    const [roomy, most] = all.reduce((best, [place, {slack}]) => slack > best[1] ? [place, slack] : best, [null, 24]);
    if (roomy === null) continue;
    const sharing = all.filter(([, {shares}]) => shares);
    const short = sharing.find(([place, {slack}]) => place !== roomy && slack < -2);
    if (short) {
      found.push(`${describe(table)}: ${named(roomy)} holds ${Math.round(most)}px past its widest cell while ${named(short[0])} gets ${Math.round(-short[1].slack)}px less than it asks`);
      continue;
    }
    const least = sharing.filter(([, {kept}]) => kept).reduce((low, entry) => !low || entry[1].slack < low[1].slack ? entry : low, null);
    if (least && least[0] !== roomy && most - least[1].slack > 24) {
      found.push(`${describe(table)}: ${named(roomy)} holds ${Math.round(most)}px past its widest cell while ${named(least[0])} holds ${Math.round(least[1].slack)}px`);
    }
  }
  return found;
}"""
)


# What a row is (its state, a cell its pills say) and what a reader changes in it (a field)
# never leave with a column a narrow box lets go: a row without them no
# longer says what it is or does what it is for.
STATE_LEFT = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  for (const cell of document.querySelectorAll('main table.data-table > tbody > tr:not(.is-expanded) > [data-drop]:not([data-blank])')) {
    if (!cell.closest('table').checkVisibility() || cell.checkVisibility()) continue;
    // A pill beside a value is a note on it; a cell its pills alone say is the row's state.
    const pills = [...cell.querySelectorAll('.pill')];
    const stated = pills.length && pills.map((pill) => pill.textContent).join('').replace(/\\s+/g, '') === cell.textContent.replace(/\\s+/g, '');
    const held = cell.querySelector('select, textarea, input:not([type="hidden"])') || (stated ? pills[0] : null);
    if (held) found.push(`${describe(cell)} "${(cell.dataset.label || '').slice(0, 30)}" leaves with its column, holding ${describe(held)}`);
  }
  return found;
}"""
)


# A value cut short that nothing shows whole: its row does not open at this
# width, and either the cut draws no ellipsis, so nothing tells a reader there
# is more to see, or nothing around it carries its whole text as a title or a
# tip (`data-tip`, app.js), or its box is narrow (`--whole`: 0, tables.css),
# where no pointer hovers. Cut is read as `CUTS` reads it, up to the cell.
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
    const narrow = getComputedStyle(row.closest('table')).getPropertyValue('--whole').trim() === '0';
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

# In every table on the page with a row toggle, opens its first row and reads
# each part of that row's cells that showed whole before it opened, a box
# holding text that neither it nor anything up to its cell cuts: each that
# moved or changed width once the row opened, as text. Opening a row only
# lifts cuts, so a part that was whole keeps its box.
WHOLE_PARTS_MOVED_BY_OPENING = """async (scroll) => {
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
    toggle.click();
    await frame();
    parts.forEach((part, at) => {
      const after = edges(part);
      if (after !== before[at]) moved.push(`${part.tagName.toLowerCase()} "${part.textContent.trim().slice(0, 24)}": ${before[at]} became ${after}`);
    });
    toggle.click();
    await frame();
  }
  return moved;
}"""

# Press the first row toggle (never a family fold) that shows in the page's first table, and say which
# of its records (counted as PHONE_TABLE counts them) it opened.
PRESS_FIRST_ROW_TOGGLE = """(scroll) => {
  const table = document.querySelector('main ' + scroll).querySelector('table');
  const records = [...table.querySelectorAll('tbody > tr:not([data-row-detail]):not(:has(> th[scope="rowgroup"]))')];
  const toggle = [...table.querySelectorAll('tbody button[aria-expanded]:not([data-fold-toggle])')].find((el) => el.checkVisibility());
  toggle.click();
  return records.indexOf(toggle.closest('tr'));
}"""

# A row folded under its family (`data-folded`) is drawn only once its family
# is opened (`data-open`), at every width: a rule that sets a row's display (a
# row stacked in a narrow box) must leave a folded row folded. With no script
# every row shows, so it holds once script runs.
FOLDED_ROWS_SHOWN = (
    "() => {"
    + DESCRIBE
    + """
  if (!matchMedia('(scripting: enabled)').matches) return [];
  return [...document.querySelectorAll('.data-table tr[data-folded]:not([data-open])')]
    .filter((row) => row.checkVisibility())
    .map((row) => `${describe(row)} is drawn while its family is folded: ${row.textContent.trim().replace(/\\s+/g, ' ').slice(0, 40)}`);
}"""
)

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

# A row's two controls with labels of two words, beside a name and a status,
# in a card: on the narrowest phone their column leaves for the line beneath
# the row, each label whole. Beside it a table that nothing it gives can fit,
# a cell asking more than a phone has, which scrolls in its region rather than
# run past its card.
ROW_ACTIONS_TABLES = """<section class="card">
{% table_region "Devices" %}<table class="data-table" data-probe="actions"><thead><tr>
{% table_heading "Device" "identifier" gutter=True %}{% table_heading "Approval" "status" %}
{% table_heading "Calls" drop=1 %}{% table_heading "Approve or block" "control" hidden=True drop=2 %}</tr></thead><tbody>
<tr><th scope="row">example-host-01</th><td><span class="pill">Needs approval</span></td><td>2 calls</td>
<td class="row-actions"><form><button type="button" class="btn compact primary">Withdraw approval</button></form><form><button type="button" class="btn compact">Block device</button></form></td></tr>
</tbody></table>{% endtable_region %}
{% table_region "Unfittable" %}<table class="data-table" data-probe="unfittable"><thead><tr>{% table_heading "Name" %}</tr></thead><tbody>
<tr><td><span style="display: inline-block; inline-size: 40rem">Example wider than any phone</span></td></tr>
</tbody></table>{% endtable_region %}
</section>"""

COLUMNS_SHOWN = """() => [...document.querySelectorAll('thead th')]
  .filter((th) => getComputedStyle(th).display !== 'none').map((th) => th.textContent.replace(/[^A-Za-z ]/g, '').trim())"""

# Three tables whose every row the server made openable. In the first a
# narrow box drops a column: one row has something in it, one has a dash
# there. One row's only clipped text is for screen readers, beside a
# decoration hidden from them, and one row's value is longer than a line of
# its column (`data-long`). In the second a row folds part of a list, and a
# row's value is that long too. In the third a run of parts (`line_list`)
# holds only the parts that size its column, and another holds a rest past
# them.
ROW_TOGGLE_TABLES = """
{% table_region "Dropping" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Value" %}{% table_heading "Note" drop=1 %}</tr></thead><tbody>
<tr data-probe="dropped"><td>Dropped</td><td>Short</td>
<td><span style="display: inline-block; inline-size: 30rem">Something too wide for a phone to keep</span></td></tr>
<tr data-probe="dropped-blank"><td>Blank</td><td>Short</td><td>–</td></tr>
<tr data-probe="screen-reader"><td>Chest</td>
<td><span class="cadence-mark is-hit" aria-hidden="true"></span>
<span class="visually-hidden">One session in the week of Aug 10, said in full</span></td><td>–</td></tr>
<tr data-probe="cut"><td>Name</td>
<td>a value far too long to show whole on one line of its column</td><td>–</td></tr>
</tbody></table>{% endtable_region %}
{% table_region "Folding" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Value" %}</tr></thead><tbody>
<tr data-probe="folded"><td>Folded</td>
<td><span class="tag">One</span><span class="muted row-less">+1</span><span class="row-more"><span class="tag">Two</span></span></td></tr>
<tr data-probe="cut-in-a-kept-gutter"><td>Name</td>
<td>a value far too long to show whole on one line of its column</td></tr>
</tbody></table>{% endtable_region %}
{% table_region "Lining" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Parts" %}</tr></thead><tbody>
<tr data-probe="line-fits"><td>Fits</td>
<td>{% line_list fits as part %}<span class="tag">{{ part }}</span>{% endline_list %}</td></tr>
<tr data-probe="line-cut"><td>Cut</td>
<td><div style="max-width: 9rem">{% line_list many as part %}<span class="tag">{{ part }}</span>{% endline_list %}</div></td></tr>
</tbody></table>{% endtable_region %}"""

ROW_TOGGLE_VALUES = {
    "fits": ("One", "Two", "Three"),
    "many": ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight"),
}

# A table of cut values, each the word its row is probed by then more: one
# with no title, one carrying its whole text as a title, one longer than a
# line of its column in a row that opens, two past the line a clamp ends with an ellipsis (a block and a
# link set in its line, as a cell's name is), one cut where no ellipsis can
# land, a box of words around a box of its own, and a name's chip in the muted
# line under a row's name, as a unit's page drew one, which draws its own; the
# third stands in a table of its own whose rows open, since every row under a
# gutter heading does, and offers its toggle for its long value. A note wider
# than a phone puts each table in a narrow box there, where a row that cannot
# open breaks the chip whole; a wide box shows each titled value whole on
# hover, and a cut with no ellipsis tells no one to hover.
_CUT = (
    '<div style="max-width: 40px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap"{}>'
    "{} value far too long to fit</div>"
)
_CHIP = "chip-" + "-".join(["example-unit-name-long-enough-to-be-cut"] * 4) + ".timer"
_NOTE = '<td><span style="display: inline-block; inline-size: 30rem">A note wider than a phone</span></td>'
CUT_TABLE = (
    '{% load value_tags %}{% table_region "Cuts" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Name" %}{% table_heading "Value" %}{% table_heading "Note" %}</tr></thead><tbody>'
    + "".join(
        f"<tr><td>{name.title()}</td><td>{_CUT.format(title.format(name), name)}</td>{_NOTE}</tr>"
        for name, title in (("untitled", ""), ("titled", ' title="{} value far too long to fit"'))
    )
    + '<tr><td>Clamped</td><td><div style="display: -webkit-box; -webkit-box-orient: vertical; '
    '-webkit-line-clamp: 1; overflow: hidden; max-width: 80px">clamped value far too long to fit on its one line'
    f"</div></td>{_NOTE}</tr>"
    + '<tr><td>Inline</td><td><a href="#" style="display: -webkit-inline-box; -webkit-box-orient: '
    'vertical; -webkit-line-clamp: 1; overflow: hidden; max-width: 80px">inline value far too long to fit on its one '
    f"line</a></td>{_NOTE}</tr>" + '<tr><td>Unmarked</td><td><div style="max-width: 40px; overflow: hidden; '
    'text-overflow: ellipsis; white-space: nowrap"><span style="display: inline-block">unmarked value far too long '
    f"to fit</span></div></td>{_NOTE}</tr>"
    + f'<tr><td>Chip<br><span class="muted"><code>{{{{ chip|breakable }}}}</code></span></td><td>Short</td>{_NOTE}</tr>'
    + "</tbody></table>{% endtable_region %}"
    + '{% table_region "Opening" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Name" gutter=True %}{% table_heading "Value" %}{% table_heading "Note" %}</tr></thead><tbody>'
    + f"<tr><td>Opens</td><td>opens value far too long to show whole on one line of its column</td>{_NOTE}</tr>"
    "</tbody></table>{% endtable_region %}"
)

# The chip's name is a value the page prints, as a template's is: a literal is
# already safe, so `breakable` would pass it as plain words, which wrap.
CUT_VALUES = {"chip": _CHIP}

# Rows that open, as an event's log draws them: a host name in a column a
# phone lets go, so an opened row shows it in the line beneath, and a value
# its cell cuts while the row is closed.
OPEN_ROW_TABLE = """{% load value_tags %}
{% table_region "Opened" %}<table class="data-table"><thead><tr>
{% table_heading "Name" gutter=True %}{% table_heading "Object" drop=1 %}{% table_heading "Note" %}</tr></thead><tbody>
<tr><td>Updated</td><td>{{ host|breakable }}</td>
<td><span style="display: inline-block; inline-size: 14rem">A note that keeps its line</span></td></tr>
<tr><td>Created</td><td>{{ host|breakable }}</td>
<td>a value far too long to show whole on one line of its column, so its cell cuts it while the row is closed</td></tr>
</tbody></table>{% endtable_region %}"""

OPEN_ROW_VALUES = {"host": "example-hostname-with-a-long-descriptive-name-01.example.com"}

# A panel shown from a cell, as an item's origin is: a key and a source too
# long for the cell, which the panel has the room to show whole.
POPOVER_IN_A_CELL = """{% load value_tags %}
{% table_region "Origins" %}<table class="data-table"><thead><tr>
{% table_heading "Item" gutter=True %}{% table_heading "Urgency" %}</tr></thead><tbody>
<tr><td class="row-gutter">Example item</td><td><span class="pill">Urgent</span>
<div class="modal origin-panel" id="example-origin" popover><div class="modal-panel">{% facts %}
{% fact "From" value=source %}{% fact "Key" %}<code>{{ key|breakable }}</code>{% endfact %}{% endfacts %}</div></div></td></tr>
</tbody></table>{% endtable_region %}"""

POPOVER_VALUES = {
    "key": "example.approvals:approval:d7f2b3ed-186d-402a-9c5e-1f0b2a3c4d5e",
    "source": "an-example-source-with-a-deliberately-long-name",
}

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
{% table_heading "Since" drop=3 %}{% table_heading "Impact" drop=2 %}{% table_heading "Read from" drop=1 %}</tr></thead><tbody>
<tr><td>Proxy host</td><td><a href="/proxy/" data-entity="Proxy host">{{ address|breakable }}</a></td>
<td><span class="pill">Up</span></td><td>1 day</td><td>If it is removed, the name stops reaching the app it serves.</td>
<td>Read from the proxy's own list of hosts, a minute ago</td></tr>
<tr><td>Forwards to</td><td>{{ address|breakable }}</td><td><span class="pill">Up</span></td><td>1 day</td>
<td>If nothing listens there, a reader gets a gateway error.</td><td>Read from the container's ports</td></tr>
<tr><td>Service</td><td><span data-entity="Service">{{ host|breakable }}</span></td><td></td><td>1 day</td>
<td>The service stops answering at this name.</td><td>Declared in HQ</td></tr>
</tbody></table>{% endtable_region %}
{% table_region "Access" %}<table class="data-table"><thead><tr>
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


# The survey checks these probes answer (`probes/__init__.py`); the table
# checks (`table=True`) are read again by the sweep of every width
# (`BrowserGate.table_defects_at_every_width`) at the first width of each shape
# of the layout and of its tables (`TABLE_LAYOUT`), in each state a reader can
# draw the rows in: as drawn, with their folds open, and opened.
CHECKS = {
    "cells": Check(CELLS, table=True, reader_sees="a cell's content running out of its cell"),
    "tables": Check(
        TABLES, table=True, findings=lambda report: report["found"], reader_sees="a table wider than its box"
    ),
    "tables fit": Check(
        (TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]),
        table=True,
        reader_sees="a table running past its box",
    ),
    "unseen columns": Check(
        (UNSEEN_COLUMNS, SELECTORS["table_scroll"]),
        table=True,
        reader_sees="a column cut off where nothing scrolls to it",
    ),
    "cut beside room": Check(
        (CUT_BESIDE_ROOM, SELECTORS["unseen"]), table=True, reader_sees="a value cut while its row has room to show it"
    ),
    "room unshared": Check(ROOM_UNSHARED, table=True, reader_sees="a column holding room another column goes without"),
    "row state kept": Check(
        STATE_LEFT, table=True, reader_sees="a row's state or its field gone with a column a narrow box let go"
    ),
    "cut and closed": Check(
        (CUT_AND_CLOSED, SELECTORS["unseen"]), table=True, reader_sees="a cut value with no way to read it whole"
    ),
    "cut when open": Check(
        (CUT_WHEN_OPEN, SELECTORS["unseen"]), table=True, reader_sees="a value still cut in its opened row"
    ),
    "folded rows stay folded": Check(
        FOLDED_ROWS_SHOWN, table=True, scripted=True, reader_sees="a folded row drawn while its group is closed"
    ),
    # Every box and every line of text, not only a card's or code's.
    "gutter": Check((PAST_THE_GUTTER, ["*", True]), reader_sees="content past the page's gutter"),
}
