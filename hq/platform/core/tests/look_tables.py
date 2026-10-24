"""What dev:look reads from a page's tables, run in the page: what each
table shows a reader (`--rule tables`), and each column's widths as drawn and
at its intrinsic sizes (`--columns`), with the line a table's columns are
printed on."""

# A heading's words as a reader hears them, without the marks beside them.
NAME = r"""
  const name = (el) => {
    if (!el) return '';
    const copy = el.cloneNode(true);
    copy.querySelectorAll('[aria-hidden=true]').forEach((mark) => mark.remove());
    return copy.textContent.trim().replace(/\s+/g, ' ');
  };
"""

# Each table as a reader finds it: which columns show, which are gone and
# whether for being blank or for want of room, which rows offer to open and
# have the gutter for it, the sort controls, and what each open row lists.
TABLES = (
    "() => {"
    + NAME
    + r"""
  const blank = (cell) => !cell || (/^[\s\u2013\u2014-]*$/.test(cell.textContent)
    && !cell.querySelector(':not(.empty-value, .empty-value *, br)'));
  const seen = (el) => el.checkVisibility();
  const pad = (el) => Math.round(parseFloat(getComputedStyle(el).paddingInlineStart));
  const tally = (values) => {
    const counts = new Map();
    values.forEach((value) => counts.set(value, (counts.get(value) || 0) + 1));
    return [...counts].map(([value, count]) => (count > 1 ? `${value} x${count}` : value)).join('; ');
  };
  const out = [];
  document.querySelectorAll('main table').forEach((table, index) => {
    if (!seen(table)) return;
    const region = table.closest('.table-scroll');
    const label = region?.getAttribute('aria-label') || table.caption?.textContent.trim() || '';
    const rows = [...table.querySelectorAll(':scope > tbody > tr:not([data-row-detail])')].filter(seen);
    const records = rows.filter((row) => !row.classList.contains('row-group'));
    const headings = [...(table.tHead?.rows[table.tHead.rows.length - 1]?.cells || [])];
    const columns = headings.map((heading, column) => {
      const said = name(heading) || '(unnamed)';
      if (seen(heading)) return said;
      const full = records.filter((row) => row.cells.length === headings.length);
      return (full.length && full.every((row) => blank(row.cells[column])) ? '~' : '-') + said;
    });
    const toggles = records.filter((row) => [...row.querySelectorAll('[data-row-toggle]')].some(seen));
    // The column a shown toggle stands in, else the one that names the row:
    // the gutter is its inset.
    const shown = toggles.map((row) => [...row.querySelectorAll('[data-row-toggle]')].find(seen)?.closest('td, th')).find(Boolean);
    const column = shown ? shown.cellIndex
      : Math.max(0, headings.findIndex((heading) => !heading.classList.contains('select-column') && !heading.querySelector('input[type=checkbox]')));
    const gutter = records.map((row) => row.cells[column]).filter(Boolean).map(pad);
    const headingPad = headings[column] ? pad(headings[column]) : '-';
    const at = (rows) => (rows.length === records.length ? 'every row' : rows.length ? rows.map((row) => records.indexOf(row) + 1).join(',') : 'none');
    const open = [...table.querySelectorAll(':scope > tbody > tr[data-row-detail]')].map(
      (detail) => [...detail.querySelectorAll('.row-columns > dt')].map((term) => term.textContent.trim()).join(', ') || '(below only)',
    );
    const r = region?.getBoundingClientRect();
    const lines = [
      `[${index + 1}] ${label}${region ? ` region ${Math.round(r.width)}` : ''} table ${Math.round(table.getBoundingClientRect().width)}`
        + `${region?.classList.contains('is-pane') ? ' pane' : ''}`
        + ` cap ${getComputedStyle(table).getPropertyValue('--identifier-max').trim()}`,
      `  columns: ${columns.join(' | ') || '(no headings)'}`,
      `  widths: ${headings.filter(seen).map((heading) => Math.round(heading.getBoundingClientRect().width)).join(' ')}`,
      `  rows ${records.length}; toggles: ${at(toggles)}; sort ${[...table.querySelectorAll(':scope > thead .table-sort-link')].filter(seen).length}`,
      `  inset of the toggle column: heading ${headingPad}; cells ${tally(gutter) || '-'}`,
    ];
    if (open.length) lines.push(`  open: ${tally(open)}`);
    out.push(lines.join('\n'));
  });
  return out.join('\n');
}"""
)


