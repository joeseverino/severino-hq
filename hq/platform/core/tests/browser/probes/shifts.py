"""The probe that scores how far a page moved by itself since it began to load."""

import json

from hq.platform.core.tests.browser.probes.describe import DESCRIBE
from hq.platform.core.tests.browser.selectors import SELECTORS

# The most a page may shift by itself: Core Web Vitals' line for a good
# Cumulative Layout Shift.
CLS_LIMIT = 0.1

# The page's Cumulative Layout Shift as Core Web Vitals scores it, from the
# browser's own layout-shift entries: shifts no input of the reader's explains,
# grouped into sessions of shifts each under a second from the last and five
# seconds from the first, the largest session's sum the score. A score past
# `limit` is a finding naming every shift in that session. What a vendored
# script moves in the box HQ gave it (`vendored`) is the script's own and left
# out. A part still in the page is judged by what holds it, so HQ's own part
# drawn over the box (a loading line sharing the mount's grid cell) stays HQ's.
# A part no longer in the page (a script's skeleton it replaced, or its markup
# the gates stand in for while they read) has only its rects to go by: it is
# the script's when it met the box before the shift or after it, since a script
# may draw a part past its box's edge, or carry it out, and then remove it. A
# page loads at its top, so a shift's rect, taken in the viewport as it
# happened, is the page's. A browser that keeps no such record (WebKit) has
# nothing to say; Chromium's legs of the gates read it. A shift until the
# frame after the page's face arrived is the face's: the gates wait for it
# (`serving.in_the_face`) where a reader's browser draws it from the first paint
# or never, so that move is the gate's own and left out. The record is read at
# once: with script off Chromium runs no timer and calls back no observer, so a
# probe that waits on either never returns.
#
# Run alone (`dev:look --probe shifts:LAYOUT_SHIFTS`, no `--arg`), it takes the
# survey's box and no limit, and lists every shift the browser kept, each part
# marked HQ's or the script's, and the score HQ's shifts make.
LAYOUT_SHIFTS = (
    "(given) => {"
    + DESCRIBE
    + f"""
  const [vendored, limit] = given ?? [{json.dumps(SELECTORS["vendored"])}, null];"""
    + """
  if (!PerformanceObserver.supportedEntryTypes.includes('layout-shift')) return [];
  const boxes = [...document.querySelectorAll(vendored)].map((el) => {
    const r = el.getBoundingClientRect();
    return [el, r.left + scrollX, r.top + scrollY, r.right + scrollX, r.bottom + scrollY];
  });
  const meets = (rect, [, left, top, right, bottom]) => rect.left < right - 1 && rect.right > left + 1
    && rect.top < bottom - 1 && rect.bottom > top + 1;
  const theScripts = (part) => boxes.some((box) => (part.node?.isConnected
    ? box[0].contains(part.node) : meets(part.previousRect, box) || meets(part.currentRect, box)));
  const at = (rect) => [rect.x, rect.y, rect.width, rect.height].map(Math.round).join(',');
  const named = (node) => {
    if (!node?.isConnected) return 'a node since removed';
    const el = node instanceof Element ? node : node.parentElement;
    const words = (node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 30);
    return `${describe(el)} "${words}"`;
  };
  const moved = (part) => `${named(part.node)} ${at(part.previousRect)} -> ${at(part.currentRect)}`;
  const when = (shift) => `${shift.value.toFixed(4)} at ${Math.round(shift.startTime)} ms`;
  const said = (shift) => `${when(shift)}: ${shift.sources.map(moved).join('; ')}`;
  const whose = (shift) => `${when(shift)}${shift.hadRecentInput ? ' after input' : ''}: `
    + shift.sources.map((part) => `${moved(part)} (${theScripts(part) ? "the script's" : "HQ's"})`).join('; ');
  const sum = (shifts) => shifts.reduce((total, shift) => total + shift.value, 0);
  const faceArrived = Math.max(0, ...performance.getEntriesByType('resource')
    .filter((entry) => entry.name.endsWith('.woff2')).map((entry) => entry.responseEnd)) + 50;
  const worst = (entries) => {
    let found = [], session = [];
    const counted = (s) => !s.hadRecentInput && !s.sources.every(theScripts) && s.startTime > faceArrived;
    for (const shift of entries.filter(counted)) {
      const [first, last] = [session[0], session.at(-1)];
      const joins = first && shift.startTime - last.startTime < 1000 && shift.startTime - first.startTime < 5000;
      session = joins ? [...session, shift] : [shift];
      if (sum(session) > sum(found)) found = session;
    }
    return found;
  };
  const report = (entries) => {
    const session = worst(entries), score = sum(session);
    if (limit === null) return [...entries.map(whose), `HQ's score ${score.toFixed(4)}`];
    return score > limit ? [`layout shift ${score.toFixed(4)} past ${limit}: ${session.map(said).join(' | ')}`] : [];
  };
  // A buffered observe() puts what the browser kept in the observer's buffer
  // as it returns (Performance Timeline, observe()); only the callback waits
  // for a task.
  const observer = new PerformanceObserver(() => {});
  observer.observe({type: 'layout-shift', buffered: true});
  const seen = observer.takeRecords();
  observer.disconnect();
  return report(seen);
}"""
)


# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    # How far the page moved by itself since it began to load, scored from the
    # browser's own record of it as Cumulative Layout Shift.
    "layout shifts": (LAYOUT_SHIFTS, [SELECTORS["vendored"], CLS_LIMIT]),
}
