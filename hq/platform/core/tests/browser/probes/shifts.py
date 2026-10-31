"""The probe that scores how far a page moved by itself since it began to load."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE

# The most a page may shift by itself: Core Web Vitals' line for a good
# Cumulative Layout Shift.
CLS_LIMIT = 0.1

# The page's Cumulative Layout Shift as Core Web Vitals scores it, from the
# browser's own layout-shift entries: shifts no input of the reader's explains,
# grouped into sessions of shifts each under a second from the last and five
# seconds from the first, the largest session's sum the score. A score past
# `limit` is a finding naming every shift in that session. What a vendored
# script moves in the box HQ gave it (`vendored`) is the script's own: a part
# inside that box, or no longer in the page (the gates stand in for its markup
# while they read) and moved within the box's bounds, is left out. A page loads
# at its top, so a shift's rect, taken in the viewport as it happened, is the
# page's. A browser that keeps no such record (WebKit) has nothing to say;
# Chromium's legs of the gates read it.
LAYOUT_SHIFTS = (
    "([vendored, limit]) => new Promise((done) => {"
    + DESCRIBE
    + """
  if (!PerformanceObserver.supportedEntryTypes.includes('layout-shift')) { done([]); return; }
  const boxes = [...document.querySelectorAll(vendored)].map((el) => {
    const r = el.getBoundingClientRect();
    return [el, r.left + scrollX, r.top + scrollY, r.right + scrollX, r.bottom + scrollY];
  });
  const within = (rect, [, left, top, right, bottom]) => rect.left >= left - 1 && rect.top >= top - 1
    && rect.right <= right + 1 && rect.bottom <= bottom + 1;
  const theScripts = (part) => boxes.some((box) => (part.node?.isConnected
    ? box[0].contains(part.node) : within(part.currentRect, box)));
  const at = (rect) => [rect.x, rect.y, rect.width, rect.height].map(Math.round).join(',');
  const named = (node) => {
    if (!node?.isConnected) return 'a node since removed';
    const el = node instanceof Element ? node : node.parentElement;
    const words = (node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 30);
    return `${describe(el)} "${words}"`;
  };
  const said = (shift) => `${shift.value.toFixed(4)} at ${Math.round(shift.startTime)} ms: `
    + shift.sources.map((part) => `${named(part.node)} ${at(part.previousRect)} -> ${at(part.currentRect)}`).join('; ');
  const sum = (shifts) => shifts.reduce((total, shift) => total + shift.value, 0);
  const report = (entries) => {
    let worst = [], session = [];
    for (const shift of entries.filter((s) => !s.hadRecentInput && !s.sources.every(theScripts))) {
      const [first, last] = [session[0], session.at(-1)];
      const joins = first && shift.startTime - last.startTime < 1000 && shift.startTime - first.startTime < 5000;
      session = joins ? [...session, shift] : [shift];
      if (sum(session) > sum(worst)) worst = session;
    }
    const score = sum(worst);
    return score > limit ? [`layout shift ${score.toFixed(4)} past ${limit}: ${worst.map(said).join(' | ')}`] : [];
  };
  const seen = [];
  const observer = new PerformanceObserver((list) => seen.push(...list.getEntries()));
  observer.observe({type: 'layout-shift', buffered: true});
  // What the browser kept from before is handed over in a task of its own.
  setTimeout(() => {
    seen.push(...observer.takeRecords());
    observer.disconnect();
    done(report(seen));
  }, 0);
})"""
)
