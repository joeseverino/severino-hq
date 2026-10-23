"""dev:look's ``floors`` rule: each table column's derived floor beside the
browser's own min-content for it.

A table's region derives where each column leaves from the floors of the
columns still showing (`table_fit`), so a floor below what the browser needs
lets the table overflow its region before the column leaves, and one far above
it drops the column while it still fit. The rule lays each table out in a copy
of its region as wide as each width a column leaves at, where the columns
still showing are at their tightest, and sets the browser's min-content for
each beside its floor.
"""

from hq.platform.application.table_fit import floors_at
from hq.platform.core.templatetags.table_tags import fit_columns

# A heading's words as a reader hears them, without the marks beside them.
NAME = r"""
  const name = (el) => {
    if (!el) return '';
    const copy = el.cloneNode(true);
    copy.querySelectorAll('[aria-hidden=true]').forEach((mark) => mark.remove());
    return copy.textContent.trim().replace(/\s+/g, ' ');
  };
"""

# Each region's own table, laid out at min-content in an offscreen copy of its
# region as wide as each width one of its columns leaves at (or, where none
# leaves, the width below which its box is narrow): the width each column
# heading takes there, or null for one gone at that width.
_MEASURE = (
    "() => {"
    + NAME
    + r"""
  const width = (el) => Math.round(el.getBoundingClientRect().width);
  // The cells that stand for its columns: its last heading row's, or in a
  // table without headings its first row of no spanning cell, as its region
  // reads them.
  const across = (table) => [...((table.tHead?.rows[table.tHead.rows.length - 1]
    || [...table.rows].find((row) => ![...row.cells].some((cell) => cell.colSpan > 1)))?.cells || [])];
  const fix = (el, style) => Object.entries(style).forEach(([property, value]) => el.style.setProperty(property, value, 'important'));
  const laidOut = (region, table, stage) => {
    const place = [...region.querySelectorAll('table')].indexOf(table);
    const copy = region.cloneNode(true);
    copy.setAttribute('aria-hidden', 'true');
    copy.querySelectorAll('[id]').forEach((el) => el.removeAttribute('id'));
    fix(copy, {
      position: 'absolute', 'inset-inline-start': '-100000px', 'inset-block-start': '0', visibility: 'hidden',
      'box-sizing': 'content-box', 'inline-size': `${stage}px`, 'min-inline-size': '0', 'max-inline-size': 'none',
    });
    const laid = copy.querySelectorAll('table')[place];
    fix(laid, { 'table-layout': 'auto', 'inline-size': 'min-content', 'min-inline-size': '0', 'max-inline-size': 'none' });
    region.after(copy);
    try {
      return across(laid).map((cell) => (cell.checkVisibility() ? width(cell) : null));
    } finally {
      copy.remove();
    }
  };
  return [...document.querySelectorAll('main .table-scroll table[data-narrow-at]')]
    .filter((table) => table.checkVisibility() && !table.parentElement.closest('table[data-narrow-at]'))
    .map((table) => {
      const region = table.closest('.table-scroll');
      const heads = across(table);
      const leaves = [...new Set(heads.map((cell) => Number(cell.dataset.dropAt) || 0).filter(Boolean))];
      const stages = (leaves.length ? leaves : [Number(table.dataset.narrowAt)]).sort((a, b) => b - a);
      return {
        label: region.getAttribute('aria-label') || '',
        html: table.outerHTML,
        names: heads.map((cell) => name(cell) || '(unnamed)'),
        stages: stages.map((stage) => ({ width: stage, least: laidOut(region, table, stage) })),
      };
    });
}"""
)

# How far, in CSS pixels, a floor may stand above its column's min-content
# before the column counts as leaving early.
MARGIN = 0


def judged(tables: list[dict]) -> tuple[list[str], list[str]]:
    """Each table's columns at each width it measured, each column's floor
    there beside its min-content, and what is wrong: a floor below its
    min-content, where the table overflows before the column leaves, or more
    than ``MARGIN`` above it, where the column leaves while it still fits."""

    said, wrong = [], []
    for place, table in enumerate(tables, 1):
        columns = fit_columns(table["html"])[0]
        title = f'table {place} "{table["label"]}"'
        for stage in table["stages"]:
            width = stage["width"]
            least = {index: need for index, need in enumerate(stage["least"]) if need is not None}
            floors = floors_at(columns, set(least) & set(range(len(columns))), width)
            shown = []
            for index, floor in floors.items():
                name, need = table["names"][index], least[index]
                shown.append(f"{name} {floor}/{need}")
                if floor < need:
                    wrong.append(f"{title} at {width}: {name} floor {floor} < min-content {need}, overflows")
                elif floor > need + MARGIN:
                    wrong.append(
                        f"{title} at {width}: {name} floor {floor} > min-content {need} + {MARGIN}, leaves early"
                    )
            said.append(f"{title} at {width}: {' · '.join(shown)}")
    return said, wrong


def measured(page) -> list[dict]:
    return page.evaluate(_MEASURE)
