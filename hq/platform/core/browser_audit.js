// Layout audit: the checks a person should not have to run by eye.
//
// Each rule catches a readability fault in a rendered page: a card with a
// band of empty space under its last row, a short card beside a tall one
// leaving a hole, a column seam that moves between bands of the page, a
// tooltip that never fires. None of it is
// reachable from the Django suite, which asserts that data is correct rather
// than that a page is readable.
//
// Run through the Playwright MCP against a page:
//   browser_navigate → browser_run_code_unsafe { filename: hq/platform/core/browser_audit.js }
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
    // card is stretching to a neighbor it should not match, or padding has
    // outlived the content it held.
    const CARD_SLACK = 28;
    // A row that stretches its cards to end together (a section pair, the
    // dashboard's grid) owes a card the difference between what it holds and
    // what the fullest card on its row holds, and only that: slack beyond it
    // is still a fault. Whether a row stretches is read from its computed
    // style, so the audit and the stylesheet cannot disagree about it.
    const PAIRS = '.two-col, .section-flow';
    const stretches = (card, row) => {
      const display = getComputedStyle(row).display;
      if (!/grid|flex/.test(display)) return false;
      const own = getComputedStyle(card).alignSelf;
      const align = ['auto', 'normal'].includes(own) ? getComputedStyle(row).alignItems : own;
      return ['normal', 'stretch'].includes(align);
    };
    const shown = (el) => [...el.children].filter((c) => c.getBoundingClientRect().height > 0);
    const filled = (el) => {
      const kids = shown(el);
      if (!kids.length) return 0;
      return (
        Math.max(...kids.map((k) => k.getBoundingClientRect().bottom)) -
        el.getBoundingClientRect().top
      );
    };
    const owed = (card) => {
      const row = card.parentElement;
      if (!row || !stretches(card, row)) return 0;
      const top = round(card.getBoundingClientRect().top);
      // Compared by *content*, not by box: stretched, the boxes are equal by
      // construction, and what a card was stretched by is how much less it
      // had to say than the fullest beside it.
      const beside = shown(row).filter((k) => round(k.getBoundingClientRect().top) === top);
      return round(Math.max(...beside.map(filled)) - filled(card));
    };
    const CARDS = '.card, figure.chart-card, .control-card, :is(' + PAIRS + ') > :is(section, .page-section)';
    document.querySelectorAll(CARDS).forEach((card) => {
      const kids = shown(card);
      if (!kids.length) return;
      const bottom = card.getBoundingClientRect().bottom;
      const last = Math.max(...kids.map((k) => k.getBoundingClientRect().bottom));
      const slack = round(bottom - last) - owed(card);
      if (slack > CARD_SLACK) add('empty-card-bottom', { card: name(card), slack });
    });

    // A short card beside a tall one. The next row cannot start until the
    // tallest finishes, so the difference is a hole in the page.
    const ROW_GAP = 48;
    document.querySelectorAll('.two-col, .split').forEach((row) => {
      const kids = [...row.children];
      if (kids.length < 2) return;
      // Stacked (a pair's one-column layout on a narrow screen), the cards are
      // not a row, and one being shorter than the other is no hole.
      const tops = kids.map((k) => round(k.getBoundingClientRect().top));
      if (Math.max(...tops) - Math.min(...tops) > 2) return;
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

    // A card far wider than what is drawn in it. Measured as ink (text-node
    // rects and replaced elements) because a paragraph, a flex row or a
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
    // earned it, because the causes are invisible to reading the stylesheet.
    //
    // A drawing given a `min-width` wider than the column it is placed in
    // scrolls in every card narrower than that floor, at widths no breakpoint
    // watches.
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
      // A scroller drawn without a scrollbar offers none: a one-row strip of
      // links that pans sideways is a choice, not an accident.
      // A text field scrolls what is typed into it; that is what it is for.
      if (!axes.length || style.scrollbarWidth === 'none' || el.matches('textarea')) return;
      // A box the stylesheet has explicitly capped is one whose author chose
      // to scroll it: a filter menu held to 280px so a long list of options
      // does not run off the page, a wide table held to its card so it scrolls
      // instead of widening the document. The cap is the statement of intent,
      // so it is read from the box rather than kept as a list of class names
      // here: a list would need editing every time a capped box is added,
      // and a box missing from it would be reported as a fault.
      //
      // So is a box that declares its own vertical scroll and whose height
      // the stylesheet sets without its content (a dialog that is the whole
      // phone screen, `height: 100dvh; overflow-y: auto`). The scroll is its
      // own when it outlives the other axis turning visible, rather than
      // being the `auto` that axis forced on it; the height is set when the
      // box keeps it while overflowing visibly, where a box as tall as its
      // content would grow.
      //
      // An accidental scrollbar is exactly the case with no cap: nothing was
      // limiting the box, it simply came out a pixel smaller than its
      // contents.
      const overridden = (property, read) => {
        const inline = el.getAttribute('style');
        el.style.setProperty(property, 'visible', 'important');
        const value = read();
        if (inline === null) el.removeAttribute('style');
        else el.setAttribute('style', inline);
        return value;
      };
      const scrollsTall = () =>
        scrolls(overridden('overflow-x', () => getComputedStyle(el).overflowY))
        && Math.abs(overridden('overflow', () => el.getBoundingClientRect().height) - el.getBoundingClientRect().height)
          < SCROLL_SLOP;
      const capped = (axis) =>
        axis === 'x' ? style.maxWidth !== 'none' : style.maxHeight !== 'none' || scrollsTall();
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
    // neighbors is the kind of thing that is obvious in a screenshot and
    // invisible in a diff. It happens whenever a control is wrapped (a
    // button in a form, a summary in a details) because the wrapper stretches
    // and its child does not.
    document.querySelectorAll('.page-actions, .form-actions, .filter-bar').forEach((row) => {
      // Controls only: a status line at the end of a filter bar is text, and
      // text is not the height of a button.
      const control = 'input, select, textarea, button, summary, a.btn';
      const controls = Array.from(row.children)
        .map((child) => (child.matches(control) ? child : child.querySelector(control)))
        .filter((el) => el && el.getBoundingClientRect().height > 0);
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
    // in a row is simply cut in half and nothing anywhere reports it: the
    // scrollbar rule above cannot see this.
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

    // The operating system's gray tooltip where the app has its own.
    document.querySelectorAll('.card [title], figure [title]').forEach((el) => {
      add('native-tooltip', { tag: el.tagName.toLowerCase(), title: el.title });
    });

    // The shared tooltip answers any data-tip on hover and on focus, so a tip
    // outside a chart is dead only to the keyboard: an element that cannot take
    // focus never shows it to someone who does not use a pointer. Chart marks
    // are exempt; the chart's own table carries their values.
    const focusable = 'a[href], button, input, select, textarea, summary, [tabindex]';
    document.querySelectorAll('[data-tip]').forEach((el) => {
      if (!el.closest('[data-chart]') && !el.matches(focusable)) add('dead-tooltip', { tip: el.dataset.tip });
    });

    // A column whose every cell is empty or an em dash is a heading over
    // nothing: the session cannot have that measurement.
    //
    // Text is not the only thing a cell can hold. The row-selection column is
    // a checkbox under a deliberately blank header, so measured by text alone
    // it would read as dead on every list page. A cell counts as saying
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
        if (text && text !== '\u2014') return true;
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


    // A sentence inside a flex row. `.row-main` lays its children out as flex
    // items, so inline emphasis inside a sentence is promoted to a block and
    // the sentence is rendered as separate boxes with gaps between them.
    //
    // Detected structurally rather than by guessing at prose: punctuation
    // is unreliable ("27.06%." ends a sentence with no letter before the full
    // stop). What distinguishes a sentence from a row of chips is that a
    // sentence interleaves bare text with elements: a chip row is elements
    // all the way down.
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
