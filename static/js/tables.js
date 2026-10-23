(() => {
  let searchTimer = null;
  let tableLocation = `${window.location.pathname}${window.location.search}`;

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
  // wider, what keeps its line whole gives way too (`data-tight`); as the
  // region grows they come back, the last to leave first, each only if the
  // table still fits. A row's toggle shows what left. Only the browser
  // knows how wide a table is, and a ResizeObserver tells it in the frame it
  // is laid out, before that frame is painted: this script loads in the head
  // without `defer`, so every region is watched from the moment it is parsed
  // and nothing moves once a page is drawn.
  const REGION = ".table-scroll";
  const headingsOf = (table) => [...table.querySelectorAll(":scope > thead > tr:last-child > th")];
  // The order the table's columns leave in, by place.
  const dropOrder = (table) => headingsOf(table)
    .map((heading, place) => [Number(heading.dataset.drop) || 0, place, heading])
    .filter(([rank, , heading]) => rank && !heading.hasAttribute("data-blank-column"))
    .sort((left, right) => left[0] - right[0] || right[1] - left[1])
    .map(([, place]) => place);
  // How far each table has given way: how many of its columns have left, one
  // more once it is in a narrow box, and one more once it is tight.
  const gaveWay = new WeakMap();
  const giveWay = (table, steps) => {
    const order = dropOrder(table);
    const left = new Set(order.slice(0, steps));
    const before = new Set(order.slice(0, gaveWay.get(table) ?? 0));
    // Only the cells of a column that left or came back are marked again.
    const changed = order.filter((place) => left.has(place) !== before.has(place));
    const width = headingsOf(table).length;
    if (changed.length) {
      table.querySelectorAll(":scope > * > tr").forEach((row) => {
        if (row.children.length !== width) return;
        changed.forEach((place) => row.children[place].toggleAttribute("data-dropped", left.has(place)));
      });
    }
    table.toggleAttribute("data-narrow", steps > order.length);
    table.toggleAttribute("data-tight", steps > order.length + 1);
    // The columns that share the region's width (tables.css, `--share`).
    const sharing = headingsOf(table).filter((heading) => !left.has(heading.cellIndex)
      && !heading.matches(".select-column, [data-blank-column]")).length;
    table.style.setProperty("--showing", Math.min(Math.max(sharing, 1), 16));
    gaveWay.set(table, steps);
  };
  // A column is never narrower than the longest part of what its cells say
  // that cannot break: a run of a name between its seams and hyphens, a word.
  // An open row lifts every cut and holds its columns where they were, so a
  // narrower column would break such a part where it has no seam, and fitting
  // the table again then would move it. Free text breaks anywhere, so it asks
  // for nothing. Measured in the face it is drawn in, since an engine lays out
  // none of a cut text past its ellipsis; each heading holds its column's
  // floor (`--part-floor`), so the fit counts it before a row opens.
  const PART = /[^\s-]+-*|-+/g;
  const pen = document.createElement("canvas").getContext("2d");
  const drawnWidth = (words, style) => {
    pen.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    const spacing = parseFloat(style.letterSpacing) || 0;
    return pen.measureText(words).width + spacing * words.length;
  };
  // What lies between a text and its cell's content edges: each box's
  // padding and border. The cell's own padding is its heading's too (the
  // gutter's rules hold both), so the floor is the heading's content width.
  const insets = (el, cell) => {
    let sum = 0;
    for (let box = el; box !== cell; box = box.parentElement) {
      const style = getComputedStyle(box);
      sum += parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
        + parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
    }
    return sum;
  };
  // Whether a box inside the cell hides the words: the cell itself may be a
  // column that left or a row folded away, which still asks for its floor.
  const hiddenIn = (el, cell) => {
    for (let box = el; box !== cell; box = box.parentElement) {
      if (getComputedStyle(box).display === "none") return true;
    }
    return false;
  };
  const longestPart = (cell) => {
    let most = 0;
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode(); text; text = walker.nextNode()) {
      const holder = text.parentElement;
      if (unseen(holder) || holder.closest("[data-row-toggle], .cell-data")) continue;
      const words = text.data.match(PART);
      if (!words || hiddenIn(holder, cell)) continue;
      const style = getComputedStyle(holder);
      const widest = Math.max(...words.map((word) => drawnWidth(word, style)));
      most = Math.max(most, widest + insets(holder, cell));
    }
    return most;
  };
  const floorParts = (table) => {
    const headings = headingsOf(table);
    const most = headings.map(() => 0);
    table.querySelectorAll(":scope > tbody > tr:not(.row-detail)").forEach((row) => {
      if (row.children.length !== headings.length) return;
      [...row.children].forEach((cell, place) => {
        most[place] = Math.max(most[place], longestPart(cell));
      });
    });
    headings.forEach((heading, place) => heading.style.setProperty("--part-floor", `${Math.ceil(most[place])}px`));
  };
  const overflows = (table) => table.offsetWidth > table.parentElement.clientWidth;
  // A hidden region has no width to fit.
  const shown = (region) => region.clientWidth > 0;
  // A table that overflows gives way until it fits. One that fits takes back
  // what left, the last first, while it still fits; a region that only
  // narrowed has nothing to give back, so that is not tried.
  const fit = (table, mayReturn = true) => {
    const was = gaveWay.get(table) ?? 0;
    const most = dropOrder(table).length + 2;
    floorParts(table);
    let steps = Math.min(was, most);
    if (steps !== was) giveWay(table, steps);
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
  // A table whose columns are held while a row is open is fitted as it lets
  // them go.
  const refit = (table) => {
    if (table && shown(table.parentElement) && !("rowsHeld" in table.dataset)) fit(table);
  };
  // The region is an inline-size container, so its width never follows the
  // table's: it is the one thing watched, and only a change of its width
  // fits again. What changes a table's content (its faces arriving, a swap,
  // a fold, a row closing) fits it where it happens.
  const regionWidths = new WeakMap();
  const fitted = new ResizeObserver((entries) => entries.forEach(({ target: region, contentBoxSize }) => {
    if (!region.isConnected) {
      fitted.unobserve(region);
      return;
    }
    const width = contentBoxSize[0].inlineSize;
    const before = regionWidths.get(region);
    if (width === before) return;
    regionWidths.set(region, width);
    const table = tableIn(region);
    if (table && width > 0 && !("rowsHeld" in table.dataset)) fit(table, before === undefined || width > before);
  }));
  new MutationObserver((changes) => changes.forEach(({ addedNodes }) => addedNodes.forEach((node) => {
    if (!(node instanceof Element)) return;
    if (node.matches(REGION)) fitted.observe(node);
    node.querySelectorAll(REGION).forEach((region) => fitted.observe(region));
  }))).observe(document.documentElement, { childList: true, subtree: true });

  // A row can say more than it shows: a clamped name, an ellipsis, a list
  // folded behind "+5 more". Such a row's toggle shows all of it, every
  // cut-off value in full and every folded part open, and puts it back. The
  // parts keep their own toggles too; this is the row's, not a replacement.
  // A value cut short is measured (`markCut`); everything else a row holds
  // back the stylesheet reads from the HTML.
  // Screen-reader text is clipped to a pixel on purpose, and a decoration
  // hidden from assistive technology says nothing a reader could miss, so
  // neither makes a row "say more".
  const unseen = (el) => el.closest(".visually-hidden, [aria-hidden=true]") !== null;

  const isCut = (el) => {
    if (unseen(el)) return false;
    const style = getComputedStyle(el);
    if (style.overflowX === "visible" && style.overflowY === "visible") return false;
    return el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
  };

  // Cells of this row that are not shown at all: a column a narrow table
  // dropped. One without words says nothing to list, so it is not something
  // held back.
  const droppedCells = (row) =>
    [...row.children].filter(
      (cell) =>
        cell.tagName === "TD"
        && getComputedStyle(cell).display === "none"
        && !cell.hasAttribute("data-blank")
        && cell.textContent.trim() !== "",
    );

  // Within a cell that is showing, what a narrow table holds back.
  const heldBack = (cell) =>
    [...cell.querySelectorAll(".narrow-more")].filter(
      (el) => getComputedStyle(el).display === "none",
    );

  // A list whose server sorts it has links in its headings; any other table
  // whose rows are worth sorting has buttons that sort the rows on the page.
  // Both are drawn by the server (`table_heading`). A press sorts
  // ascending, the next descending, the third puts the rows back as they came.
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
    const amount = text.replace(/\u2212/g, "-").match(/^([-+]?)[$\u00a3\u20ac]?\s?(\d[\d,]*(?:\.\d+)?)\s*(?:[%a-z\u00b0/ ]{0,12})$/i);
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
  const markCut = (table) => {
    table.querySelectorAll(":scope > tbody > tr:not(.row-detail):not(.is-expanded)").forEach((row) => {
      row.toggleAttribute("data-cut", [...row.querySelectorAll(":is(td, th) *")].some(isCut));
    });
  };
  // A chart's table is read whole and never opens a row.
  const markCuts = () => {
    document.querySelectorAll(".table-scroll:not([data-chart]) > .data-table").forEach((table) => {
      if (!table.parentElement.parentElement?.classList.contains("chart-data")) markCut(table);
    });
  };

  // Opening a row must not move the table. Left to itself the browser sizes
  // every column again around whatever the open row now shows, and each row
  // on the page shifts. So the columns are held at the widths they have, for
  // as long as any row is open, and let go when the last one closes or the
  // window changes.
  const holdColumns = (table) => {
    if ("rowsHeld" in table.dataset) return;
    const headings = [...table.querySelectorAll(":scope > thead > tr:last-child > th")];
    if (!headings.length) return;
    const widths = headings.map((heading) => heading.getBoundingClientRect().width);
    table.style.width = `${table.getBoundingClientRect().width}px`;
    headings.forEach((heading, index) => {
      if (widths[index]) heading.style.width = `${widths[index]}px`;
    });
    table.style.tableLayout = "fixed";
    table.dataset.rowsHeld = "";
  };
  const releaseColumns = (table) => {
    if (!("rowsHeld" in table.dataset)) return;
    table.querySelectorAll(":scope > thead > tr:last-child > th").forEach((heading) => {
      heading.style.width = "";
    });
    table.style.width = "";
    table.style.tableLayout = "";
    delete table.dataset.rowsHeld;
  };

  // What an open row was holding back, in a line of its own beneath it and
  // the full width of the table: the columns a narrow table dropped, and the
  // parts of the columns that stayed, each under its column's name. Beneath,
  // not inside, so the row itself stays exactly as it was. Built when the row
  // opens and removed when it closes, so it is never stale and never a second
  // copy of something that is showing.
  // A heading's words, without the sort arrow beside them.
  const headingName = (heading) => {
    if (!heading) return "";
    const copy = heading.cloneNode(true);
    copy.querySelectorAll("[aria-hidden=true]").forEach((mark) => mark.remove());
    return copy.textContent.trim().replace(/\s+/g, " ");
  };

  // What the line beneath shows is the row's own content, moved there and
  // moved back when it closes, never a copy: a control copied into the line
  // would be a second control of the same name in the same form.
  const restore = new WeakMap();
  const closeDetail = (row) => {
    const detail = row.nextElementSibling;
    if (!detail?.classList.contains("row-detail")) return;
    restore.get(detail)?.forEach((putBack) => putBack());
    detail.remove();
  };
  const openDetail = (row) => {
    closeDetail(row);
    const table = row.closest("table");
    const headings = [...table.querySelectorAll(":scope > thead > tr:last-child > th")];
    const cells = [...row.children];
    const dropped = droppedCells(row);
    const parts = [];
    const undo = [];
    cells.forEach((cell, index) => {
      // A column of controls has no heading to read; it is still named here.
      const name = headingName(headings[index])
        || (cell.querySelector("a, button, select, input") ? "Actions" : "");
      if (dropped.includes(cell)) {
        const nodes = [...cell.childNodes];
        parts.push([name, nodes, cell.classList.contains("cell-data")]);
        undo.push(() => cell.append(...nodes));
        return;
      }
      const held = heldBack(cell);
      if (held.length) {
        parts.push([name, held]);
        held.forEach((el) => {
          const place = document.createComment("");
          el.replaceWith(place);
          el.classList.remove("narrow-more");
          undo.push(() => {
            el.classList.add("narrow-more");
            place.replaceWith(el);
          });
        });
      }
    });
    // What the row keeps for this line at any width. A part that names itself
    // (`data-label`) joins the named list, so the line reads as one set of
    // facts on one edge; any other is set in under the cell it came from,
    // where its words started.
    const rowLeft = row.getBoundingClientRect().left;
    row.querySelectorAll(":is(td, th) .row-below[data-label]").forEach((el) => {
      const place = document.createComment("");
      el.replaceWith(place);
      undo.push(() => place.replaceWith(el));
      parts.push([el.dataset.label, [el]]);
    });
    const below = [...row.querySelectorAll(":is(td, th) .row-below")].map((el) => {
      const from = el.closest("td, th");
      const indent = from.getBoundingClientRect().left + parseFloat(getComputedStyle(from).paddingLeft) - rowLeft;
      const place = document.createComment("");
      el.replaceWith(place);
      undo.push(() => {
        el.style.removeProperty("margin-inline-start");
        place.replaceWith(el);
      });
      return [el, indent];
    });
    if (!parts.length && !below.length) return;
    const cell = document.createElement("td");
    cell.colSpan = cells.length;
    cell.append(...below.map(([el]) => el));
    if (parts.length) {
      const list = document.createElement("dl");
      list.className = "row-columns";
      parts.forEach(([name, nodes, words]) => {
        const term = document.createElement("dt");
        term.textContent = name;
        const value = document.createElement("dd");
        // A cell of free text keeps its rule where its words go.
        if (words) value.className = "cell-data";
        value.append(...nodes);
        list.append(term, value);
      });
      cell.append(list);
    }
    const detail = document.createElement("tr");
    detail.className = "row-detail";
    // Named by attribute as well, for anything that has to tell a record from
    // the line beneath it without knowing how either is styled.
    detail.dataset.rowDetail = "";
    detail.append(cell);
    restore.set(detail, undo);
    row.after(detail);
    // A name over nothing that shows (a column whose words moved to a part of
    // their own, a preview the open row does without) is not drawn.
    detail.querySelectorAll(".row-columns > dd").forEach((value) => {
      const shows = [...value.childNodes].some((node) => (node.nodeType === Node.TEXT_NODE
        ? node.textContent.trim() !== ""
        : node.nodeType === Node.ELEMENT_NODE && node.getBoundingClientRect().height > 0));
      if (shows) return;
      value.previousElementSibling?.remove();
      value.hidden = true;
    });
    const start = cell.getBoundingClientRect().left + parseFloat(getComputedStyle(cell).paddingLeft) - rowLeft;
    below.forEach(([el, indent]) => el.style.setProperty("margin-inline-start", `${Math.max(0, indent - start)}px`));
  };

  document.addEventListener("click", (event) => {
    const button = event.target.closest(".row-expand");
    if (!button) return;
    const row = button.closest("tr");
    const expanding = !row.classList.contains("is-expanded");
    const table = row.closest("table");
    // Held before anything in the row changes, so what is held is the table
    // as the reader was looking at it.
    if (expanding) {
      holdColumns(table);
      openDetail(row);
    } else {
      closeDetail(row);
    }
    row.classList.toggle("is-expanded", expanding);
    if (!table.querySelector(":scope > tbody > tr.is-expanded")) {
      releaseColumns(table);
      refit(table);
    }
    button.setAttribute("aria-expanded", String(expanding));
    button.setAttribute("aria-label", expanding ? "Show less of this row" : "Show all of this row");
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
  });

  // A row that heads rows folded under it (`data-folds`, its rows naming it
  // in `data-folded`) opens and closes them from anywhere on it but its own
  // controls. Closing it closes any of its rows that were open, so no line is
  // left beneath a row that is not there.
  const foldedUnder = (head) =>
    head.closest("tbody").querySelectorAll(`:scope > tr[data-folded="${CSS.escape(head.id)}"]`);
  const setFold = (head, opening) => {
    head.querySelector("[data-fold-toggle]")?.setAttribute("aria-expanded", String(opening));
    foldedUnder(head).forEach((row) => {
      if (!opening && row.classList.contains("is-expanded")) row.querySelector(".row-expand")?.click();
      row.toggleAttribute("data-open", opening);
    });
    refit(head.closest("table"));
    markTablesSoon();
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
    // Measured now, since the row may only just have been shown.
    markTables();
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

  // A table drawn through the table tags has its sort controls in the HTML
  // (`table_heading local_sort=…`), and its headings declare a kind. One drawn
  // by hand, as an extension's template still does, gets them here.
  const HAND_DRAWN = ".table-scroll > .data-table:not(:has(> thead th[data-kind]))";
  function markSortable() {
    document.querySelectorAll(HAND_DRAWN).forEach((table) => {
      if (table.parentElement.hasAttribute("data-chart") || table.parentElement.parentElement?.classList.contains("chart-data")) return;
      const headings = [...table.querySelectorAll(":scope > thead > tr:last-child > th")];
      if (!headings.length || table.querySelector(":scope > thead a.table-sort-link")) return;
      if (table.querySelector(":scope > thead > tr:only-child") === null) return;
      if (table.querySelector(":scope > tbody > tr > [rowspan]")) return;
      const records = table.querySelectorAll(":scope > tbody > tr:not(.row-group):not(.row-detail)");
      if (records.length < 2) return;
      headings.forEach((heading) => {
        if ("sortReady" in heading.dataset || heading.colSpan > 1) return;
        const name = headingName(heading);
        if (!name || heading.querySelector("input, button, a, select")) return;
        heading.dataset.sortReady = "";
        heading.setAttribute("aria-sort", "none");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "table-sort-link";
        button.dataset.sortLocal = "";
        button.append(...heading.childNodes);
        const mark = document.createElement("span");
        mark.className = "sort-indicator";
        mark.setAttribute("aria-hidden", "true");
        button.append(mark);
        button.setAttribute("aria-label", `Sort by ${name}`);
        heading.append(button);
      });
      refit(table);
    });
  }

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

  let rowsTimer = null;
  // A window that changes size changes what shows, so widths held for the old
  // size are let go and taken again, and each open row lists what is held
  // back now, which may be nothing if its columns came back.
  const settleOpenRows = () => {
    document.querySelectorAll(".data-table[data-rows-held]").forEach((table) => {
      releaseColumns(table);
      const open = [...table.querySelectorAll(":scope > tbody > tr.is-expanded")];
      open.forEach((row) => {
        row.classList.remove("is-expanded");
        closeDetail(row);
      });
      // Fitted as it stands with every row closed, which is what is held.
      refit(table);
      if (!open.length) return;
      holdColumns(table);
      open.forEach((row) => {
        openDetail(row);
        row.classList.add("is-expanded");
      });
    });
  };

  const markTables = () => {
    settleOpenRows();
    markSortable();
    markCuts();
  };
  const markTablesSoon = () => {
    window.clearTimeout(rowsTimer);
    rowsTimer = window.setTimeout(markTables, 120);
  };


  // A filter's menu that was open when one of its options was ticked is open
  // in the answer too, so the next option is a press away and the keyboard
  // stays on the box it ticked.
  function keepFilterOpen(next) {
    document.querySelectorAll("[data-table-toolbar] details[data-menu][open] input[name]").forEach((input) => {
      const menu = next.querySelector(`[data-table-toolbar] input[name="${CSS.escape(input.name)}"]`)?.closest("details[data-menu]");
      if (menu) menu.open = true;
    });
  }

  function preserveDisclosureState(next, url) {
    const currentQuery = new URL(window.location.href).searchParams.get("q");
    const nextQuery = new URL(url, window.location.href).searchParams.get("q");
    if (!currentQuery || !nextQuery) return;
    document.querySelectorAll("details[data-preserve-open]").forEach((details) => {
      const key = details.dataset.preserveOpen;
      const replacement = next.querySelector(`[data-preserve-open="${key}"]`);
      if (replacement) replacement.open = details.open;
    });
  }

  window.addEventListener("resize", markTablesSoon);
  // Whether a value is cut depends on the face it is drawn in, so it is asked
  // again when the faces are in.
  // So is how wide a name's longest part is, which a table sized the same
  // as before is not observed to change.
  document.fonts?.ready.then(() => {
    document.querySelectorAll(REGION).forEach((region) => refit(tableIn(region)));
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

  // Auto table layout recomputes column widths from whichever rows are
  // visible, so sorting or paging makes columns jitter. The incoming table is
  // pinned to the current widths while it is swapped in, and let go once it
  // is in, so it fits its region again as the window changes.
  const pinned = new WeakSet();
  const unpin = (table) => {
    if (!pinned.has(table)) return;
    pinned.delete(table);
    table.querySelectorAll("thead th").forEach((heading) => heading.style.removeProperty("width"));
    table.style.removeProperty("table-layout");
    refit(table);
  };
  function pinColumnWidths(next) {
    const currentTables = document.querySelectorAll(".table-scroll table");
    const nextTables = next.querySelectorAll(".table-scroll table");
    currentTables.forEach((table, tableIndex) => {
      const nextTable = nextTables[tableIndex];
      if (!nextTable) return;
      const currentHeads = table.querySelectorAll("thead th");
      const nextHeads = nextTable.querySelectorAll("thead th");
      if (!currentHeads.length || currentHeads.length !== nextHeads.length) return;
      giveWay(nextTable, gaveWay.get(table) ?? 0);
      currentHeads.forEach((th, index) => {
        const width = th.getBoundingClientRect().width;
        if (width) nextHeads[index].style.width = `${width}px`;
      });
      nextTable.style.tableLayout = "fixed";
      pinned.add(nextTable);
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

  // The fetch, the swap, focus and the address bar are the fragment
  // primitive's. This adds only what a table knows: the columns it is holding.
  async function refreshTable(url, { history = "push" } = {}) {
    try {
      await hqFragment.swap(TABLE_PARTS, {
        url,
        strict: false,
        busy: document.querySelector("main"),
        before: (next) => {
          preserveDisclosureState(next, url);
          keepFilterOpen(next);
          pinColumnWidths(next);
        },
        title: true,
        history,
      });
      tableLocation = `${window.location.pathname}${window.location.search}`;
      document.querySelectorAll(".table-scroll table").forEach(unpin);
      initializeSelection();
      markTables();
    } catch (error) {
      if (error.name !== "AbortError") window.location.assign(url);
    }
  }

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-table-toolbar]");
    if (!form) return;
    event.preventDefault();
    refreshTable(tableUrl(form));
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
      if (form) refreshTable(tableUrl(form), { history: "replace" });
    }, 250);
  });

  document.addEventListener("change", (event) => {
    const control = event.target.closest(
      "[data-table-toolbar] input[type=checkbox], [data-table-toolbar] select",
    );
    if (control) refreshTable(tableUrl(control.form));
  });

  document.addEventListener("click", (event) => {
    // Leave modified clicks (new tab, window, download) to the browser.
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest("a.table-sort-link, .pagination a");
    if (!link) return;
    event.preventDefault();
    refreshTable(link.href);
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

  window.addEventListener("popstate", () => {
    const nextLocation = `${window.location.pathname}${window.location.search}`;
    // The mobile navigation and other lightweight disclosures use fragments.
    // Chromium emits popstate for those history entries too; a fragment cannot
    // change table data, so it must never trigger a fetch or DOM replacement.
    if (nextLocation === tableLocation) return;
    tableLocation = nextLocation;
    refreshTable(window.location.href, { history: "none" });
  });

  document.addEventListener("DOMContentLoaded", () => {
    // Every row is in now, which a region watched since it was parsed may
    // not have held when it was first fitted.
    document.querySelectorAll(REGION).forEach((region) => refit(tableIn(region)));
    initializeSelection();
    markTables();
    landOnRow();
  });
})();
