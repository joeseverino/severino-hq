"""Probes of a page's parts: disclosures, labels, the dashboard's readings, drawings and a check's evidence."""

from hq.platform.core.included_parts import CHART, MODAL
from hq.platform.core.templatetags.ladder_tags import CHECK_RUNG
from hq.platform.core.tests.browser.probes import Check, Example, Plant
from hq.platform.core.tests.browser.probes.describe import DESCRIBE
from hq.platform.core.tests.browser.selectors import SELECTORS

# Press a heading's sort control and read its column back, top to bottom.
SPILLED_LABELS = (
    "(selector) => {"
    + DESCRIBE
    + """
  // A control is at least as wide as what it says. One sized for a glyph and
  // given a word draws the word over whatever stands beside it. One with no
  // words has no label to spill. The label is what the control lays out in
  // flow: its words and boxes, and what those spill in turn. A box placed
  // out of flow (the hit area hung beside a small control) is no label,
  // though it widens the control's scrollable overflow.
  const found = [];
  const words = document.createRange();
  const reach = (el) => {
    let right = -Infinity;
    const take = (r) => { if (r.width > 0) right = Math.max(right, r.right); };
    const text = (parent) => {
      for (const node of parent.childNodes) {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          words.selectNodeContents(node);
          [...words.getClientRects()].forEach(take);
        } else if (node.nodeType === Node.ELEMENT_NODE && getComputedStyle(node).display === 'contents') {
          text(node);
        }
      }
    };
    const walk = (parent) => {
      text(parent);
      for (const [child, style, r] of inFlow(parent)) {
        take(r);
        if (style.overflowX === 'visible') walk(child);
      }
    };
    walk(el);
    return right;
  };
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility() || !el.textContent.trim()) continue;
    const style = getComputedStyle(el);
    // A box whose content the browser skips (`hidden="until-found"`) draws
    // no label until it is found.
    if (style.overflowX !== 'visible' || style.display === 'inline' || style.contentVisibility === 'hidden') continue;
    const start = el.getBoundingClientRect().left + parseFloat(style.borderLeftWidth);
    const held = Math.round(reach(el) - start + parseFloat(style.paddingRight));
    if (held > el.clientWidth + 1) {
      found.push(`${describe(el)} holds ${held}px of label in ${el.clientWidth}px`);
    }
  }
  return found;
}"""
)


# An ask drawn beside a table whose rows hold the same ask: the row's button
# again, outside every row, which reads as a control nothing in the table
# owns.
ASKS_OUTSIDE_THEIR_ROWS = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  const asked = (button) => button.getAttribute('formaction');
  for (const table of document.querySelectorAll('table')) {
    const rows = new Set([...table.querySelectorAll('tbody [data-ask] [data-ask-button]')].map(asked));
    if (!rows.size) continue;
    const scope = table.closest('section, main') ?? document.body;
    for (const button of scope.querySelectorAll('[data-ask] [data-ask-button]')) {
      if (button.closest('table') || !rows.has(asked(button)) || !button.checkVisibility()) continue;
      found.push(`${describe(button)} asks what a row of the table beside it asks, outside every row`);
    }
  }
  return [...new Set(found)];
}"""
)


# A heading in a run of columns starts the column that holds what it heads:
# the first words after its head are drawn below it, never at the top of the
# next column while the head ends the one before. Its head is the box the
# heading stands in with what sits beside it (a section's head, a summary).
HEADINGS_PARTED = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  const range = document.createRange();
  const columned = [...document.querySelectorAll('main *')].filter((el) => {
    const style = getComputedStyle(el);
    return style.columnCount !== 'auto' || style.columnWidth !== 'auto';
  });
  for (const flow of columned) {
    for (const heading of flow.querySelectorAll('h1, h2, h3, h4, h5, h6')) {
      if (!heading.checkVisibility()) continue;
      const head = heading.closest('header, summary, figcaption, hgroup') ?? heading;
      const h = box(head);
      const walker = document.createTreeWalker(flow, NodeFilter.SHOW_TEXT);
      walker.currentNode = head;
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (head.contains(node) || !node.data.trim() || !node.parentElement.checkVisibility()) continue;
        range.selectNodeContents(node);
        const first = [...range.getClientRects()].find((r) => r.width && r.height);
        if (!first) continue;
        if (first.top < h.bottom - 1) {
          found.push(`${describe(heading)} "${heading.textContent.trim().replace(/\\s+/g, ' ')}" ends one column; "${node.data.trim().slice(0, 30)}" under it starts another`);
        }
        break;
      }
    }
  }
  return found;
}"""
)


# A check's evidence follows its statement: on the same line within a short
# gap, or on the next line starting where the statement starts.
STRANDED_EVIDENCE = (
    "([statement, evidence]) => {"
    + DESCRIBE
    + """
  const found = [];
  let read = 0;
  for (const el of document.querySelectorAll('main ' + evidence)) {
    const said = el.previousElementSibling;
    if (!el.checkVisibility() || !said || !said.matches(statement)) continue;
    read += 1;
    const e = box(el);
    const s = box(said);
    const besideIt = e.top < s.bottom - 1;
    if (besideIt ? e.left - s.right > 48 : Math.abs(e.left - s.left) > 2) {
      found.push(`${describe(el)}: ${Math.round(e.left - (besideIt ? s.right : s.left))}px from its statement`);
    }
  }
  return {read, found};
}"""
)

