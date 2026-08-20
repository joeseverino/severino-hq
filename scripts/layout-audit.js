// Layout audit — the checks a person should not have to run by eye.
//
// Every rule here exists because the same complaint was made more than once
// about a rendered page: a card with a band of empty space under its last row,
// a short card beside a tall one leaving a hole, a column seam that moves
// between bands of the page, a tooltip that never fires. None of it is
// reachable from the Django suite, which asserts that data is correct rather
// than that a page is readable.
//
// Run through the Playwright MCP against a page:
//   browser_navigate → browser_run_code_unsafe { filename: scripts/layout-audit.js }
//
// Returns violations with measurements, so a fix can be checked rather than
// eyeballed. Zero violations is the bar.

async (page) => {
  return await page.evaluate(() => {
    const round = (n) => Math.round(n);
    const violations = [];
    const add = (rule, detail) => violations.push({ rule, ...detail });
    const name = (el) => {
      const heading = el.querySelector('h2, h3');
      return heading ? heading.textContent.trim().slice(0, 40) : el.className;
    };

    // A card whose content stops well above its own bottom edge. Either the
    // card is stretching to a neighbour it should not match, or something was
    // removed and the padding stayed.
    const CARD_SLACK = 28;
    // How much of a card's slack is owed to the pair it sits in. A stretched
    // pair is equal by construction, so the shorter card's spare height is the
    // difference between them -- that is what "equal" costs, not a defect.
    // Only the difference is forgiven: slack beyond it is still a fault, so
    // this cannot quietly excuse an unrelated gap.
    const filled = (el) => {
      const kids = [...el.children].filter(
        (c) => c.getBoundingClientRect().height > 0,
      );
      if (!kids.length) return 0;
      return (
        Math.max(...kids.map((k) => k.getBoundingClientRect().bottom)) -
        el.getBoundingClientRect().top
      );
    };
    const paired = (card) => {
      const row = card.parentElement;
      if (!row || !row.classList.contains('two-col')) return 0;
      if (row.classList.contains('align-top')) return 0;
      const kids = [...row.children].filter(
        (k) => k.getBoundingClientRect().height > 0,
      );
      if (kids.length !== 2) return 0;
      // Compared by *content*, not by box. Once a pair has stretched, the two
      // boxes are equal by construction and their difference is zero -- so
      // measuring the boxes forgives nothing and flags the shorter card for
      // slack the stretch itself created. What one card was stretched by is
      // how much less it had to say than the other.
      const content = kids.map(filled);
      return round(Math.max(...content) - filled(card));
    };
    document.querySelectorAll('.card, figure.chart-card').forEach((card) => {
      const kids = [...card.children].filter(
        (c) => c.getBoundingClientRect().height > 0,
      );
      if (!kids.length) return;
      const bottom = card.getBoundingClientRect().bottom;
      const last = Math.max(...kids.map((k) => k.getBoundingClientRect().bottom));
      const slack = round(bottom - last) - paired(card);
      if (slack > CARD_SLACK) add('empty-card-bottom', { card: name(card), slack });
    });

    // A short card beside a tall one. The next row cannot start until the
    // tallest finishes, so the difference is a hole in the page.
    const ROW_GAP = 48;
    document.querySelectorAll('.two-col, .split').forEach((row) => {
      const kids = [...row.children];
      if (kids.length < 2) return;
      const heights = kids.map((k) => round(k.getBoundingClientRect().height));
      const gap = Math.max(...heights) - Math.min(...heights);
      if (gap <= ROW_GAP) return;
      // A short column is only a hole when something follows it. At the end of
      // a page, columns ending at different points is just the page ending.
      let after = row.nextElementSibling;
      while (after && !after.getBoundingClientRect().height) {
        after = after.nextElementSibling;
      }
      if (!after) return;
      add('row-height-hole', { row: name(kids[0]), heights, gap });
    });

    // The seam between two columns should land in the same place on every
    // row, or the page reads as a stack of unrelated grids.
    const seams = new Set();
    document.querySelectorAll('.two-col, .split').forEach((row) => {
      const kids = [...row.children];
      if (kids.length === 2) {
        seams.add(round(kids[1].getBoundingClientRect().left));
      }
    });
    if (seams.size > 1) add('seam-mismatch', { seams: [...seams].sort() });

    // A card far wider than what is drawn in it. Measured as ink -- text-node
    // rects and replaced elements -- because a paragraph, a flex row or a
    // table stretches to the card whatever its contents need, so no wrapper's
    // width says how much of the card is actually used.
    const CARD_FILL = 0.72;
    const inkRight = (root) => {
      let max = 0;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        if (!node.nodeValue.trim()) continue;
        if (node.parentElement.closest('.section-head, figcaption')) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const r of range.getClientRects()) if (r.width) max = Math.max(max, r.right);
      }
      root.querySelectorAll('svg, img, canvas, input, button, select, textarea').forEach((el) => {
        if (el.closest('.section-head, figcaption')) return;
        const r = el.getBoundingClientRect();
        if (r.width) max = Math.max(max, r.right);
      });
      return max;
    };
    document.querySelectorAll('.card, figure.chart-card').forEach((card) => {
      const rect = card.getBoundingClientRect();
      const style = getComputedStyle(card);
      const left = rect.left + parseFloat(style.paddingLeft);
      const right = rect.right - parseFloat(style.paddingRight);
      if (right - left < 200) return;
      // A form's fields are bounded for readability, so a card wider than the
      // form inside it is not a layout fault.
      if (card.querySelector('form')) return;
      const used = Math.max(inkRight(card), left) - left;
      const fill = used / (right - left);
      if (fill < CARD_FILL) {
        add('card-too-wide', {
          card: name(card),
          fill: +fill.toFixed(2),
          used: round(used),
          available: round(right - left),
        });
      }
    });

    // A row of content far narrower than the box holding it.
    const SLACK_RATIO = 0.6;
    // Legends are excluded: a one-series chart has one legend item, and a
    // short legend is not a layout fault.
    document.querySelectorAll('.pie-row').forEach((box) => {
      const width = box.getBoundingClientRect().width;
      const kids = [...box.children];
      if (!kids.length || !width) return;
      const used = kids.reduce((a, k) => a + k.getBoundingClientRect().width, 0);
      const ratio = used / width;
      if (ratio < SLACK_RATIO) {
        add('horizontal-slack', {
          box: box.className,
          ratio: +ratio.toFixed(2),
          slack: round(width - used),
        });
      }
    });

    // A scrollbar nobody asked for. Any box that offers to scroll and has
    // something to scroll is reported, in both axes, with the overflow that
    // earned it -- because the two ways this has gone wrong were invisible to
    // reading the stylesheet.
    //
    // A drawing given a `min-width` wider than the column it is placed in: the
    // floor was 480px and a half-width card is 284-446px, so every chart on
    // the page grew a horizontal scrollbar at the same moment and none of the
    // breakpoints were watching that band.
    //
    // And `overflow-x: auto` on its own, which is not on its own: the other
    // axis computes from `visible` to `auto`, so one declared scrollbar is two
    // offered ones, and an SVG whose height lands on a fraction overflows
    // itself by a single rounded pixel. One pixel draws a full-height bar.
    //
    // Threshold of 1px, not 0: sub-pixel rounding is normal and is not what
    // this is looking for.
    const SCROLL_SLOP = 1;
    document.querySelectorAll('*').forEach((el) => {
      const style = getComputedStyle(el);
      const scrolls = (value) => value === 'auto' || value === 'scroll';
      const horizontal = el.scrollWidth - el.clientWidth;
      const vertical = el.scrollHeight - el.clientHeight;
      const axes = [];
      if (scrolls(style.overflowX) && horizontal > SCROLL_SLOP) {
        axes.push({ axis: 'x', overflow: round(horizontal) });
      }
      if (scrolls(style.overflowY) && vertical > SCROLL_SLOP) {
        axes.push({ axis: 'y', overflow: round(vertical) });
      }
      if (!axes.length) return;
      // A box the stylesheet has explicitly capped is one whose author chose
      // to scroll it: a filter menu held to 280px so a long list of options
      // does not run off the page, a wide table held to its card so it scrolls
      // instead of widening the document. The cap is the statement of intent,
      // so it is read from the box rather than kept as a list of class names
      // here -- a list would need editing every time a capped box is added,
      // and the one nobody edited it for would be reported as a fault.
      //
      // An accidental scrollbar is exactly the case with no cap: nothing was
      // limiting the box, it simply came out a pixel smaller than its
      // contents.
      const capped = (axis) =>
        axis === 'x' ? style.maxWidth !== 'none' : style.maxHeight !== 'none';
      axes.forEach(({ axis, overflow }) => {
        if (capped(axis)) return;
        add('unwanted-scrollbar', {
          box: el.className || el.tagName.toLowerCase(),
          card: name(el.closest('.card, figure.chart-card') || el),
          axis,
          overflow,
          box_size: axis === 'x' ? el.clientWidth : el.clientHeight,
        });
      });
    });

    // Controls sitting together at different heights. A row of buttons is
    // read as one object, and one control four pixels shorter than its
    // neighbours is the kind of thing that is obvious in a screenshot and
    // invisible in a diff. It happens whenever a control is wrapped -- a
    // button in a form, a summary in a details -- because the wrapper stretches
    // and its child does not.
    document.querySelectorAll('.page-actions, .form-actions, .filter-bar').forEach((row) => {
      const controls = Array.from(row.children)
        .map((child) => child.querySelector('button, summary, a.btn') || child)
        .filter((el) => el.getBoundingClientRect().height > 0);
      if (controls.length < 2) return;
      const heights = controls.map((el) => round(el.getBoundingClientRect().height));
      const spread = Math.max(...heights) - Math.min(...heights);
      if (spread > 1) {
        add('uneven-controls', {
          row: row.className,
          heights: heights.join(', '),
          spread,
        });
      }
    });

    // Content wider than the box holding it, where the box hides the evidence.
    // `overflow: clip` and `hidden` produce no scrollbar, so the last control
    // in a row is simply cut in half and nothing anywhere reports it -- the
    // scrollbar rule above cannot see this, which is exactly how a clipped
    // action row reached production.
    document.querySelectorAll('main, .page-head, .page-actions, .card').forEach((el) => {
      const style = getComputedStyle(el);
      const hides = ['clip', 'hidden'].includes(style.overflowX);
      const over = el.scrollWidth - el.clientWidth;
      if (!hides || over <= 1) return;
      add('clipped-content', {
        box: el.className || el.tagName.toLowerCase(),
        overflow_x: style.overflowX,
        content: el.scrollWidth,
        box_size: el.clientWidth,
        cut_off: over,
      });
    });

    // The operating system's grey tooltip where the app has its own.
    document.querySelectorAll('.card [title], figure [title]').forEach((el) => {
      add('native-tooltip', { tag: el.tagName.toLowerCase(), title: el.title });
    });

    // A data-tip with no data-chart ancestor never fires at all.
    document.querySelectorAll('[data-tip]').forEach((el) => {
      if (!el.closest('[data-chart]')) add('dead-tooltip', { tip: el.dataset.tip });
    });

    // A column whose every cell is empty or an em dash is a heading over
    // nothing: the session cannot have that measurement.
    //
    // Text is not the only thing a cell can hold. The row-selection column is
    // a checkbox under a deliberately blank header, so measured by text alone
    // it read as dead on every list page in HQ -- a rule that is wrong on
    // pages that are right is worse than no rule, because the next real dead
    // column arrives in a report nobody trusts. A cell counts as saying
    // something if it has text or if it has a control in it.
    document.querySelectorAll('table').forEach((table) => {
      const heads = [...table.querySelectorAll('thead th')].map((th) =>
        th.textContent.trim(),
      );
      const rows = [...table.querySelectorAll('tbody tr')];
      if (rows.length < 2) return;
      const speaks = (cell) => {
        if (!cell) return false;
        const text = (cell.textContent || '').trim();
        if (text && text !== '—') return true;
        return !!cell.querySelector('input, button, select, textarea, a, svg, img');
      };
      heads.forEach((head, index) => {
        if (!rows.some((row) => speaks(row.children[index]))) {
          add('dead-column', { table: heads.join(' | '), column: head });
        }
      });
    });

    // Numbers in one column should share a right edge.
    document.querySelectorAll('table').forEach((table) => {
      const columns = new Map();
      table.querySelectorAll('tbody td.num-col').forEach((cell) => {
        const rect = cell.getBoundingClientRect();
        // A cell inside a closed disclosure has no box. It is not ragged, it
        // is not on screen.
        if (!rect.width && !rect.height) return;
        const key = cell.cellIndex;
        const right = round(rect.right);
        columns.set(key, (columns.get(key) || new Set()).add(right));
      });
      columns.forEach((edges, index) => {
        if (edges.size > 1) {
          add('ragged-number-column', { column: index, edges: [...edges] });
        }
      });
    });

    // A paired row whose two cards end at different points. This is the single
    // most-repeated complaint about these pages, and it was answered by hand
    // each time -- reordering cards, trimming a table, moving a panel -- which
    // only ever fixed the one session whose data happened to be on screen.
    // A pair stretches; if these differ, something stopped it stretching.
    document.querySelectorAll('.two-col').forEach((row) => {
      const kids = [...row.children].filter(
        (k) => k.getBoundingClientRect().height > 0,
      );
      if (kids.length !== 2) return;
      const heights = kids.map((k) => round(k.getBoundingClientRect().height));
      const gap = Math.max(...heights) - Math.min(...heights);
      if (gap > 2) {
        add('uneven-pair', { row: name(kids[0]), heights, gap });
      }
    });

    // A sentence inside a flex row. `.row-main` lays its children out as flex
    // items, so inline emphasis inside a sentence is promoted to a block and
    // the sentence is rendered as separate boxes with gaps between them.
    //
    // Detected structurally rather than by guessing at prose: the first
    // version of this rule looked for sentence punctuation and missed the
    // defect that prompted it, because the text read "27.06%." and the
    // pattern wanted letters before the full stop. What actually distinguishes
    // a sentence from a row of chips is that a sentence interleaves bare text
    // with elements -- a chip row is elements all the way down. That has no
    // heuristic in it and no false positives on the rows already written.
    document.querySelectorAll('.list-rows .row-main, .list-rows li').forEach((row) => {
      if (getComputedStyle(row).display !== 'flex') return;
      const texts = [...row.childNodes].filter(
        (n) => n.nodeType === 3 && n.nodeValue.trim(),
      ).length;
      const elements = [...row.children].length;
      if (texts && elements) {
        add('sentence-in-a-chip-row', {
          row: row.className,
          text: row.textContent.replace(/\s+/g, ' ').trim().slice(0, 80),
        });
      }
    });

    return {
      url: location.pathname,
      viewport: [innerWidth, innerHeight],
      count: violations.length,
      violations,
    };
  });
}
