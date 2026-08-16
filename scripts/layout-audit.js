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
    document.querySelectorAll('.card, figure.chart-card').forEach((card) => {
      const kids = [...card.children].filter(
        (c) => c.getBoundingClientRect().height > 0,
      );
      if (!kids.length) return;
      const bottom = card.getBoundingClientRect().bottom;
      const last = Math.max(...kids.map((k) => k.getBoundingClientRect().bottom));
      const slack = round(bottom - last);
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
    document.querySelectorAll('table').forEach((table) => {
      const heads = [...table.querySelectorAll('thead th')].map((th) =>
        th.textContent.trim(),
      );
      const rows = [...table.querySelectorAll('tbody tr')];
      if (rows.length < 2) return;
      heads.forEach((head, index) => {
        const cells = rows.map((r) => (r.children[index] || {}).textContent || '');
        if (cells.every((c) => !c.trim() || c.trim() === '—')) {
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
