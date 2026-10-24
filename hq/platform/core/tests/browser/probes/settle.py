"""Probes that bring a page to a known state: loaded, drawn, still, and put back as it was."""

from hq.platform.core.tests.browser.probes.describe import _DESCRIBE

_STYLESHEETS = """() => {
  const links = [...document.querySelectorAll('link[rel="stylesheet"]')];
  const loaded = [...document.styleSheets].filter((sheet) => sheet.href);
  return {links: links.length,
          empty: loaded.filter((sheet) => !sheet.cssRules.length).map((s) => s.href),
          loaded: loaded.length};
}"""

# Every style rule's selector the browser kept, nested rules included. A rule
# whose selector does not parse is dropped whole and silently: the stylesheet
# still loads and nothing reports it.
_KEPT_SELECTORS = """(directory) => {
  const sheets = [...document.styleSheets].filter((s) => s.href && new URL(s.href).pathname.includes(directory));
  const found = [];
  const walk = (rules) => {
    for (const rule of rules) {
      if (rule.selectorText !== undefined) found.push(rule.selectorText);
      if (rule.cssRules) walk(rule.cssRules);
    }
  };
  sheets.forEach((sheet) => walk(sheet.cssRules));
  return found;
}"""

# A frame has been drawn and nothing is moving.
_PAINTED = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"

_STILL = """() => document.getAnimations()
  .every((motion) => motion.playState !== 'running' || motion.effect?.getComputedTiming().iterations === Infinity)"""

_SETTLED = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"

# Every face the page asked for has arrived (see _LATE_FACE).
_FONTS_READY = "() => document.fonts.ready.then(() => document.fonts.status)"

# The page as the server drew it, with its scripts held back: parsed, styled
# and in its own face, which is what a reader is shown first. An image a sheet
# fetched would arrive after this, unwaited, so no sheet fetches one
# (test_a_sheet_fetches_nothing_but_its_faces).
_DRAWN = """() => document.readyState !== 'loading'
  && [...document.querySelectorAll('link[rel=stylesheet]')].every((link) => link.sheet)
  && document.fonts.status === 'loaded'"""

# The scripts the parser stops for, which are drawn before the page is.
_PARSER_SCRIPTS = "() => [...document.scripts].filter((s) => s.src && !s.defer && !s.async).map((s) => s.src)"

_IN_THE_HEAD = "() => !document.body"

# Where every element that shows stands on the page as first drawn, kept on
# the window for _MOVED_SINCE_DRAWN to read once the scripts have run.
_DRAWN_BOXES = """() => {
  const boxes = new Map();
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (!el.checkVisibility() || (!r.width && !r.height)) continue;
    boxes.set(el, [r.left + scrollX, r.top + scrollY, r.width, r.height]);
  }
  window.drawnBoxes = boxes;
  return boxes.size;
}"""

# One frame's look at a page being drawn: until it is, the scripts its parser
# waits for; once it is, where everything stands (null) in the next frame it
# paints. A ResizeObserver's first answer comes after layout and the page's
# own observers (tables.js fits a table in one), just before that paint.
_UNTIL_DRAWN = f"""() => {{
  if (!({_DRAWN})()) return ({_PARSER_SCRIPTS})();
  return new Promise((done) => {{
    const painting = new ResizeObserver(() => {{
      painting.disconnect();
      ({_DRAWN_BOXES})();
      done(null);
    }});
    painting.observe(document.documentElement);
  }});
}}"""

# Every element that still shows and no longer stands where it was drawn. A
# child carried by its parent, the same distance and at the same size, is the
# parent's move and is not named again.
_MOVED_SINCE_DRAWN = (
    "() => {"
    + _DESCRIBE
    + """
  const drawn = window.drawnBoxes;
  const now = (el) => { const r = el.getBoundingClientRect();
    return [r.left + scrollX, r.top + scrollY, r.width, r.height]; };
  const change = (el) => now(el).map((value, index) => value - drawn.get(el)[index]);
  const still = (moved) => moved.every((by) => Math.abs(by) < 0.5);
  const found = [];
  for (const [el, was] of drawn) {
    if (!el.isConnected || !el.checkVisibility()) continue;
    const moved = change(el);
    if (still(moved)) continue;
    const parent = el.parentElement;
    if (drawn.has(parent) && parent.checkVisibility() && still(moved.slice(2))) {
      const by = change(parent);
      if (still([moved[0] - by[0], moved[1] - by[1]])) continue;
    }
    const words = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 30);
    const at = (box) => box.map(Math.round).join(',');
    found.push(`${describe(el)} "${words}" ${at(was)} -> ${at(now(el))}`);
  }
  return found;
}"""
)

# Colors transition between palettes, so contrast is measured after the
# transition ends.
_FINISH_TRANSITIONS = """() => document.getAnimations().forEach((a) => { try { a.finish(); } catch { a.cancel(); } })"""

# The disclosures a page loads closed, and closing them again: a rule opens
# them to lay out what they hold, and the page read at its next width is the
# page as it loaded.
_KEEP_CLOSED = "() => { window.hqLoadedClosed = [...document.querySelectorAll('details:not([open])')]; }"

_CLOSE_AGAIN = "() => window.hqLoadedClosed.forEach((details) => { details.open = false; })"

# Press every one of these a reader can see: one drawn but not offered is not.
_PRESS_EVERY_SHOWN = "(controls) => controls.forEach((control) => control.checkVisibility() && control.click())"

# What a vendored script draws is not HQ's markup, so the layout rules judge
# the page with it taken out of the box HQ wrote for it, and an empty block as
# tall in its place: the box keeps its own rules and the page its height. The
# script's own tests hold what it draws. Put back by `_PUT_BACK`.
_STAND_IN = """(selector) => {
  window.hqStandIns = [...document.querySelectorAll(selector)].map((box) => {
    const stand = document.createElement('div');
    stand.style.blockSize = `${box.getBoundingClientRect().height}px`;
    const drawn = document.createDocumentFragment();
    drawn.append(...box.childNodes);
    box.append(stand);
    return [box, stand, drawn];
  });
}"""

_PUT_BACK = """() => (window.hqStandIns || []).forEach(([box, stand, drawn]) => {
  stand.remove();
  box.append(drawn);
})"""