# What a disclosure's handle names is shown wherever the handle is.
HIDDEN_IN_SUMMARIES = (
    "(value) => {"
    + DESCRIBE
    + """
  return [...document.querySelectorAll(value)]
    .filter((el) => el.closest('summary').checkVisibility() && !el.checkVisibility())
    .map(describe);
}"""
)


# A link among words differs from them by more than its color (WCAG 1.4.1):
# it is underlined at rest, or set in a weight, size, style or face of its
# own, the cues axe's link-in-text-block accepts. A link that is the whole of
# its line, row, item or chip is never underlined at rest. The words around a
# link are the text its nearest box lays out in line with it, outside every
# link and up to a forced break; a version or a count alone is no words, nor
# is a box inside that line (a chip, a badge), and a link laid out as a block
# stands alone unless it is an item in a row of words. Read from the rendered
# page, so every link on every page is judged at every width, whatever markup
# or class drew it.
LINKS_AT_REST = """([links, vendored]) => {
  const style = (el) => getComputedStyle(el);
  const atomic = (el) => !['inline', 'contents'].includes(style(el).display);
  const inLine = (el) => /inline/.test(style(el).display);
  const row = (el) => /flex|grid/.test(style(el).display);
  const shown = (node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return [...range.getClientRects()].some((r) => r.width > 1 && r.height > 1);
  };
  const lettered = (node) => /\\p{L}/u.test(node.textContent) && shown(node);
  const among = (link, box) => {
    const inBox = (el) => {
      while (el !== box && !atomic(el)) el = el.parentElement;
      return el === box;
    };
    const walker = document.createTreeWalker(box, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let line = 0;
    let own = -1;
    const worded = new Map();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node === link) own = line;
      else if (node.nodeType === Node.ELEMENT_NODE && node.localName === 'br' && inBox(node.parentElement)) line += 1;
      else if (node.nodeType === Node.TEXT_NODE && !node.parentElement.closest(links) && inBox(node.parentElement) && lettered(node)) {
        worded.set(line, worded.get(line) || node.parentElement);
      }
    }
    return worded.get(own);
  };
  const face = (el) => ['font-weight', 'font-size', 'font-style', 'font-family'].map((p) => style(el).getPropertyValue(p)).join();
  const ownText = (link) => {
    const walker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) if (lettered(node)) return node.parentElement;
    return link;
  };
  const found = [];
  for (const link of document.querySelectorAll(links)) {
    if ((vendored && link.closest(vendored)) || !link.textContent.trim() || !shown(link)) continue;
    if (link.matches(':hover, :focus-visible')) continue;
    let box = link.parentElement;
    while (box && !atomic(box)) box = box.parentElement;
    const words = box && (inLine(link) || (link.parentElement === box && row(box))) && among(link, box);
    const underlined = style(link).textDecorationLine.includes('underline');
    const name = link.textContent.trim().replace(/\\s+/g, ' ').slice(0, 60);
    const where = box ? [box.localName, ...box.classList].join('.') : 'the page';
    if (words && !underlined && face(ownText(link)) === face(words)) {
      found.push(`link ${name} sits among words in ${where} and differs from them only by its color`);
    }
    if (!words && underlined) found.push(`link ${name} stands alone in ${where} and is underlined at rest`);
  }
  return found;
}"""

# A dialog a page declares is one a reader can open: some control on the page
# names it, so drawing a dialog without its control is a finding.
DIALOGS_WITHOUT_OPENERS = """([dialogs, openers]) => {
  const opened = (dialog) => document.querySelector(openers.replaceAll('{id}', dialog.id.slice('modal-'.length)));
  const read = [...document.querySelectorAll(dialogs)];
  const found = read.filter((dialog) => !opened(dialog)).map((dialog) => `dialog #${dialog.id} has no control that opens it`);
  return {read: read.length, found};
}"""

# How many lines an element's words run over: the runs of its text that share
# no height, and where the first starts and the last ends. Its text alone: a
# range over the element would also give the box of each element inside it,
# and a list's box spans every line its names wrap to.
LINE_COUNT = """
const lineRange = document.createRange();
const linesOf = (el) => {
  const drawn = [];
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    lineRange.selectNodeContents(node);
    drawn.push(...lineRange.getClientRects());
  }
  const rects = drawn.filter((r) => r.width && r.height).sort((a, b) => a.top - b.top);
  if (!rects.length) return {lines: 0, top: 0, bottom: 0};
  let lines = 1;
  let bottom = rects[0].bottom;
  for (const r of rects.slice(1)) {
    if (r.top >= bottom - 2) {
      lines += 1;
      bottom = r.bottom;
    } else bottom = Math.max(bottom, r.bottom);
  }
  return {lines, top: rects[0].top, bottom: Math.max(...rects.map((r) => r.bottom))};
};
"""

