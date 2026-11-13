"""Probes of color: field and button edges, the fills a theme paints, and the tokens that paint them."""

from hq.platform.core.tests.browser.probes import Check
from hq.platform.core.tests.browser.probes.describe import DESCRIBE
from hq.platform.core.tests.browser.selectors import SELECTORS

# The least a fill may differ from the surface it sits on to show at all:
# below this it is the surface's own color.
LINE_CONTRAST = 1.07

# WCAG 1.4.11: what identifies a control, a field's edge, clears 3:1.
FIELD_CONTRAST = 3.0

# What the browser paints, as an opaque color: any color syntax read back
# through a canvas pixel, a translucent one composited over what is behind it,
# and the WCAG contrast ratio between two.
_COLOR = """
  const canvas = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
  canvas.canvas.width = canvas.canvas.height = 1;
  // Whatever syntax the browser serializes a color in (rgb(), color(srgb),
  // oklch from a color-mix), paint it and read the pixel back.
  const cache = new Map();
  const rgba = (value) => {
    if (cache.has(value)) return cache.get(value);
    canvas.clearRect(0, 0, 1, 1);
    canvas.fillStyle = '#000'; canvas.fillStyle = value;
    canvas.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = canvas.getImageData(0, 0, 1, 1).data;
    const parsed = [r, g, b, a / 255];
    cache.set(value, parsed);
    return parsed;
  };
  const over = (top, under) => {
    const a = top[3];
    return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat(1);
  };
  const background = (el) => {
    const layers = [];
    for (let up = el; up; up = up.parentElement) {
      const color = rgba(getComputedStyle(up).backgroundColor);
      if (color[3] > 0) layers.push(color);
      if (color[3] >= 1) break;
    }
    let result = rgba(getComputedStyle(document.documentElement).backgroundColor);
    if (result[3] < 1) result = [255, 255, 255, 1];
    for (const layer of layers.reverse()) result = over(layer, result);
    return result;
  };
  const luminance = ([r, g, b]) => {
    const [R, G, B] = [r, g, b].map((c) => {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * R + 0.7152 * G + 0.0722 * B;
  };
  const ratio = (a, b) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
"""

