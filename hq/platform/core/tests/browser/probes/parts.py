"""Probes of a page's parts: heads, links, disclosures, controls, routes and what a pressed control says."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE

HEAD_ON_ONE_LINE = """([leadSelector, titleSelector, headSelector]) => {
  // A narrow head is one line: the title and its lead control, nothing
  // between them and the lede. The control takes a row of its own only when
  // the title's widest word or name could not sit whole beside the controls.
  const lead = document.querySelector(leadSelector);
  const title = document.querySelector(titleSelector);
  if (!lead || !title) return null;
  const a = lead.getBoundingClientRect(), t = title.getBoundingClientRect();
  if (a.top < t.bottom && a.bottom > t.top) return true;
  const head = title.closest(headSelector);
  const style = getComputedStyle(head);
  const room = head.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const kept = title.getAttribute('style');
  title.style.flex = 'none';
  title.style.width = 'min-content';
  const narrowest = title.getBoundingClientRect().width;
  if (kept === null) title.removeAttribute('style'); else title.setAttribute('style', kept);
  return narrowest + parseFloat(style.columnGap) + lead.parentElement.getBoundingClientRect().width > room + 1;
}"""

# A link that sits above or below the words it continues: an inline line in a
# smaller size than its cell aligns a clamped link to the cell's taller line.
RAISED_LINKS = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  const rects = (node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return [...range.getClientRects()].filter((r) => r.width);
  };
  for (const link of document.querySelectorAll('main td [data-entity]')) {
    if (!link.checkVisibility() || !link.firstChild) continue;
    const before = link.previousSibling;
    if (!before || before.nodeType !== Node.TEXT_NODE || !before.textContent.trim()) continue;
    const words = rects(before).at(-1);
    // The link's own words, however deep: a box around them (a capped part)
    // also holds the line's leading, which is not where the words sit.
    // Walked without a filter callback, which an engine with script off
    // refuses to call back into.
    const walker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT);
    let text = walker.nextNode();
    while (text && !text.textContent.trim()) text = walker.nextNode();
    const linked = text && rects(text)[0];
    if (!words || !linked) continue;
    // Wrapped onto a line of its own: nothing beside it to sit off.
    if (linked.top >= words.bottom || linked.bottom <= words.top) continue;
    if (Math.abs(words.bottom - linked.bottom) > 1.5) {
      found.push(`${describe(link)} sits ${Math.round(words.bottom - linked.bottom)}px off the words before it`);
    }
  }
  return [...new Set(found)];
}"""
)

# An external link's arrow on a line of its own, below every word of the
# link it marks: the link's last line box holds nothing but the arrow.
LONE_ARROWS = (
    "(selector) => {"
    + DESCRIBE
    + """
  const found = [];
  const range = document.createRange();
  for (const link of document.querySelectorAll('main ' + selector)) {
    if (!link.checkVisibility()) continue;
    const lines = [...link.getClientRects()].filter((r) => r.width);
    if (lines.length < 2) continue;
    range.selectNodeContents(link);
    const words = [...range.getClientRects()].filter((r) => r.width);
    if (!words.length) continue;
    if (lines.at(-1).top >= Math.max(...words.map((r) => r.bottom)) - 1) {
      found.push(`${describe(link)} leaves its arrow on a line of its own: ${link.textContent.trim().slice(0, 40)}`);
    }
  }
  return found;
}"""
)

# A summary says it opens with one mark: the disclosure primitive's
# (surfaces.css), or its own where its details opts out of that one. A second
# reads as a second control. A mark is the browser's own marker, or a glyph a
# pseudo-element draws on the summary or anything in it.
SUMMARY_MARKS = (
    "() => {"
    + DESCRIBE
    + r"""
  const found = [];
  const glyph = (el, pseudo) => {
    const match = /^"([^"]*)"$/.exec(getComputedStyle(el, pseudo).content);
    return match && /^\s*[▸▹▶►▾▿▼⌄⌃›»+−＋]\s*$/u.test(match[1]) ? match[1].trim() : '';
  };
  for (const summary of document.querySelectorAll('main summary')) {
    if (!summary.checkVisibility()) continue;
    const style = getComputedStyle(summary);
    const marks = style.display === 'list-item' && style.listStyleType !== 'none' ? ['its marker'] : [];
    for (const el of [summary, ...summary.querySelectorAll('*')]) {
      for (const pseudo of ['::before', '::after']) {
        const mark = glyph(el, pseudo);
        if (mark) marks.push(mark);
      }
    }
    if (marks.length > 1) found.push(`${describe(summary)} "${summary.textContent.trim().slice(0, 30)}" draws ${marks.join(' and ')}`);
  }
  return found;
}"""
)