# What keeps its line wherever it is drawn: a count beside a legend.
WRAPPED = (
    "(selector) => {"
    + DESCRIBE
    + LINE_COUNT
    + """
  return [...document.querySelectorAll(selector)]
    .filter((el) => el.checkVisibility() && linesOf(el).lines > 1)
    .map((el) => `${describe(el)} "${el.textContent.trim().replace(/\\s+/g, ' ')}" runs over ${linesOf(el).lines} lines`);
}"""
)


# A pill is one box: drawn in pieces, one a line, a sentence it holds reads
# as two pills, each a state of its own.
PILLS_IN_PIECES = (
    "() => {"
    + DESCRIBE
    + """
  return [...document.querySelectorAll('main .pill')]
    .map((pill) => [pill, [...pill.getClientRects()].filter((r) => r.width >= 1 && r.height >= 1)])
    .filter(([pill, pieces]) => pieces.length > 1 && pill.checkVisibility())
    .map(([pill, pieces]) => `${describe(pill)} "${pill.textContent.trim().replace(/\\s+/g, ' ').slice(0, 32)}" is drawn as ${pieces.length} pills`);
}"""
)


# A chart, a trend, a bar or a calendar is drawn wherever what holds it is: a
# drawing hidden for want of room is a figure that shows nothing, when the
# table beside it or the line under it has the room to show it.
UNDRAWN = """(drawings) => {
  const read = [...document.querySelectorAll(drawings)];
  const found = read.filter((drawing) => {
    const held = drawing.parentElement.getBoundingClientRect();
    const drawn = drawing.getBoundingClientRect();
    return held.width > 0 && held.height > 0 && (drawn.width < 1 || drawn.height < 1);
  }).map((drawing) => drawing.outerHTML.slice(0, 120));
  return {read: read.length, found};
}"""

# Whether a box scrolls wider than it shows, which a plant's premise asks.
_WIDENED = """(box) => document.querySelector(box).scrollWidth > document.querySelector(box).clientWidth
  ? [] : [`${box} scrolls no wider than it shows`]"""

# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "control labels": Check(
        (SPILLED_LABELS, SELECTORS["control"]),
        reader_sees="a control's label spilling out of its box",
        # The hit area a glyph smaller than a target hangs beside itself
        # widens what it scrolls, never what it says; a word wider than its
        # control spills.
        plants=(
            Plant(
                '<button type="button" class="example-glyph" data-hit aria-label="Example favorite" '
                'style="inline-size: 1rem; block-size: 1rem; padding: 0">★</button> '
                '<button type="button" class="example-word" style="inline-size: 2rem; white-space: nowrap">'
                "example label</button>",
                premise=(_WIDENED, "main .example-glyph"),
                finds=("button.example-word",),
            ),
        ),
    ),
    "cell labels": Check(
        (SPILLED_LABELS, SELECTORS["cell_content"]), table=True, reader_sees="a cell's words spilling out of their box"
    ),
    "dialogs": Check(
        (DIALOGS_WITHOUT_OPENERS, [SELECTORS["dialog"], SELECTORS["dialog_opener"]]),
        reader_sees="a dialog nothing on the page opens",
        part=MODAL,
        # Each opener the modal contract allows: a link the page's wiring
        # intercepts, a control that names its dialog, and the platform's own
        # invoker, bare.
        clear=(
            Example(
                '<a href="/" data-modal-open="example-link">Link</a>'
                '{% include "partials/_modal.html" with id="example-link" title="Example" %}'
                '<a href="/" aria-haspopup="dialog" aria-controls="modal-example-named">Named</a>'
                '{% include "partials/_modal.html" with id="example-named" title="Example" %}'
                '<button type="button" command="show-modal" commandfor="modal-example-invoker">Invoker</button>'
                '{% include "partials/_modal.html" with id="example-invoker" title="Example" %}'
            ),
        ),
    ),
    "links at rest": Check(
        (LINKS_AT_REST, [SELECTORS["link"], SELECTORS["vendored"]]),
        reader_sees="a link among words told apart from them by color alone",
    ),
    "undrawn drawings": Check(
        (UNDRAWN, SELECTORS["drawing"]),
        reader_sees="a chart or a calendar given room and drawing nothing",
        part=CHART,
    ),
    "pills in pieces": Check(
        PILLS_IN_PIECES, table=True, reader_sees="one pill's words drawn as two pills, a line each"
    ),
    "headings parted": Check(HEADINGS_PARTED, reader_sees="a heading at a column's foot, what it heads in the next"),
    "asks outside their rows": Check(
        ASKS_OUTSIDE_THEIR_ROWS, reader_sees="a row's button drawn again beside its table, owned by no row"
    ),
    "evidence apart": Check(
        (STRANDED_EVIDENCE, [SELECTORS["check_statement"], SELECTORS["check_evidence"]]),
        reader_sees="a check's evidence drawn away from its statement",
        part=CHECK_RUNG,
    ),
    "summaries hiding": Check(
        (HIDDEN_IN_SUMMARIES, SELECTORS["summary_value"]), reader_sees="what a fold's handle names, hidden"
    ),
}