# The custom properties (tokens) a color on an element is written in, so a
# finding names the token every page shares rather than the one element it was
# seen on. The browser keeps no record of the declaration that won the cascade:
# getComputedStyle and computedStyleMap give computed values only. A rule's
# own CSSStyleDeclaration keeps what it declared, var() and all (CSSOM, and a
# shorthand written with var() keeps its text where its longhands read empty),
# so the declarations of the property that reach the element are each painted
# where they apply, and the ones that paint the color the element has are read
# for the names in their var(). A color inherited (ink) is declared on the
# nearest box that declares it. A sheet another origin serves, or one a
# vendored widget brings (`vendored`), is not HQ's to name.
_PAINTED_BY = """
  let declarations = null;
  const declared = () => {
    if (declarations) return declarations;
    declarations = [];
    const nest = (selector, parent) => {
      if (selector.includes('&')) return selector.replaceAll('&', `:is(${parent})`);
      return /^\\s*[>+~]/.test(selector) ? `:is(${parent}) ${selector}` : `:is(${parent}) :is(${selector})`;
    };
    const walk = (rules, parent) => {
      for (const rule of rules) {
        if (rule instanceof CSSImportRule) {
          try { walk(rule.styleSheet.cssRules, parent); } catch { /* another origin's */ }
          continue;
        }
        if (rule instanceof CSSMediaRule && !matchMedia(rule.media.mediaText).matches) continue;
        if (rule instanceof CSSSupportsRule && !CSS.supports(rule.conditionText)) continue;
        let selector = parent;
        if (rule instanceof CSSStyleRule) selector = parent === null ? rule.selectorText : nest(rule.selectorText, parent);
        // A nested block of bare declarations applies with its rule's selector;
        // a font face or a keyframe has a style and no selector at all.
        if (rule.style && selector !== null) declarations.push([selector, rule.style]);
        if (rule.cssRules) walk(rule.cssRules, selector);
      }
    };
    for (const sheet of [...document.styleSheets, ...document.adoptedStyleSheets]) {
      if (vendored && sheet.ownerNode?.closest?.(vendored)) continue;
      let rules;
      try { rules = sheet.cssRules; } catch { continue; }
      walk(rules, null);
    }
    return declarations;
  };
  const applies = (node, selector) => { try { return node.matches(selector); } catch { return false; } };
  // What `value`, declared as `property` on `node`, paints there, read as
  // `read`: a box placed in it inherits what it holds (its tokens,
  // currentColor, its palette).
  const paintedAs = (node, property, value, read) => {
    const probe = document.createElement('span');
    probe.style.position = 'absolute';
    probe.style.setProperty(property, value);
    node.append(probe);
    const painted = getComputedStyle(probe)[read];
    probe.remove();
    return painted;
  };
  const scheme = (el) => paintedAs(el, 'color', 'light-dark(rgb(0, 0, 0), rgb(255, 255, 255))', 'color') === 'rgb(255, 255, 255)' ? 'dark' : 'light';
  const PAINTS = {
    color: {declared: ['color'], read: 'color', inherited: true},
    background: {declared: ['background-color', 'background'], read: 'backgroundColor', inherited: false},
  };
  const tokensOf = (el, which) => {
    const {declared: properties, read, inherited} = PAINTS[which];
    for (let node = el; node; node = node.parentElement) {
      const styles = [...declared().filter(([selector]) => applies(node, selector)).map(([, style]) => style), node.style];
      const values = styles.flatMap((style) => properties.map((property) => [property, style.getPropertyValue(property).trim()]))
        .filter(([, value]) => value && !(inherited && ['inherit', 'unset'].includes(value)));
      if (!values.length && inherited && node.parentElement) continue;
      const has = rgba(getComputedStyle(node)[read]).join();
      const names = values.filter(([property, value]) => rgba(paintedAs(node, property, value, read)).join() === has)
        .flatMap(([, value]) => [...value.matchAll(/var\\(\\s*(--[\\w-]+)/g)].map((match) => match[1]));
      return [...new Set(names)];
    }
    return [];
  };
  // A finding's word for what paints a color: its tokens in the palette it is
  // drawn in, or that it is written as a literal.
  const paintedBy = (el, which) => {
    const names = tokensOf(el, which);
    return names.length ? `${names.join(' and ')} in ${scheme(el)}` : 'no token paints this';
  };
  // The box whose fill is what an element is read against.
  const ground = (el) => {
    for (let up = el; up; up = up.parentElement) if (rgba(getComputedStyle(up).backgroundColor)[3] > 0) return up;
    return document.documentElement;
  };
"""

# A field is found by its edge: the border it draws, or, for a control drawn
# bare inside one that acts as the field (a search box's frame, a drop zone),
# the nearest frame around it. That edge, or the frame's own fill, stands out
# from what is behind the frame. A disabled field is exempt, as WCAG exempts it,
# and so is one framed by a control that is pressed (`pressed`: the sort's
# label around its select), which its label identifies, as a button's does.
FIELD_EDGES = (
    "([fillable, least, pressed]) => {"
    + DESCRIBE
    + _COLOR
    + """
  const edged = (style) => ['Top', 'Right', 'Bottom', 'Left'].some((side) =>
    parseFloat(style[`border${side}Width`]) >= 1 && !['none', 'hidden'].includes(style[`border${side}Style`])
    && rgba(style[`border${side}Color`])[3] > 0);
  const found = [];
  for (const field of document.querySelectorAll(fillable)) {
    if (field.closest(':disabled, [aria-disabled="true"], [hidden]')) continue;
    let frame = field;
    while (frame && !edged(getComputedStyle(frame))) frame = frame.parentElement;
    if (!frame || !frame.parentElement || !frame.checkVisibility() || frame.matches(pressed)) continue;
    const style = getComputedStyle(frame);
    const behind = background(frame.parentElement);
    const fill = rgba(style.backgroundColor);
    const sides = ['Top', 'Right', 'Bottom', 'Left'].filter((side) => parseFloat(style[`border${side}Width`]) >= 1);
    const measured = Math.max(
      ...sides.map((side) => ratio(over(rgba(style[`border${side}Color`]), behind), behind)),
      fill[3] > 0 ? ratio(over(fill, behind), behind) : 0,
    );
    if (measured < least) found.push(`${describe(frame)} ${measured.toFixed(2)}:1 on ${behind.slice(0, 3).map(Math.round)}`);
  }
  return [...new Set(found)];
}"""
)