# A row has one disclosure. The row toggle shows what a row holds back; a
# dropdown inside the row beside it is a second, smaller way to do the same.
NESTED_DISCLOSURES = (
    "() => {"
    + DESCRIBE
    + """
  return [...document.querySelectorAll('main table > tbody > tr')]
    .filter((row) => row.querySelector('button[aria-expanded]') && row.querySelector(':is(td, th) details'))
    .map((row) => `${describe(row)} has a row toggle and a dropdown`);
}"""
)

# Opening a disclosure adds what it holds after its summary and moves nothing
# before it: the summary pressed stays under the pointer that pressed it.
MOVING_SUMMARIES = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  for (const details of document.querySelectorAll('main details:not([open])')) {
    const summary = details.querySelector(':scope > summary');
    if (!summary || !summary.checkVisibility()) continue;
    const before = box(summary);
    details.open = true;
    const after = box(summary);
    details.open = false;
    if (Math.abs(after.top - before.top) > 1 || Math.abs(after.left - before.left) > 1) {
      found.push(`${describe(details)}: "${summary.textContent.trim().slice(0, 30)}" moved ${Math.round(after.left - before.left)}, ${Math.round(after.top - before.top)} when opened`);
    }
  }
  return found;
}"""
)

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
    if (style.overflowX !== 'visible' || style.display === 'inline') continue;
    const start = el.getBoundingClientRect().left + parseFloat(style.borderLeftWidth);
    const held = Math.round(reach(el) - start + parseFloat(style.paddingRight));
    if (held > el.clientWidth + 1) {
      found.push(`${describe(el)} holds ${held}px of label in ${el.clientWidth}px`);
    }
  }
  return found;
}"""
)

MOVED_BY_SPEAKING = (
    "async ([said, spoken]) => {"
    + DESCRIBE
    + """
  // Pressing a control moves nothing. What it says about the work it asked
  // for is drawn over the page, so every other box is where it was, the page
  // is the size it was, and the words themselves are on the screen.
  const asks = [...document.querySelectorAll('[data-ask]:has([data-ask-note])')].filter((ask) => ask.checkVisibility());
  if (!asks.length) return null;
  // A note already showing is one of the things being made to speak.
  const others = [...document.querySelectorAll('body *')]
    .filter((el) => el.checkVisibility() && !el.closest(spoken) && !el.closest('[data-ask-note]'));
  const page = document.documentElement;
  const size = () => [page.scrollWidth, page.scrollHeight];
  const place = (el) => { const r = box(el); return [r.left, r.top, r.width, r.height].map(Math.round).join(' '); };
  const before = others.map(place);
  const was = size();
  // Each control as it was drawn, to put back once it has been measured.
  const drawn = asks.map((ask) => [ask, ask.dataset.askState,
    ...[...ask.querySelectorAll('[data-ask-note], [data-ask-elapsed]')].map((el) => [el, el.hidden, [...el.childNodes]])]);
  for (const ask of asks) {
    ask.dataset.askState = 'running';
    ask.querySelector('[data-ask-note]').textContent = said;
    ask.querySelector('[data-ask-note]').hidden = false;
    const elapsed = ask.querySelector('[data-ask-elapsed]');
    if (elapsed) elapsed.textContent = '7 s';
  }
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const found = [];
  others.forEach((el, index) => {
    if (place(el) !== before[index]) found.push(`${describe(el)} moved from ${before[index]} to ${place(el)}`);
  });
  if (String(size()) !== String(was)) found.push(`the page went from ${was} to ${size()}`);
  for (const ask of asks) {
    const note = ask.querySelector(spoken) || ask.querySelector('[data-ask-note]');
    const r = box(note);
    // Inside a box that scrolls, it goes where its button goes.
    let held = false;
    for (let up = ask.parentElement; up && up !== document.body; up = up.parentElement) {
      if (scrolls(up)) { held = true; break; }
    }
    if (r.width < 2) found.push(`${describe(ask)} says nothing that shows`);
    else if (!held && (r.left < -1 || r.right > page.clientWidth + 1)) {
      found.push(`${describe(note)} spans ${Math.round(r.left)}..${Math.round(r.right)}px`);
    }
  }
  // Back as it loaded, so the next probe reads the page the server drew.
  for (const [ask, state, ...parts] of drawn) {
    if (state === undefined) delete ask.dataset.askState; else ask.dataset.askState = state;
    for (const [el, hidden, nodes] of parts) { el.replaceChildren(...nodes); el.hidden = hidden; }
  }
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  return found;
}"""
)

