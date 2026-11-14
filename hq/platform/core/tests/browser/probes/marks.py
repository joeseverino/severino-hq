"""Probes of how words are marked: a cut with its ellipsis, a bold the font
has, a fill that ends between words, and small print under what it notes."""

from hq.platform.core.tests.browser.probes import Check
from hq.platform.core.tests.browser.probes.describe import DESCRIBE, PAINTED_PART, TEXT_SHOWN

# A line cut with an ellipsis says it was cut, but only where the ellipsis is
# drawn: it stands in for text alone, so a box of its own crossing the cut
# (an inline block, a `|breakable` token outside a cell) is clipped mid-word
# with no mark, and reads as the whole value.
CUT_WITH_NO_ELLIPSIS = (
    "() => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll('body *')) {
    const style = getComputedStyle(el);
    if (style.textOverflow !== 'ellipsis' || style.overflowX === 'visible' || el.scrollWidth <= el.clientWidth + 1) continue;
    if (!el.checkVisibility({visibilityProperty: true})) continue;
    const end = el.getBoundingClientRect().left + el.clientLeft + el.clientWidth;
    for (const inner of el.querySelectorAll('*')) {
      if (['inline', 'contents', 'none'].includes(getComputedStyle(inner).display)) continue;
      // What the box draws, past its own edge too when its words run over it.
      const b = inner.getBoundingClientRect();
      const right = Math.max(b.right, b.left + inner.scrollWidth);
      if (b.width < 1 || b.left >= end - 1 || right <= end + 1) continue;
      found.push(`${describe(el)} cuts ${describe(inner)} "${inner.textContent.trim().slice(0, 30)}" with no ellipsis`);
      break;
    }
  }
  return found;
}"""
)


# Words set in a weight their face does not have, which the browser fakes by
# smearing the face it has thicker: a `strong` in a line already bold asks
# for `bolder` past the heaviest weight the font is served in. Only a face
# the page loaded is judged, since a system font has its own bold.
FAKED_WEIGHTS = (
    "() => {"
    + DESCRIBE
    + TEXT_SHOWN
    + r"""
  const heaviest = {};
  for (const face of document.fonts) {
    if (face.status !== 'loaded') continue;
    const family = face.family.replace(/["']/g, '');
    heaviest[family] = Math.max(heaviest[family] ?? 0, ...face.weight.split(/\s+/).map(Number));
  }
  const found = [];
  for (const el of document.querySelectorAll('body *')) {
    const words = [...el.childNodes].find((node) => node.nodeType === Node.TEXT_NODE && node.data.trim());
    if (!words || !textShown(words)) continue;
    const style = getComputedStyle(el);
    if (style.fontSynthesisWeight === 'none') continue;
    const family = style.fontFamily.split(',')[0].trim().replace(/["']/g, '');
    const weight = parseFloat(style.fontWeight);
    if (heaviest[family] !== undefined && weight > heaviest[family]) {
      found.push(`${describe(el)} "${words.data.trim().slice(0, 30)}" asks ${family} for ${weight}, served to ${heaviest[family]}`);
    }
  }
  return found;
}"""
)


# A fill drawn behind words (a share's bar, a box placed under a row) ends
# between words: one ending inside a word draws it half on the fill and half
# off it, read as a highlight cut short. A fill is a box placed out of flow
# that holds no words of its own and paints, or a meter, whose bar ends at its
# value's share of its width.
WORDS_ACROSS_A_FILL = (
    "() => {"
    + DESCRIBE
    + PAINTED_PART
    + TEXT_SHOWN
    + r"""
  const shown = (color) => !/^(transparent|rgba\(.*,\s*0\)|color\(.*\/\s*0\))$/.test(color);
  const range = document.createRange();
  const found = [];
  for (const fill of document.querySelectorAll('main *')) {
    const style = getComputedStyle(fill);
    if (!['absolute', 'fixed'].includes(style.position) || fill.textContent.trim() || !fill.checkVisibility()) continue;
    const b = fill.getBoundingClientRect();
    if (b.width < 2 || b.height < 2) continue;
    let end;
    if (fill.tagName === 'METER') end = b.left + b.width * ((fill.value - fill.min) / ((fill.max - fill.min) || 1));
    else if (shown(style.backgroundColor)) end = b.right;
    else continue;
    const walker = document.createTreeWalker(fill.parentElement, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!textShown(node)) continue;
      for (const word of node.data.matchAll(/\S+/g)) {
        range.setStart(node, word.index);
        range.setEnd(node, word.index + word[0].length);
        const across = [...range.getClientRects()].map((r) => painted(node.parentElement, r))
          .some((r) => r && r.top < b.bottom - 1 && r.bottom > b.top + 1 && r.left < end - 1 && r.right > end + 1);
        if (across) found.push(`${describe(fill)} ends inside "${word[0].slice(0, 30)}"`);
      }
    }
  }
  return [...new Set(found)];
}"""
)


# Small print stands under the line it notes, its words beneath that line's
# words: set clear of them, to one side, it is paired with nothing (a
# toggle's label centered in its line over notes at the line's start).
NOTES_APART = (
    "(notes) => {"
    + DESCRIBE
    + TEXT_SHOWN
    + """
  const range = document.createRange();
  const span = (el) => {
    let left = Infinity, right = -Infinity;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.data.trim() || !textShown(node)) continue;
      range.selectNodeContents(node);
      for (const r of range.getClientRects()) {
        if (r.width < 1) continue;
        left = Math.min(left, r.left); right = Math.max(right, r.right);
      }
    }
    return left < right ? {left, right} : null;
  };
  const found = [];
  for (const note of document.querySelectorAll('main ' + notes)) {
    const above = note.previousElementSibling;
    if (!above || !note.checkVisibility() || !above.checkVisibility()) continue;
    if (above.getBoundingClientRect().bottom > note.getBoundingClientRect().top + 1) continue;
    const [noted, said] = [span(above), span(note)];
    if (noted && said && (said.left >= noted.right - 1 || said.right <= noted.left + 1)) {
      found.push(`${describe(note)} "${note.textContent.trim().slice(0, 30)}" stands clear of the words of ${describe(above)} "${above.textContent.trim().slice(0, 30)}"`);
    }
  }
  return found;
}"""
)


# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {
    "cut with no ellipsis": Check(
        CUT_WITH_NO_ELLIPSIS, reader_sees="a value cut off mid-word with no mark that it goes on"
    ),
    "faked weights": Check(
        FAKED_WEIGHTS, reader_sees="words in a bold their font lacks, smeared thicker by the browser"
    ),
    "words across a fill": Check(
        WORDS_ACROSS_A_FILL,
        reader_sees="a word half on a bar drawn behind it and half off, read as a highlight cut short",
    ),
    "notes apart": Check(
        (NOTES_APART, "small"), reader_sees="small print set to one side of the line it notes, paired with nothing"
    ),
}