# A field's edge (`--field-line`) says "type here". A control that is pressed
# or picked from (a button, a disclosure's handle, a label around a select or
# a checkbox) is identified by its label and wears a button's edge, so no
# side of its border is drawn in the field's. The token is resolved where the
# control sits, in the palette the page is in.
BUTTON_EDGES = (
    "(pressed) => {"
    + DESCRIBE
    + """
  const found = [];
  const controls = [...document.querySelectorAll(pressed)].filter((control) => control.parentElement && control.checkVisibility());
  // Every parent's probe is placed before any is read, so the page's style
  // is worked out once, where a probe placed and read in turn has it worked
  // out again for each parent.
  const probes = new Map();
  for (const control of controls) {
    if (probes.has(control.parentElement)) continue;
    const probe = document.createElement('span');
    probe.style.cssText = 'position: absolute; border: 1px solid var(--field-line)';
    control.parentElement.append(probe);
    probes.set(control.parentElement, probe);
  }
  const fieldLine = new Map([...probes].map(([parent, probe]) => [parent, getComputedStyle(probe).borderTopColor]));
  probes.forEach((probe) => probe.remove());
  for (const control of controls) {
    const style = getComputedStyle(control);
    const edge = fieldLine.get(control.parentElement);
    const sides = ['Top', 'Right', 'Bottom', 'Left'].filter((side) =>
      parseFloat(style[`border${side}Width`]) > 0 && style[`border${side}Style`] !== 'none' && style[`border${side}Color`] === edge);
    if (sides.length) found.push(`${describe(control)} wears a field's edge`);
  }
  return [...new Set(found)];
}"""
)

# How far each box that paints a fill (a chip, a code span, the item a menu is
# at, a group's heading row) steps from the surface behind it: by its fill or
# by its edge, whichever shows more, with every box it read (`seen`). A live
# page changes between the two readings (a figure that samples adds a bar), so
# a box is keyed by itself, the same key on this load however the document
# around it grows, and is judged only where both palettes read it. A vendored
# widget carries its own two palettes, and is not HQ's to hold.
_FILL_BOXES = "(window.hqFillBoxes ??= {keys: new WeakMap(), boxes: []})"

FILL_STEPS = (
    "([scope, vendored]) => {"
    + DESCRIBE
    + _COLOR
    + f"""
  const known = {_FILL_BOXES};"""
    + """
  const key = (el) => {
    if (!known.keys.has(el)) known.keys.set(el, known.boxes.push(el) - 1);
    return known.keys.get(el);
  };
  const steps = {};
  const seen = [];
  for (const root of document.querySelectorAll(scope)) {
    for (const el of root.querySelectorAll('*')) {
      if (!el.parentElement || el.closest(vendored) || !el.checkVisibility()) continue;
      const r = box(el);
      if (r.width < 2 || r.height < 2) continue;
      seen.push(key(el));
      const style = getComputedStyle(el);
      const fill = rgba(style.backgroundColor);
      if (fill[3] === 0) continue;
      const behind = background(el.parentElement);
      const edges = ['Top', 'Right', 'Bottom', 'Left']
        .filter((side) => parseFloat(style[`border${side}Width`]) >= 1 && !['none', 'hidden'].includes(style[`border${side}Style`]))
        .map((side) => ratio(over(rgba(style[`border${side}Color`]), behind), behind));
      const painted = over(fill, behind);
      const words = el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 30);
      steps[key(el)] = [Math.max(ratio(painted, behind), ...edges), `${describe(el)} (${painted.slice(0, 3).map(Math.round)} on ${behind.slice(0, 3).map(Math.round)}): ${words}`];
    }
  }
  return {steps, seen};
}"""
)


