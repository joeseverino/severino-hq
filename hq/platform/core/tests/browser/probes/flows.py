"""Probes of the flow (`.section-flow`): the order it paints its members in,
and the rows they share."""

from hq.platform.core.tests.browser.probes.describe import DESCRIBE
from hq.platform.core.tests.browser.selectors import SELECTORS

# A flow paints its members in the order the markup holds them, row by row
# and along each row from its inline start, so what a reader hears and where
# the keyboard goes is the order the page is drawn in (WCAG 1.3.2, 2.4.3). A
# member painted on an earlier row than the one before it, or before it on
# the same row, was moved by the stylesheet (`order`, a reversed direction).
FLOWS_IN_READING_ORDER = (
    "(flow) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll(flow)) {
    if (!el.checkVisibility()) continue;
    const rtl = getComputedStyle(el).direction === 'rtl';
    const start = (r) => (rtl ? -r.right : r.left);
    const members = inFlow(el);
    for (let i = 1; i < members.length; i++) {
      const [before, , a] = members[i - 1];
      const [member, , b] = members[i];
      const sameRow = Math.abs(b.top - a.top) <= 1;
      if (b.top < a.top - 1 || (sameRow && start(b) < start(a) - 1)) {
        found.push(`${describe(el)} paints ${describe(member)} before ${describe(before)}, which the markup puts first`);
      }
    }
  }
  return found;
}"""
)

# A flow's short members collect to share rows. A wide member (one that takes
# the whole row) written between two short ones that would share a row splits
# them, each run left short of the row it could fill; and a member left alone
# on its row while a later one would have fitted beside it sits beside a gap.
# A wide member goes ahead of the short ones in the markup, which then collect
# after it. Read from each member's flex basis, the width it asks a row for.
# Every member grows into what its row leaves, so a row ends at the flow's
# end: one that caps its width (a max size) or grows by less than its share
# (a flex-grow under one) leaves the rest of any row it ends empty. Read from
# its computed style rather than from where its row ends, so a cap is found
# at any width, not only at one where the row is wide enough to reach it.
FLOWS_SHARE_THEIR_ROWS = (
    "(flow) => {"
    + DESCRIBE
    + """
  const found = [];
  for (const el of document.querySelectorAll(flow)) {
    if (!el.checkVisibility()) continue;
    const at = getComputedStyle(el);
    const room = el.clientWidth - parseFloat(at.paddingLeft) - parseFloat(at.paddingRight);
    const gap = parseFloat(at.columnGap) || 0;
    const asks = (style) => {
      const basis = style.flexBasis;
      if (basis.endsWith('%')) return (parseFloat(basis) / 100) * room;
      return basis.endsWith('px') ? parseFloat(basis) : null;
    };
    const members = inFlow(el).map(([member, style, r]) => ({member, style, r, wants: asks(style)}));
    for (const {member, style} of members) {
      const cap = [['max-inline-size', style.maxInlineSize], ['max-width', style.maxWidth]].find(([, value]) => value !== 'none');
      if (cap) found.push(`${describe(member)} caps its width in ${describe(el)} (${cap[0]}: ${cap[1]}), so a row it ends stops short of the flow's end`);
      else if (parseFloat(style.flexGrow) < 1) found.push(`${describe(member)} grows by ${style.flexGrow} in ${describe(el)}, so a row it ends stops short of the flow's end`);
    }
    const pair = (a, b) => a.wants !== null && b.wants !== null && a.wants + gap + b.wants <= room + 1;
    const wide = (m) => m.wants !== null && m.wants >= room - 1;
    members.forEach((m, i) => {
      if (wide(m)) {
        const before = members.slice(0, i).filter((o) => !wide(o));
        const after = members.slice(i + 1).filter((o) => !wide(o));
        const split = before.flatMap((a) => after.filter((b) => pair(a, b)).map((b) => [a, b]))[0];
        if (split) found.push(`${describe(m.member)} takes a row of ${describe(el)} between ${describe(split[0].member)} and ${describe(split[1].member)}, which would share one`);
        return;
      }
      const alone = members.every((o, j) => j === i || Math.abs(o.r.top - m.r.top) > 1);
      const later = alone && members.slice(i + 1).find((o) => !wide(o) && pair(m, o));
      if (later) found.push(`${describe(m.member)} sits alone on its row in ${describe(el)}, where ${describe(later.member)} after it would share it`);
    });
  }
  return found;
}"""
)

CHECKS = {
    "flows in reading order": (FLOWS_IN_READING_ORDER, SELECTORS["flow"]),
    "flows share their rows": (FLOWS_SHARE_THEIR_ROWS, SELECTORS["flow"]),
}
