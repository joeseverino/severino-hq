"""Probes that bring a page to a known state: loaded, drawn, still, and put back as it was."""

STYLESHEETS = """() => {
  const links = [...document.querySelectorAll('link[rel="stylesheet"]')];
  const loaded = [...document.styleSheets].filter((sheet) => sheet.href);
  return {links: links.length,
          empty: loaded.filter((sheet) => !sheet.cssRules.length).map((s) => s.href),
          loaded: loaded.length};
}"""

# Every style rule's selector the browser kept, nested rules included. A rule
# whose selector does not parse is dropped whole and silently: the stylesheet
# still loads and nothing reports it.
KEPT_SELECTORS = """(directory) => {
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
PAINTED = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"

STILL = """() => document.getAnimations()
  .every((motion) => motion.playState !== 'running' || motion.effect?.getComputedTiming().iterations === Infinity)"""

SETTLE = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"

# Every face the page asked for has arrived (see _LATE_FACE).
FONTS_READY = "() => document.fonts.ready.then(() => document.fonts.status)"


# Colors transition between palettes, so contrast is measured after the
# transition ends.
FINISH_TRANSITIONS = """() => document.getAnimations().forEach((a) => { try { a.finish(); } catch { a.cancel(); } })"""

# The disclosures a page loads closed, and closing them again: a rule opens
# them to lay out what they hold, and the page read at its next width is the
# page as it loaded.
KEEP_CLOSED = "() => { window.hqLoadedClosed = [...document.querySelectorAll('details:not([open])')]; }"

CLOSE_AGAIN = "() => window.hqLoadedClosed.forEach((details) => { details.open = false; })"

# Press every one of these a reader can see: one drawn but not offered is not.
PRESS_EVERY_SHOWN = "(controls) => controls.forEach((control) => control.checkVisibility() && control.click())"

# What a vendored script draws is not HQ's markup, so the layout rules judge
# the page with it taken out of the box HQ wrote for it, and an empty block as
# tall in its place: the box keeps its own rules and the page its height. The
# script's own tests hold what it draws. Put back by `PUT_BACK`.
STAND_IN = """(selector) => {
  window.hqStandIns = [...document.querySelectorAll(selector)].map((box) => {
    const stand = document.createElement('div');
    stand.style.blockSize = `${box.getBoundingClientRect().height}px`;
    const drawn = document.createDocumentFragment();
    drawn.append(...box.childNodes);
    box.append(stand);
    return [box, stand, drawn];
  });
}"""

PUT_BACK = """() => (window.hqStandIns || []).forEach(([box, stand, drawn]) => {
  stand.remove();
  box.append(drawn);
})"""

# A probe measures some things on a copy it puts in place and takes away
# again (a pill's one-line twin, a field's edge, a stand-in). A page script
# that answers a change in its tree would reshape the page as it is read:
# tables.js fits a region again when what it holds changes. Every page's
# MutationObserver is made deaf while a trip of probes runs (`hushed`), so the
# probes judge the page as a reader has it. The probes still see their own
# changes, since only the page's observers miss the records.
_HUSH = "Symbol.for('hq.probing')"

DEAF_WHILE_PROBED = f"""(() => {{
  const Heard = window.MutationObserver;
  window.MutationObserver = class MutationObserver extends Heard {{
    constructor(callback) {{
      super((records, observer) => {{ if (!globalThis[{_HUSH}]) callback.call(observer, records, observer); }});
    }}
  }};
}})();"""


def hushed(trip):
    """``trip``, an async function of its args, run with the page's observers
    deaf. The records its probes make are delivered in the microtask after
    each change, so one more turn before the hush lifts lets the last go by."""

    return f"""async (args) => {{
  globalThis[{_HUSH}] = true;
  try {{ return await ({trip})(args); }}
  finally {{ await null; globalThis[{_HUSH}] = false; }}
}}"""
