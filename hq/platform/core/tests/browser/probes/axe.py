"""axe-core, Deque's accessibility engine, as a probe: the rules of its that HQ's pages are held to."""

from functools import cache
from pathlib import Path

from hq.platform.core.tests.browser.probes import Check
from hq.platform.core.tests.browser.probes.describe import DESCRIBE
from hq.platform.core.tests.browser.probes.palette import _COLOR, _PAINTED_BY
from hq.platform.core.tests.browser.probes.settle import settled
from hq.platform.core.tests.browser.selectors import SELECTORS

# Where setup:lint-tools installs the release tools/lint/package.json pins.
AXE_CORE = Path(__file__).parents[6] / "tools" / "lint" / "node_modules" / "axe-core" / "axe.min.js"

# Text contrast (WCAG 1.4.3), read in each palette rather than with the rest.
CONTRAST = {"type": "rule", "values": ["color-contrast"]}

# The page as it is loaded is held to every rule of WCAG 2.0, 2.1 and 2.2 at
# A and AA and of axe's best practices, its experimental rules aside, but the
# ones LEFT_OUT names.
TAGS = {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]}

# Each rule of TAGS the survey does not hold, and why: contrast is read in
# each palette, and axe reads target size short of WCAG 2.5.8.
LEFT_OUT = {
    "color-contrast": "held in each palette, as the survey's contrast",
    # axe counts neither the area a pseudo-element hangs over the space beside
    # a target nor a tick box's labels, and measures spacing from the target's
    # own box, so it refuses the calendar's marks, the figures' dots and a
    # link the size of its line, each of which SMALL_TARGETS passes as the
    # criterion does.
    "target-size": "held by SMALL_TARGETS",
}


@cache
def axe_core():
    """axe-core's source, read once a run asks for it, so a checkout that
    has not installed the lint tools imports this module all the same."""

    try:
        return AXE_CORE.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RuntimeError(f"{AXE_CORE} is not installed. Run: mise run setup:lint-tools") from None


@cache
def axe_probe():
    """The probe that runs axe over what ``scope`` selects (the document
    without one), as ``([scope, only, leftOut, vendored]) =>
    findings``: ``only`` is axe's runOnly, ``leftOut`` the rules it skips.

    It is evaluated as every probe is, by Playwright rather than by a script
    tag, so the page's script-src never meets it. With script off an engine
    runs no timer, and Chromium calls back nothing queueMicrotask holds, yet
    each runs a promise's reactions; axe settles every rule on a timer of no
    delay, so here such a timer runs as one. A timer with a delay is axe's
    wait on a frame or an asset, which it is told not to fetch, and never
    runs, as it would not with script off. The bundle sets ``window.axe``,
    taken back once read, so a page's own scripts find nothing of it.

    A contrast finding names the tokens its ink and its ground are painted
    in, as the fill check's do, so the fix goes to the token every page
    shares. It reads the page settled (``settled``)."""

    return settled(
        "async ([scope, only, leftOut, vendored]) => {"
        + "const setTimeout = (run, delay, ...args) => { if (!delay) Promise.resolve().then(() => run(...args)); return 0; };"
        + "const clearTimeout = () => {};"
        + axe_core()
        + DESCRIBE
        + _COLOR
        + _PAINTED_BY
        + """
  const axe = window.axe;
  delete window.axe;
  const include = scope ? [...document.querySelectorAll(scope)] : [document];
  if (!include.length) return [`nothing on the page matches ${scope}`];
  // WCAG leaves out only an inactive component, and axe's contrast rule
  // already does: a control disabled or aria-disabled is not measured, with
  // all it holds. Text faded by an opacity is measured as it is drawn.
  const {violations, incomplete} = await axe.run({include}, {
    runOnly: only,
    rules: Object.fromEntries(leftOut.map((rule) => [rule, {enabled: false}])),
    resultTypes: ['violations', 'incomplete'],
    elementRef: true,
    preload: false,
    iframes: false,
  });
  // What axe measured, both colors read, and leaves to a person all the
  // same: ink the color of its ground, which reads as nothing, and a lone
  // character, which it counts as too short to judge and a reader still has
  // to read.
  const measured = ({data}) => ['equalRatio', 'shortTextContent'].includes(data?.messageKey)
    && data.fgColor && data.bgColor && data.contrastRatio < parseFloat(data.expectedContrastRatio);
  const unread = incomplete.filter(({id}) => id === 'color-contrast')
    .map((result) => ({...result, nodes: result.nodes.filter((node) => node.any.some(measured))}));
  const found = [];
  for (const {id, nodes} of [...violations, ...unread]) {
    for (const node of nodes) {
      const el = node.element;
      const words = (el === document.documentElement ? '' : el.textContent || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
      const contrast = node.any.find(({data}) => data?.contrastRatio !== undefined)?.data;
      if (id === 'color-contrast' && contrast) {
        const {contrastRatio, fgColor, bgColor, expectedContrastRatio} = contrast;
        found.push(`${describe(el)} ${contrastRatio}:1 (${fgColor} on ${bgColor}, needs ${expectedContrastRatio}): ${words}; ink: ${paintedBy(el, 'color')}; ground: ${paintedBy(ground(el), 'background')}`);
        continue;
      }
      const said = [...node.any, ...node.all, ...node.none].map((check) => check.message).join('; ');
      const where = el.parentElement ? ` in ${describe(el.parentElement)}` : '';
      found.push(`${id}: ${describe(el)}${where}: ${said}: ${words}`);
    }
  }
  return [...new Set(found)];
}"""
    )


def contrast_read():
    """Text contrast over the page and the header over it, as a probe and its
    argument."""

    return (axe_probe(), [SELECTORS["contrast_scope"], CONTRAST, [], SELECTORS["vendored"]])


def rules_read(left_out=()):
    """Every rule of TAGS but those LEFT_OUT and ``left_out``, the rules a
    page declares it leaves out (``BrowserGate.left_out_on``), over the whole
    page, as a probe and its argument."""

    return (axe_probe(), [None, TAGS, [*LEFT_OUT, *left_out], SELECTORS["vendored"]])


# The survey checks these probes answer (`probes/__init__.py`), each read only
# when it runs: a tree without the lint tools (the image, the composed suite's
# copy) names them all the same.
CHECKS = {
    "axe": Check(rules_read, reader_sees="a part a screen reader or the keyboard cannot use"),
    "contrast": Check(contrast_read, palette=True, reader_sees="text too faint to read"),
}
