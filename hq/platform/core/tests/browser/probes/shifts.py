"""The probe that reads what moved on a page by itself since it began to load."""

from hq.platform.core.tests.browser.probes.describe import _DESCRIBE

# Every layout shift the browser recorded on this page that no input of the
# reader's explains, each with its score and the boxes it moved: whatever
# moves a page once it is drawn, read from the browser's own record rather
# than from a list of what might. What a vendored script moves in the box HQ
# gave it (`vendored`) is the script's own: a part inside that box, or no
# longer in the page (the gates stand in for its markup while they read) and
# moved within the box's bounds, is left out. A page loads at its top, so a
# shift's rect, taken in the viewport as it happened, is the page's. A browser
# that keeps no such record (WebKit) has nothing to say; Chromium's leg of
# the gates reads it.
_LAYOUT_SHIFTS = (
    "(vendored) => new Promise((done) => {"
    + _DESCRIBE
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
  const report = (entries) => entries
    .filter((shift) => !shift.hadRecentInput && !shift.sources.every(theScripts))
    .map((shift) => {
      const moved = shift.sources.map((part) => `${named(part.node)} ${at(part.previousRect)} -> ${at(part.currentRect)}`);
      return `shifted ${shift.value.toFixed(4)} at ${Math.round(shift.startTime)} ms: ${moved.join('; ')}`;
    });
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

# Where each part of the site header that shows stands, by its class, and the
# header's own box: read on a page cut short inside the header and on the
# whole page, so a part that moves once its later siblings arrive is found.
_HEADER_PARTS = """(selector) => {
  const header = document.querySelector(selector);
  const at = (el) => { const r = el.getBoundingClientRect(); return [r.x, r.y, r.width, r.height].map(Math.round); };
  const parts = {header: at(header)};
  for (const part of header.children) {
    if (part.checkVisibility()) parts[part.className] = at(part);
  }
  return parts;
}"""