# Each reading's mark and lines, as laid out: where the mark sits, and for each
# line whether it starts beside the mark, whether it is cut short, and where
# its top is.
READINGS = """(heads) => heads.map((head) => {
  const [mark, ...lines] = [...head.children].filter((el) => el.checkVisibility());
  const at = mark.getBoundingClientRect();
  return {
    top: Math.round(head.getBoundingClientRect().top),
    mark: Math.round((at.top + at.bottom) / 2),
    lead: (() => { const r = lines[0].getBoundingClientRect(); return Math.round((r.top + r.bottom) / 2); })(),
    under_the_mark: lines.filter((line) => line.getBoundingClientRect().left < at.right - 1)
      .map((line) => line.textContent.trim().slice(0, 30)),
    cut: lines.filter((line) => line.scrollWidth > line.clientWidth + 1)
      .map((line) => line.textContent.trim().slice(0, 30)),
    second: lines[1] ? Math.round(lines[1].getBoundingClientRect().top - head.getBoundingClientRect().top) : null,
    size: getComputedStyle(lines[0].querySelector('strong') || lines[0]).fontSize,
  };
})"""


def reading_problems(readings):
    """What breaks the readings' one shape: a line under the mark or cut short,
    a mark off its lead, leads of two sizes, rows of different counts, second
    lines at different depths."""

    found: list[str] = []
    for reading in readings:
        found.extend(f"under the mark: {line}" for line in reading["under_the_mark"])
        found.extend(f"cut short: {line}" for line in reading["cut"])
        if abs(reading["mark"] - reading["lead"]) > 3:
            found.append(f"mark {reading['mark']} beside a lead at {reading['lead']}")
    if len({reading["size"] for reading in readings}) > 1:
        found.append(f"leads in {sorted({reading['size'] for reading in readings})}")
    rows: dict[int, list[dict]] = {}
    for reading in readings:
        rows.setdefault(round(reading["top"]), []).append(reading)
    if len({len(row) for row in rows.values()}) > 1:
        found.append(f"rows of {[len(row) for row in rows.values()]}")
    if len({reading["second"] for reading in readings if reading["second"]}) > 1:
        found.append(f"second lines at {sorted({r['second'] for r in readings if r['second']})}")
    return found


# One-line controls side by side are one height: a select beside a field
# beside a date, in one row of a form or of a bar. A control in a field is in
# the row its field is; any other is in the row its own top is.
UNEVEN_CONTROL_ROWS = (
    "([control, field]) => {"
    + DESCRIBE
    + """
  const rows = new Map();
  for (const el of document.querySelectorAll('main ' + control)) {
    if (!el.checkVisibility()) continue;
    const top = Math.round(box(el.closest(field) || el).top);
    const at = el.closest(field)?.parentElement || el.parentElement;
    const key = describe(at) + '@' + top;
    rows.set(key, [...(rows.get(key) || []), el]);
  }
  const found = [];
  for (const [key, controls] of rows) {
    const heights = controls.map((el) => Math.round(box(el).height));
    if (Math.max(...heights) - Math.min(...heights) > 1) {
      found.push(`${key}: ${controls.map((el, i) => describe(el) + ' ' + heights[i] + 'px').join(', ')}`);
    }
  }
  return found;
}"""
)

