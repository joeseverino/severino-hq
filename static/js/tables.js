(() => {
  let searchTimer = null;

  // Selection is keyed by row id, survives table refreshes and pagination,
  // and persists across full reloads of the same list page for the session.
  const selectionKey = `hq-selection:${window.location.pathname}`;

  const readSelection = () => {
    try {
      return JSON.parse(window.sessionStorage.getItem(selectionKey)) || [];
    } catch {
      return [];
    }
  };

  const selectedIds = new Set(readSelection());

  // A table fits its box. While it is wider than its region, a ranked column
  // showing leaves (`data-drop` 1 first, the rightmost of a rank first);
  // once none is left to go the table is in a narrow box, and if it is still
  // wider, what keeps its line whole gives way too (`data-tight`), then its
  // identifiers break where they must (`data-break`); a table wider than its
  // region even then scrolls in it (`data-scrolls`), so no
  // table ever runs past its card, and the gate's "tables fit" names it; as
  // the region grows they come back, the last to leave first, each only if the
  // table still fits. A row's toggle shows what left. Only the browser
  // knows how wide a table is, so a table is measured as its rows arrive,
  // in the microtask after the parser or a script adds them, before the
  // frame that would draw them: this script loads in the head without
  // `defer`, so every region is fitted from the moment it is parsed and
  // nothing moves once a page is drawn.
  const REGION = ".table-scroll";
  const headingsOf = (table) => [...table.querySelectorAll(":scope > thead > tr:last-child > th")];
  // The order the table's columns leave in, by place.
  const dropOrder = (table) => headingsOf(table)
    .map((heading, place) => [Number(heading.dataset.drop) || 0, place, heading])
    .filter(([rank, , heading]) => rank && !heading.hasAttribute("data-blank-column"))
    .sort((left, right) => left[0] - right[0] || right[1] - left[1])
    .map(([, place]) => place);
  // How far each table has given way: how many of its columns have left, one
  // more once it is in a narrow box, one more once it is tight, one more once
  // its identifiers break, and one more once it scrolls.
  const gaveWay = new WeakMap();
  const giveWay = (table, steps) => {
    const order = dropOrder(table);
    const left = new Set(order.slice(0, steps));
    const width = headingsOf(table).length;
    // Every row is marked, since the parser adds a long table's later rows
    // after its first are fitted; a cell already marked so is not changed.
    table.querySelectorAll(":scope > * > tr").forEach((row) => {
      if (row.children.length !== width) return;
      order.forEach((place) => row.children[place].toggleAttribute("data-dropped", left.has(place)));
    });
    // A column's part of each row's line beneath shows while the column is gone.
    table.querySelectorAll(":scope > tbody > tr.row-detail [data-column]").forEach((part) => {
      part.toggleAttribute("data-dropped", left.has(Number(part.dataset.column)));
    });
    table.toggleAttribute("data-narrow", steps > order.length);
    table.toggleAttribute("data-tight", steps > order.length + 1);
    table.toggleAttribute("data-break", steps > order.length + 2);
    table.toggleAttribute("data-scrolls", steps > order.length + 3);
    // The columns that share the region's width (tables.css, `--share`).
    const sharing = headingsOf(table).filter((heading) => !left.has(heading.cellIndex)
      && !heading.matches(".select-column, [data-blank-column]")).length;
    table.style.setProperty("--showing", Math.min(Math.max(sharing, 1), 16));
    gaveWay.set(table, steps);
  };
  // eslint-disable-next-line no-restricted-syntax -- whether a table is wider than its region, the one question the fit asks
  const overflows = (table) => table.offsetWidth > table.parentElement.clientWidth;
  // A hidden region has none.
  // eslint-disable-next-line no-restricted-syntax -- the width a region is fitted at, so a table is fitted again only when it changes
  const widthOf = (region) => region.clientWidth;
  // A table that overflows gives way until it fits. One that fits takes back
  // what left, the last first, while it still fits; a region that only
  // narrowed has nothing to give back, so that is not tried.
  const fit = (table, mayReturn = true) => {
    const was = gaveWay.get(table);
    const most = dropOrder(table).length + 4;
    let steps = Math.min(was ?? 0, most);
    // Every row is marked for how far the table has given way before it is
    // measured: a table first fitted is told how many columns share its
    // region, so its first measure is the one it keeps, and rows the parser
    // added since are measured without the columns that left, at any step,
    // the last one too.
    giveWay(table, steps);
    if (overflows(table)) {
      while (steps < most && overflows(table)) giveWay(table, ++steps);
      return;
    }
    while (mayReturn && steps > 0) {
      giveWay(table, steps - 1);
      if (overflows(table)) {
        giveWay(table, steps);
        return;
      }
      steps -= 1;
    }
  };
  const tableIn = (region) => region.querySelector(":scope > .data-table");
  // The width each region was last fitted at.
  const regionWidths = new WeakMap();
  // A region's table fitted at the width the region has now. One whose
  // content changed (`changed`) may take back what left; otherwise only a
  // change of width fits again. A table whose columns are held while a row
  // is open is fitted as it lets them go.
  // Every fit passes through here, so here a table with a row open, however
  // it was opened, is fitted as it would stand with them closed and held
  // there: what an open row shows whole never widens a column.
  const fitIn = (region, changed = false) => {
    const table = tableIn(region);
    if (!table || "rowsHeld" in table.dataset) return;
    const width = widthOf(region);
    const before = regionWidths.get(region);
    if (width === before && !changed) return;
    regionWidths.set(region, width);
    if (width === 0) return;
    const open = [...table.querySelectorAll(":scope > tbody > tr.is-expanded")];
    open.forEach((row) => row.classList.remove("is-expanded"));
    fit(table, changed || before === undefined || width > before);
    if (open.length) {
      holdColumns(table);
      open.forEach((row) => row.classList.add("is-expanded"));
    }
    // A table that arrives, changes or comes into sight is measured for cuts
    // as it is fitted: a fragment's, or one in a disclosure just opened. A
    // window resized measures them once it settles (`markTablesSoon`), and
    // what the parser draws is measured once the page has loaded.
    if (document.readyState !== "loading" && (changed || !before)) markCut(table);
  };
  const refit = (table) => {
    if (table?.parentElement?.matches(REGION)) fitIn(table.parentElement, true);
  };
  // The region is an inline-size container, so its width never follows the
  // table's. Chromium counts what a ResizeObserver's callback moves as a
  // layout shift, even before the frame is painted, and a table that grows
  // taller there resizes its region again in the same frame, which the
  // browser reports as an error. So every region is fitted before the frame
  // is laid out: in the microtask after the parser or a script changes the
  // page, since what it adds may be the region's rows or a scrollbar that
  // narrows it, and in the window's resize event, which runs before layout.
  // What holds a region's width otherwise (a region shown, a fold, a row
  // closing, its faces arriving) fits it where it happens, and the observer
  // answers only a width none of those saw. WebKit lays out a resized window
  // and delivers the observer before the resize event, so there the observer
  // fits it, and a region its fit made taller would be reported again in the
  // same frame, which the browser raises as an error: a region the observer
  // fitted is let go until the next frame, whose first report of it is the
  // width it was just fitted at.
  // eslint-disable-next-line no-restricted-syntax -- a region's width changed by nothing the page saw, which only a layout knows
  const fitted = new ResizeObserver((entries) => entries.forEach(({ target: region }) => {
    if (!region.isConnected) {
      fitted.unobserve(region);
      return;
    }
    const was = regionWidths.get(region);
    fitIn(region);
    if (regionWidths.get(region) === was) return;
    fitted.unobserve(region);
    requestAnimationFrame(() => fitted.observe(region));
  }));
  // The browser's find reaches what a closed row holds back: the line beneath
  // it is hidden until found, so find searches it and a match opens the row
  // (`beforematch`, below), and the stylesheet keeps the row such a line
  // stands in (tables.css). Set here, never by the server: a browser that
  // does not know the value hides the line for good.
  const findable = "onbeforematch" in document.documentElement;
  const holdBack = (detail) => {
    if (!findable || !detail?.matches(".data-table > tbody > tr.row-detail")) return;
    const open = detail.previousElementSibling?.classList.contains("is-expanded") ?? false;
    detail.querySelectorAll(":scope > td > *").forEach((part) => {
      if (open) part.removeAttribute("hidden");
      else if (part.getAttribute("hidden") !== "until-found") part.setAttribute("hidden", "until-found");
    });
  };
  // A line arrives as the parser or a script adds it, or a cell of it.
  const holdBackIn = (node) => {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const detail = node.closest("tr.row-detail");
    if (detail) holdBack(detail);
    else node.querySelectorAll("tr.row-detail").forEach(holdBack);
  };
  const watched = new WeakSet();
  const fitEvery = (changed = new Set()) => document.querySelectorAll(REGION).forEach((region) => {
    if (!watched.has(region)) {
      watched.add(region);
      fitted.observe(region);
    }
    fitIn(region, changed.has(region));
  });
  new MutationObserver((changes) => {
    if (findable) changes.forEach(({ addedNodes }) => addedNodes.forEach(holdBackIn));
    fitEvery(new Set(changes.map(({ target }) => target.closest?.(REGION))));
  }).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("resize", () => fitEvery());

  // A row can say more than it shows: a clamped name, an ellipsis, a list
  // folded behind "+5 more", a column that left. Such a row's toggle shows
  // all of it: every cut value in full, every folded part open, and the line
  // beneath the row that the server drew with what the row holds back. A
  // value cut short is measured (`markCut`); everything else a row holds
  // back the stylesheet reads from the HTML.
  // Screen-reader text is clipped to a pixel on purpose, and a decoration
  // hidden from assistive technology says nothing a reader could miss, so
  // neither makes a row "say more".
  const unseen = (el) => el.closest(".visually-hidden, [aria-hidden=true]") !== null;

  // A value is cut where its text is drawn past the edge of a box that clips
  // what it holds (`overflow: hidden` or `clip`), any box from the text up to
  // and including its cell; a box that scrolls is not cutting. Measured on
  // the text, which a layout places whole whatever clips it, so a run clipped
  // by a box around its chip, or by its cell, is cut too. The boxes that cut
  // each run, outermost last; the survey reads cut the same way (`CUTS`,
  // probes/describe.py).
  /* eslint-disable no-restricted-syntax -- a row whose value is cut short, which only a layout knows; markCut marks it and moves nothing */
  const cutRange = document.createRange();
  const clips = (value) => value === "hidden" || value === "clip";
  const textShown = (node) => {
    let box = node.parentElement;
    while (box.parentElement && getComputedStyle(box).display === "contents") box = box.parentElement;
    return box.checkVisibility({ visibilityProperty: true }) && getComputedStyle(node.parentElement).visibility === "visible";
  };
  const cutsIn = (cell) => {
    const found = [];
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.data.trim() || unseen(node.parentElement) || !textShown(node)) continue;
      cutRange.selectNodeContents(node);
      const rects = [...cutRange.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
      if (!rects.length) continue;
      const boxes = [];
      for (let up = node.parentElement; up; up = up.parentElement) {
        const style = getComputedStyle(up);
        if (style.display !== "inline" && style.display !== "contents") {
          const edge = up.getBoundingClientRect();
          const left = edge.left + up.clientLeft;
          const top = edge.top + up.clientTop;
          const x = clips(style.overflowX) && rects.some((r) => r.left < left - 1 || r.right > left + up.clientWidth + 1);
          const y = clips(style.overflowY) && rects.some((r) => r.top < top - 1 || r.bottom > top + up.clientHeight + 1);
          if (x || y) boxes.push(up);
        }
        if (up === cell) break;
      }
      if (boxes.length) found.push(boxes);
    }
    return found;
  };
  /* eslint-enable no-restricted-syntax */

  // A list whose server sorts it has links in its headings; any other table
  // whose rows are worth sorting has buttons that sort the rows on the page.
  // Both are drawn by the server (`table_heading`), in an extension's table
  // too (hand_drawn_tables). A press sorts ascending, the next descending,
  // the third puts the rows back as they came.
  // Rows keep to their own section, a section's heading row stays on top of
  // it, and an open row takes the line beneath it along.
  const UNIT_SECONDS = { second: 1, minute: 60, hour: 3600, day: 86400, week: 604800, month: 2629800, year: 31557600 };
  const EMPTY = /^[\s\u2013\u2014-]*$/;
  const sortValue = (cell) => {
    if (!cell) return null;
    if (cell.dataset.sort !== undefined) {
      const given = Number(cell.dataset.sort);
      return Number.isNaN(given) ? cell.dataset.sort.toLowerCase() : given;
    }
    const stamp = cell.querySelector("time[datetime]");
    if (stamp) {
      const when = Date.parse(stamp.getAttribute("datetime"));
      if (!Number.isNaN(when)) return when;
    }
    const copy = cell.cloneNode(true);
    copy.querySelectorAll(".visually-hidden, [data-row-toggle], [aria-hidden=true]").forEach((part) => part.remove());
    const text = copy.textContent.trim().replace(/\s+/g, " ");
    if (EMPTY.test(text)) return null;
    // A length of time, however it is phrased around the number.
    const span = text.match(/^(?:up |in |about )?(\d+(?:\.\d+)?)\s*(second|minute|hour|day|week|month|year)s?\b/i);
    if (span) return Number(span[1]) * UNIT_SECONDS[span[2].toLowerCase()];
    // An amount: a sign, a currency mark, digits, a unit after.
    const amount = text.replace(/\u2060/g, "").replace(/\u2212/g, "-").match(/^([-+]?)[$\u00a3\u20ac]?\s?(\d[\d,]*(?:\.\d+)?)\s*(?:[%a-z\u00b0/ ]{0,12})$/i);
    if (amount) return Number(amount[2].replace(/,/g, "")) * (amount[1] === "-" ? -1 : 1);
    if (/\d/.test(text) && text.length < 32 && /[/,:-]|[a-z]{3}/i.test(text)) {
      const when = Date.parse(text);
      if (!Number.isNaN(when)) return when;
    }
    return text.toLowerCase();
  };
  const compare = (left, right) => {
    if (typeof left === "number" && typeof right === "number") return left - right;
    return String(left).localeCompare(String(right), undefined, { numeric: true });
  };
  // How a table is laid out is the stylesheet's and the server's: which
  // columns show, where an identifier is cut, what a list folds and which
  // rows offer to open. Only whether a value is cut is a layout's to tell.
  // A row with a value cut short says more than it shows, which only a layout
  // can tell, so it is marked for the stylesheet to show its toggle in the
  // gutter the server's markup already keeps; the mark moves nothing.
  // Where its row's toggle does not show, a cut value reads whole on hover
  // instead: its title is its text, set while it is measured cut and taken
  // away once it is not, so no title outlives its cut. Hover text something
  // around it already carries (a title the server wrote, a tip) is left alone.
  const CUT_TITLE = "data-cut-title";
  // A chart's table is read whole and never opens a row.
  const marksCuts = (table) => table.parentElement?.matches(`${REGION}:not([data-chart])`)
    && !table.parentElement.parentElement?.classList.contains("chart-data");
  const plainText = (el) => el.textContent.replace(/[\u00ad\u200b\u2060]/g, "").replace(/\s+/g, " ").trim();
  const hoverText = (el) => {
    const said = el.closest(`[title]:not([${CUT_TITLE}]), [data-tip]`);
    return said ? (said.getAttribute("title") ?? said.dataset.tip) : "";
  };
  const titleCuts = (row, cut) => {
    const opens = [...row.querySelectorAll("[data-row-toggle]")].some((toggle) => toggle.checkVisibility());
    const outermost = cut.filter((el) => !cut.some((outer) => outer !== el && outer.contains(el)));
    const titled = new Set(opens ? [] : outermost.filter((el) => !hoverText(el).includes(plainText(el))));
    row.querySelectorAll(`[${CUT_TITLE}]`).forEach((el) => {
      if (titled.has(el)) return;
      el.removeAttribute("title");
      el.removeAttribute(CUT_TITLE);
    });
    titled.forEach((el) => {
      el.title = plainText(el);
      el.setAttribute(CUT_TITLE, "");
    });
  };
  const markCut = (table) => {
    if (!marksCuts(table)) return;
    table.querySelectorAll(":scope > tbody > tr:not(.row-detail)").forEach((row) => {
      // A run is titled on the outermost box that cuts it.
      const cut = row.classList.contains("is-expanded") ? [] : [...new Set([...row.querySelectorAll(":scope > :is(td, th)")].flatMap(cutsIn).map((boxes) => boxes.at(-1)))];
      if (!row.classList.contains("is-expanded")) row.toggleAttribute("data-cut", cut.length > 0);
      titleCuts(row, cut);
    });
  };
  const markCuts = () => document.querySelectorAll(`${REGION} > .data-table`).forEach(markCut);

  // Opening a row must not move the table. Left to itself the browser sizes
  // every column again around what the open row now shows whole, and each
  // row on the page shifts. So the columns are held at the widths they have,
  // for as long as any row is open, and let go when the last one closes or
  // the window changes. A fixed layout sizes its columns from its first row,
  // which in a table without headings may be a group's one-cell heading, so
  // the widths are held in a column group of their own, read from the
  // headings or else from the first row that has a cell in every column.
  const columnCells = (table) => {
    const headings = headingsOf(table);
    if (headings.length) return headings;
    const rows = [...table.querySelectorAll(":scope > tbody > tr")]
      .filter((row) => ![...row.cells].some((cell) => cell.colSpan > 1));
    const most = Math.max(0, ...rows.map((row) => row.cells.length));
    return [...(rows.find((row) => row.cells.length === most)?.cells ?? [])];
  };
  const heldColumns = (table) => table.querySelector(":scope > colgroup[data-held]");
  const holdColumns = (table) => {
    if ("rowsHeld" in table.dataset) return;
    // A column that left draws no box, and a column of the group stands for
    // each column that is drawn, in order.
    /* eslint-disable no-restricted-syntax -- every column at the width the reader saw while a row is open */
    const widths = columnCells(table).filter((cell) => cell.getClientRects().length)
      .map((cell) => cell.getBoundingClientRect().width);
    table.style.width = `${table.getBoundingClientRect().width}px`;
    /* eslint-enable no-restricted-syntax */
    const columns = document.createElement("colgroup");
    columns.dataset.held = "";
    columns.append(...widths.map((width) => {
      const column = document.createElement("col");
      if (width) column.style.width = `${width}px`;
      return column;
    }));
    if (table.caption) table.caption.after(columns);
    else table.prepend(columns);
    table.style.tableLayout = "fixed";
    table.dataset.rowsHeld = "";
  };
  const releaseColumns = (table) => {
    if (!("rowsHeld" in table.dataset)) return;
    heldColumns(table)?.remove();
    table.style.width = "";
    table.style.tableLayout = "";
    delete table.dataset.rowsHeld;
  };

  // Opens or closes a row's own line. `button` is its toggle, which a row
  // that heads a fold has none of.
  const setExpanded = (row, expanding, button = null) => {
    const table = row.closest("table");
    // Held before anything in the row changes, so what is held is the table
    // as the reader was looking at it.
    if (expanding) holdColumns(table);
    row.classList.toggle("is-expanded", expanding);
    holdBack(row.nextElementSibling);
    if (!table.querySelector(":scope > tbody > tr.is-expanded")) {
      releaseColumns(table);
      refit(table);
    }
    // An open row shows its values whole, so it keeps no title for a cut;
    // closed again, it is measured for them.
    markCut(table);
    button?.setAttribute("aria-expanded", String(expanding));
    button?.setAttribute("aria-label", expanding ? "Show less of this row" : "Show all of this row");
    // Open what was folded, and on the way back close only what this opened.
    row.querySelectorAll(":is(td, th) details").forEach((details) => {
      if (expanding && !details.open) {
        details.open = true;
        details.dataset.rowOpened = "";
      } else if (!expanding && "rowOpened" in details.dataset) {
        details.open = false;
        delete details.dataset.rowOpened;
      }
    });
  };
  document.addEventListener("click", (event) => {
    const button = event.target.closest(".row-expand");
    if (!button) return;
    const row = button.closest("tr");
    setExpanded(row, !row.classList.contains("is-expanded"), button);
  });

  // Find matched in a closed row's line: the row opens, so the match shows
  // where it belongs.
  document.addEventListener("beforematch", (event) => {
    const toggle = event.target.closest("tr.row-detail")?.previousElementSibling?.querySelector(".row-expand");
    if (toggle && toggle.getAttribute("aria-expanded") !== "true") toggle.click();
  });

  // A row that heads rows folded under it (`data-folds`, its rows naming it
  // in `data-folded`) opens and closes them from anywhere on it but its own
  // controls. Closing it closes any of its rows that were open, so no line is
  // left beneath a row that is not there. Its fold stands where its own
  // toggle would, so what its columns that left hold opens with its rows.
  const foldedUnder = (head) =>
    head.closest("tbody").querySelectorAll(`:scope > tr[data-folded="${CSS.escape(head.id)}"]`);
  const setFold = (head, opening) => {
    head.querySelector("[data-fold-toggle]")?.setAttribute("aria-expanded", String(opening));
    const line = head.nextElementSibling?.matches(".row-detail") ? head.nextElementSibling : null;
    if (head.classList.contains("is-expanded") ? !opening : opening && line?.querySelector("[data-dropped]")) {
      setExpanded(head, opening);
    }
    foldedUnder(head).forEach((row) => {
      if (!opening && row.classList.contains("is-expanded")) row.querySelector(".row-expand")?.click();
      row.toggleAttribute("data-open", opening);
    });
    refit(head.closest("table"));
    // Measured at once, so each row it shows offers its toggle as it appears.
    markTables();
  };
  document.addEventListener("click", (event) => {
    const head = event.target.closest("tr[data-folds]");
    if (!head || event.defaultPrevented) return;
    const toggle = event.target.closest("[data-fold-toggle]");
    if (!toggle && event.target.closest("a, button, input, select, label, [data-origin]")) return;
    setFold(head, head.querySelector("[data-fold-toggle]")?.getAttribute("aria-expanded") !== "true");
  });

  // A link that names a row lands on it open: inside its fold, and showing
  // what it holds back. A fragment that is not percent-encoding names no row.
  const fragmentId = () => {
    try {
      return decodeURIComponent(window.location.hash.slice(1));
    } catch {
      return "";
    }
  };
  const landOnRow = () => {
    const id = fragmentId();
    const row = id && document.getElementById(id);
    if (!(row instanceof HTMLTableRowElement) || !row.closest(".data-table")) return;
    const head = row.dataset.folded && document.getElementById(row.dataset.folded);
    if (head && !row.hasAttribute("data-open")) setFold(head, true);
    else markTables();
    const toggle = row.querySelector(".row-expand");
    if (toggle && toggle.getAttribute("aria-expanded") !== "true") toggle.click();
    row.scrollIntoView({ block: "start" });
  };
  window.addEventListener("hashchange", landOnRow);

  const cameIn = new WeakMap();

  const sortRows = (table, column, direction) => {
    table.querySelectorAll(":scope > tbody").forEach((body) => {
      const units = [];
      const fixed = [];
      [...body.children].forEach((row) => {
        if (row.classList.contains("row-detail") && units.length) units[units.length - 1].rows.push(row);
        else if (row.classList.contains("row-group") || row.querySelector(":scope > [colspan]")) fixed.push(row);
        else {
          if (!cameIn.has(row)) cameIn.set(row, cameIn.get(body) ?? 0), cameIn.set(body, (cameIn.get(body) ?? 0) + 1);
          units.push({ rows: [row], value: sortValue(row.children[column]), came: cameIn.get(row) });
        }
      });
      const sign = direction === "descending" ? -1 : 1;
      units.sort((left, right) => {
        if (direction === "none") return left.came - right.came;
        // Nothing to compare sorts last in both directions.
        if (left.value === null || right.value === null) return (left.value === null) - (right.value === null) || left.came - right.came;
        return sign * compare(left.value, right.value) || left.came - right.came;
      });
      body.append(...fixed, ...units.flatMap((unit) => unit.rows));
    });
  };

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-sort-local]");
    if (!button) return;
    const heading = button.closest("th");
    const table = heading.closest("table");
    const next = { none: "ascending", ascending: "descending", descending: "none" }[heading.getAttribute("aria-sort")] || "ascending";
    table.querySelectorAll(":scope > thead th[data-sort-ready]").forEach((other) => other.setAttribute("aria-sort", "none"));
    heading.setAttribute("aria-sort", next);
    sortRows(table, heading.cellIndex, next);
  });

  // A table held while its rows are open is let go and fitted again at the
  // width the window now gives it, which holds it again (`fitIn`).
  const settleOpenRows = () => {
    document.querySelectorAll(".data-table[data-rows-held]").forEach((table) => {
      releaseColumns(table);
      refit(table);
    });
  };

  const markTables = () => {
    settleOpenRows();
    markCuts();
  };
  let rowsTimer = null;
  const markTablesSoon = () => {
    window.clearTimeout(rowsTimer);
    rowsTimer = window.setTimeout(markTables, 120);
  };

  window.addEventListener("resize", markTablesSoon);
  // Whether a value is cut depends on the face it is drawn in, and so does
  // how wide a table is in a region whose width did not change, so both are
  // asked again when the faces are in.
  document.fonts?.ready.then(() => {
    document.querySelectorAll(REGION).forEach((region) => fitIn(region, true));
    markTables();
  });

  const persistSelection = () => {
    try {
      if (selectedIds.size) {
        window.sessionStorage.setItem(selectionKey, JSON.stringify([...selectedIds]));
      } else {
        window.sessionStorage.removeItem(selectionKey);
      }
    } catch {
      // Storage can be unavailable (private mode); in-memory selection still works.
    }
  };

  function initializeSelection() {
    document.querySelectorAll("[data-selectable-table]").forEach((table) => {
      if (table.dataset.selectionReady) return;
      table.dataset.selectionReady = "true";
      const rows = [...table.querySelectorAll("[data-row-select]")];
      const selectAll = table.querySelector("[data-select-all]");
      const bar = table.closest("main").querySelector("[data-table-selection]");
      if (!rows.length || !selectAll || !bar) return;

      const update = () => {
        rows.forEach((input) => { input.checked = selectedIds.has(input.value); });
        const visible = rows.filter((input) => input.checked).length;
        bar.hidden = selectedIds.size === 0;
        bar.querySelector("[data-selection-count]").textContent = selectedIds.size;
        selectAll.checked = visible === rows.length;
        selectAll.indeterminate = visible > 0 && visible < rows.length;
        persistSelection();
      };
      selectAll.addEventListener("change", () => {
        rows.forEach((input) => {
          if (selectAll.checked) selectedIds.add(input.value);
          else selectedIds.delete(input.value);
        });
        update();
      });
      rows.forEach((input) => input.addEventListener("change", () => {
        if (input.checked) selectedIds.add(input.value);
        else selectedIds.delete(input.value);
        update();
      }));
      bar.querySelector("[data-clear-selected]").addEventListener("click", () => {
        selectedIds.clear();
        update();
      });
      bar.querySelector("[data-copy-selected]").addEventListener("click", async () => {
        await navigator.clipboard.writeText([...selectedIds].join("\n"));
      });
      // Re-apply the persisted selection to freshly rendered rows.
      update();
    });
  }

  function tableUrl(form) {
    const params = new URLSearchParams(new FormData(form));
    return `${window.location.pathname}?${params.toString()}`;
  }

  // The parts of a list page that a search, a sort or a page turn changes,
  // each one that every answer has: a list emptied by its filters still has
  // its results, holding the empty state, so clearing them brings the table
  // back.
  const TABLE_PARTS = [
    "[data-table-toolbar]",
    "[data-table-selection]",
    "[data-table-results]",
    "[data-search-results]",
  ];

  // The fetch, the swap and focus are the fragment primitive's; the address
  // bar is the navigation's, already at `url`. The table it brings in is
  // fitted, before its first frame, as every change to the page is.
  async function refreshTable(url) {
    try {
      await hqFragment.swap(TABLE_PARTS, {
        url,
        strict: false,
        busy: document.querySelector("main"),
        title: true,
      });
      initializeSelection();
      markTables();
    } catch (error) {
      if (error.name !== "AbortError") window.location.assign(url);
    }
  }

  // Marks the navigation a typed search asks for, which replaces the search
  // before it rather than adding to Back.
  const TABLE = Symbol("table");
  const formOf = (element) => (element instanceof HTMLFormElement ? element : element?.form);

  // A list's links, its toolbar and Back and Forward between its answers are
  // navigations this page answers in place. The history API's own writes, a
  // fragment's (`hashChange`) and every other navigation are the browser's.
  const answersInPlace = (event) => {
    if (!event.canIntercept || event.hashChange || event.downloadRequest !== null) return false;
    if (event.info === TABLE) return true;
    if (event.navigationType === "traverse") return Boolean(document.querySelector("[data-table-results]"));
    const source = event.sourceElement;
    return Boolean(
      source?.matches("a.table-sort-link, .pagination a") || formOf(source)?.matches("[data-table-toolbar]"),
    );
  };

  window.navigation?.addEventListener("navigate", (event) => {
    if (!answersInPlace(event)) return;
    const url = event.destination.url;
    // Focus stays on what was pressed and the page where it was read; a
    // traverse is put back where the entry was left once its rows are in.
    const scroll = event.navigationType === "traverse" ? "after-transition" : "manual";
    event.intercept({ focusReset: "manual", scroll, handler: () => refreshTable(url) });
  });

  document.addEventListener("input", (event) => {
    const input = event.target.closest("[data-table-toolbar] input[type=search]");
    if (!input) return;
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      // The toolbar may have been replaced since this timer was scheduled;
      // read the live form so a stale query is never sent.
      const form = input.isConnected
        ? input.form
        : document.querySelector("[data-table-toolbar]");
      if (form) window.navigation?.navigate(tableUrl(form), { history: "replace", info: TABLE });
    }, 250);
  });

  // A box ticked or an option chosen asks at once, as the form's own submit.
  document.addEventListener("change", (event) => {
    const control = event.target.closest(
      "[data-table-toolbar] input[type=checkbox], [data-table-toolbar] select",
    );
    control?.form.requestSubmit();
  });

  // "/" focuses search from anywhere on a list page, GitHub-style.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target.closest("input, textarea, select, [contenteditable]")) return;
    const search = document.querySelector("[data-table-toolbar] input[type=search]");
    if (!search) return;
    event.preventDefault();
    search.focus();
    search.select();
  });

  document.addEventListener("DOMContentLoaded", () => {
    initializeSelection();
    markTables();
    landOnRow();
  });
})();