# Each shown table's columns: the width each is drawn at, and the widths it
# would take at min-content and at max-content, read from copies laid out
# beside the table (so its container and cascade are the table's own), off
# screen and removed at once. The drawn table is never restyled.
COLUMNS = (
    "() => {"
    + NAME
    + r"""
  const across = (table) => [...((table.tHead?.rows[table.tHead.rows.length - 1] || table.rows[0])?.cells || [])];
  const width = (el) => Math.round(el.getBoundingClientRect().width);
  const laidOut = (table, size) => {
    const copy = table.cloneNode(true);
    copy.setAttribute('aria-hidden', 'true');
    copy.querySelectorAll('[id]').forEach((el) => el.removeAttribute('id'));
    // Let go of what an open row holds (tables.js), so the copy is sized by
    // its content.
    copy.querySelectorAll(':scope > thead > tr > th').forEach((heading) => heading.style.removeProperty('width'));
    delete copy.dataset.rowsHeld;
    const style = {
      position: 'absolute', 'inset-inline-start': '-100000px', 'inset-block-start': '0', visibility: 'hidden',
      'table-layout': 'auto', 'inline-size': size, 'min-inline-size': '0', 'max-inline-size': 'none',
    };
    Object.entries(style).forEach(([property, value]) => copy.style.setProperty(property, value, 'important'));
    table.after(copy);
    try {
      return { table: width(copy), cells: across(copy).map(width) };
    } finally {
      copy.remove();
    }
  };
  const shown = [...document.querySelectorAll('main table')].map((table, index) => ({ table, index }))
    .filter(({ table }) => table.checkVisibility());
  return shown.map(({ table, index }) => {
    const region = table.closest('.table-scroll');
    const least = laidOut(table, 'min-content');
    const most = laidOut(table, 'max-content');
    return {
      index: index + 1,
      label: region?.getAttribute('aria-label') || table.caption?.textContent.trim() || '',
      region: region ? width(region) : null,
      drawn: width(table),
      // An open row holds the columns at the widths they had (tables.js).
      held: 'rowsHeld' in table.dataset,
      min: least.table,
      max: most.table,
      columns: across(table).map((cell, column) => ({
        name: name(cell) || '(unnamed)',
        shown: cell.checkVisibility(),
        drawn: width(cell),
        min: least.cells[column],
        max: most.cells[column],
      })),
    };
  });
}"""
)


def columns_line(page: str, width: int, table: dict) -> str:
    """One table's columns on one line: each drawn width, and the widths it
    would take at min-content and at max-content; a column the table does not
    show is named as gone. A table an open row holds says that it is drawn
    at the held widths, which its min and max are not."""

    label = f' "{table["label"]}"' if table["label"] else ""
    region = f" in {table['region']}" if table["region"] is not None else ""
    held = (
        "; held by an open row: drawn at the widths it had before, min and max measured without the hold"
        if table["held"]
        else ""
    )
    head = (
        f"{page} table {table['index']}{label} at {width}"
        f" (drawn {table['drawn']}{region}; min {table['min']}, max {table['max']}{held})"
    )
    columns = [
        f"{column['name']} {column['drawn']} (min {column['min']}, max {column['max']})"
        if column["shown"]
        else f"{column['name']} gone"
        for column in table["columns"]
    ]
    return f"{head}: {' · '.join(columns) or '(no headings)'}"