# A bar of controls that wraps is whole rows: each row it wraps to reaches
# its far edge, rather than a ragged run of controls of their own widths.
RAGGED_BARS = (
    "(bar) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + bar)) {
    if (!el.checkVisibility()) continue;
    // Controls only: a count at the end of a bar is words, which keep their own width.
    const items = [...el.children].filter((child) => child.tagName !== 'P' && child.checkVisibility() && getComputedStyle(child).position !== 'absolute');
    const rows = new Map();
    for (const item of items) {
      const top = Math.round(box(item).top);
      rows.set(top, Math.max(rows.get(top) || 0, box(item).right));
    }
    if (rows.size < 2) continue;
    const style = getComputedStyle(el);
    const edge = box(el).right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    const short = [...rows.values()].filter((end) => edge - end > 1);
    if (short.length) found.push(`${describe(el)}: ${short.length} of ${rows.size} rows stop short, by ${short.map((end) => Math.round(edge - end)).join(', ')}px`);
  }
  return found;
}"""
)

# A route is one row across its box or one column down it, never a row that
# wrapped with its line running off the edge.
WRAPPED_ROUTES = (
    "(route) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + route)) {
    const hops = [...el.children].filter((hop) => hop.checkVisibility()).map(box);
    if (hops.length < 2) continue;
    const tops = new Set(hops.map((hop) => Math.round(hop.top)));
    const lefts = new Set(hops.map((hop) => Math.round(hop.left)));
    if (tops.size > 1 && lefts.size > 1) found.push(`${describe(el)}: ${tops.size} rows of hops`);
  }
  return found;
}"""
)

# A route across its box spaces its icons evenly: no gap between two hops'
# icons is a pixel longer than another.
UNEVEN_ROUTES = (
    "([route, icon]) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + route)) {
    const icons = [...el.querySelectorAll(icon)].filter((mark) => mark.checkVisibility()).map(box);
    if (icons.length < 3 || new Set(icons.map((mark) => Math.round(mark.top))).size > 1) continue;
    const centers = icons.map((mark) => mark.left + mark.width / 2);
    const gaps = centers.slice(1).map((center, i) => center - centers[i]);
    if (Math.max(...gaps) - Math.min(...gaps) > 1) found.push(`${describe(el)}: gaps ${gaps.map(Math.round).join(', ')}`);
  }
  return found;
}"""
)

# The dashboard's queue cards end their buttons at one edge: no card's end
# stops more than a pixel short of another's. The full queue is a table, whose
# columns hold its rows to one edge.
# A route's line runs from the center of its first stop's mark to the center
# of its last, opened or closed: each stop draws its stretch as `::before`,
# read back from its computed box.
ROUTE_LINE_ENDS = (
    "([route, mark]) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('main ' + route)) {
    const stops = [...el.children].filter((stop) => stop.checkVisibility());
    if (stops.length < 2) continue;
    const stretches = stops.flatMap((stop) => {
      const line = getComputedStyle(stop, '::before');
      if (line.display === 'none' || ['none', 'normal'].includes(line.content)) return [];
      const at = box(stop);
      return [{top: at.top + parseFloat(line.top), left: at.left + parseFloat(line.left),
               width: parseFloat(line.width), height: parseFloat(line.height)}];
    });
    if (!stretches.length) { found.push(`${describe(el)}: no line`); continue; }
    const down = stretches[0].height > stretches[0].width;
    const center = (stop) => { const at = box(stop.querySelector(mark));
      return down ? at.top + at.height / 2 : at.left + at.width / 2; };
    const start = Math.min(...stretches.map((s) => down ? s.top : s.left));
    const end = Math.max(...stretches.map((s) => down ? s.top + s.height : s.left + s.width));
    const first = center(stops[0]);
    const last = center(stops.at(-1));
    if (Math.abs(start - first) > 2 || Math.abs(end - last) > 2) {
      found.push(`${describe(el)}: line ${Math.round(start)} to ${Math.round(end)}, marks ${Math.round(first)} to ${Math.round(last)}`);
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
  for (const el of document.querySelectorAll('main ' + evidence)) {
    const said = el.previousElementSibling;
    if (!el.checkVisibility() || !said || !said.matches(statement)) continue;
    const e = box(el);
    const s = box(said);
    const besideIt = e.top < s.bottom - 1;
    if (besideIt ? e.left - s.right > 48 : Math.abs(e.left - s.left) > 2) {
      found.push(`${describe(el)}: ${Math.round(e.left - (besideIt ? s.right : s.left))}px from its statement`);
    }
  }
  return found;
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

# A section of queue cards ends every card's buttons at one edge: no card's
# end stops more than a pixel short of another's in the same section.
RAGGED_QUEUES = (
    "([card, end]) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const list of document.querySelectorAll('main section:has(' + card + ')')) {
    const ends = [...list.querySelectorAll(card + ' > ' + end)]
      .filter((el) => el.checkVisibility()).map((el) => Math.round(box(el).right));
    if (ends.length > 1 && Math.max(...ends) - Math.min(...ends) > 1) found.push(`${describe(list)}: ends at ${[...new Set(ends)].join(', ')}`);
  }
  return found;
}"""
)
