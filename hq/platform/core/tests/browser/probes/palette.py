"""Probes of color: contrast, field and button edges, and the fills a theme paints."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE

# WCAG AA: body text, and text large enough to need less (24px, or 18.66px bold).
TEXT_CONTRAST = 4.5

LARGE_TEXT_CONTRAST = 3.0

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

CONTRAST = (
    "([scope, body, large]) => {"
    + DESCRIBE
    + _COLOR
    + """
  // Every visible element that holds text of its own, measured against the
  // color actually behind it: the nearest ancestor that paints a background,
  // with translucent layers composited down to an opaque one. A color
  // written for one palette only is what this catches: a light-mode ink left
  // on a dark card reads as nothing, and no other check looks at color.
  // Inactive controls and deliberately faded content are exempt, as WCAG
  // exempts them: they say they are not in play.
  const inactive = (el) => el.closest(':disabled, [aria-disabled="true"], [hidden]')
    || [...function* () { for (let up = el; up; up = up.parentElement) yield up; }()]
      .some((up) => parseFloat(getComputedStyle(up).opacity) < 1);
  const found = [];
  let sampled = 0;
  for (const root of document.querySelectorAll(scope)) {
    for (const el of root.querySelectorAll('*')) {
      const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
      if (!own || !el.checkVisibility()) continue;
      const r = box(el);
      if (r.width < 2 || r.height < 2 || inactive(el)) continue;
      const style = getComputedStyle(el);
      const size = parseFloat(style.fontSize);
      // Compact calendar bars retain accessible text but paint no glyphs.
      if (size === 0) continue;
      const bold = parseInt(style.fontWeight, 10) >= 700;
      const needed = size >= 24 || (bold && size >= 18.66) ? large : body;
      const behind = background(el);
      const ink = over(rgba(style.color), behind);
      const measured = ratio(ink, behind);
      sampled += 1;
      if (measured < needed) {
        found.push(`${describe(el)} ${measured.toFixed(2)}:1 (${style.color} on ${behind.slice(0, 3).map(Math.round)}): ${el.textContent.trim().slice(0, 40)}`);
      }
    }
  }
  return {sampled, found: [...new Set(found)]};
}"""
)


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
  const fieldLine = new Map();
  const resolved = (parent) => {
    if (!fieldLine.has(parent)) {
      const probe = document.createElement('span');
      probe.style.cssText = 'position: absolute; border: 1px solid var(--field-line)';
      parent.append(probe);
      fieldLine.set(parent, getComputedStyle(probe).borderTopColor);
      probe.remove();
    }
    return fieldLine.get(parent);
  };
  for (const control of document.querySelectorAll(pressed)) {
    if (!control.parentElement || !control.checkVisibility()) continue;
    const style = getComputedStyle(control);
    const edge = resolved(control.parentElement);
    const sides = ['Top', 'Right', 'Bottom', 'Left'].filter((side) =>
      parseFloat(style[`border${side}Width`]) > 0 && style[`border${side}Style`] !== 'none' && style[`border${side}Color`] === edge);
    if (sides.length) found.push(`${describe(control)} wears a field's edge`);
  }
  return [...new Set(found)];
}"""
)

# How far each box that paints a fill (a chip, a code span, the item a menu is
# at, a group's heading row) steps from the surface behind it: by its fill or
# by its edge, whichever shows more. Keyed by the box's place in the document,
# so the same box can be read again in the other palette. A vendored widget
# carries its own two palettes, and is not HQ's to hold.
FILL_STEPS = (
    "([scope, vendored]) => {"
    + DESCRIBE
    + _COLOR
    + """
  const place = new Map([...document.querySelectorAll('*')].map((el, at) => [el, at]));
  const steps = {};
  for (const root of document.querySelectorAll(scope)) {
    for (const el of root.querySelectorAll('*')) {
      if (!el.parentElement || el.closest(vendored) || !el.checkVisibility()) continue;
      const r = box(el);
      if (r.width < 2 || r.height < 2) continue;
      const style = getComputedStyle(el);
      const fill = rgba(style.backgroundColor);
      if (fill[3] === 0) continue;
      const behind = background(el.parentElement);
      const edges = ['Top', 'Right', 'Bottom', 'Left']
        .filter((side) => parseFloat(style[`border${side}Width`]) >= 1 && !['none', 'hidden'].includes(style[`border${side}Style`]))
        .map((side) => ratio(over(rgba(style[`border${side}Color`]), behind), behind));
      const painted = over(fill, behind);
      const words = el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 30);
      steps[place.get(el)] = [Math.max(ratio(painted, behind), ...edges), `${describe(el)} (${painted.slice(0, 3).map(Math.round)} on ${behind.slice(0, 3).map(Math.round)}): ${words}`];
    }
  }
  return steps;
}"""
)


def lost_fills(steps, least):
    """The boxes a fill sets apart in one palette and not in the other, from
    each palette's ``FILL_STEPS``: the two palettes are one set of
    distinctions, so a fill one shows and the other loses is a token whose
    other half was never checked. A fill as faint in both is passed: its
    faintness was chosen."""

    found = []
    for at in sorted(steps["light"].keys() | steps["dark"].keys(), key=int):
        read = {palette: steps[palette].get(at, (1, "")) for palette in ("light", "dark")}
        light, dark = read["light"][0], read["dark"][0]
        if min(light, dark) < least <= max(light, dark):
            lost = "dark" if dark < light else "light"
            said = read[lost][1] or read["light" if lost == "dark" else "dark"][1]
            found.append(f"{lost} loses {said}: {light:.2f}:1 in light, {dark:.2f}:1 in dark")
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