# What paints the fill of each box `places` names (its FILL_STEPS key), in the
# palette the page is drawn in: read only for the boxes a palette loses, in the
# palette that loses them, and only those still on the page.
FILL_PAINTERS = (
    "([places, vendored]) => {"
    + _COLOR
    + _PAINTED_BY
    + f"""
  const known = {_FILL_BOXES};"""
    + """
  return Object.fromEntries(places.map((at) => [at, known.boxes[Number(at)]])
    .filter(([, el]) => el?.isConnected).map(([at, el]) => [at, paintedBy(el, 'background')]));
}"""
)


def losses(steps, least):
    """Each box a fill sets apart in one palette and not in the other, from
    each palette's ``FILL_STEPS``, with the palette that loses it: the two
    palettes are one set of distinctions, so a fill one shows and the other
    loses is a token whose other half was never checked. A fill as faint in
    both is passed: its faintness was chosen."""

    found = []
    for at in sorted(steps["light"].keys() | steps["dark"].keys(), key=int):
        light, dark = (steps[palette].get(at, (1, ""))[0] for palette in ("light", "dark"))
        if min(light, dark) < least <= max(light, dark):
            found.append((at, "dark" if dark < light else "light"))
    return found


def lost_fills(steps, least, painted=None):
    """What ``losses`` finds, said: the box in the palette that loses it, and,
    from ``painted`` (FILL_PAINTERS read in that palette), what paints its
    fill there."""

    found = []
    for at, lost in losses(steps, least):
        read = {palette: steps[palette].get(at, (1, "")) for palette in ("light", "dark")}
        light, dark = read["light"][0], read["dark"][0]
        said = read[lost][1] or read["light" if lost == "dark" else "dark"][1]
        fill = f"; fill: {painted[at]}" if painted and at in painted else ""
        found.append(f"{lost} loses {said}: {light:.2f}:1 in light, {dark:.2f}:1 in dark{fill}")
    return found


# Every color each element is painted in, and the element, in document order:
# what a theme decides. A theme reaches a page two ways, the system's
# preference and the operator's choice (`data-theme`), and the two must paint
# the same page. Autofocus lands in the first frame after a load, which one load
# reaches before it is read and another does not, and a page without script
# runs no frame callback to wait on. So each reading puts focus where autofocus
# would, and a focused page takes no autofocus after it: both loads read the
# same focus, and the focus ring stays among the colors compared.
PAINT = (
    "() => {"
    + DESCRIBE
    + """
  if (document.activeElement === document.body) document.querySelector('[autofocus]')?.focus();
  document.getAnimations().forEach((a) => { try { a.finish(); } catch { a.cancel(); } });
  return [...document.querySelectorAll('body, body *')].map((el) => {
    const s = getComputedStyle(el);
    return [describe(el), [s.color, s.backgroundColor, s.borderTopColor, s.borderBottomColor,
      s.outlineColor, s.fill, s.stroke, s.boxShadow].join(' ')];
  });
}"""
)


# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "fields": Check((FIELD_EDGES, [SELECTORS["fillable"], FIELD_CONTRAST, SELECTORS["pressed"]]), palette=True),
    "button edges": Check((BUTTON_EDGES, SELECTORS["pressed"]), palette=True),
}
