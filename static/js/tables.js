(() => {
  let requestController = null;
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

  // A bounded scroll region has to be operable from the keyboard, and a plain
  // `div` that scrolls is not focusable. That is all this helper does: the
  // heading itself is `position: sticky` in the stylesheet, so the browser
  // pins it on the compositor and there is nothing here to measure.
  //
  // `is-pane` marks a wrapper that is genuinely its own scroll box: one
  // holding a table too wide to fit, or one an author capped to a reading
  // height. Those pin the heading to the box; everything else pins it to the
  // viewport, and the stylesheet reads this class to tell them apart.
  function markScrollRegions() {
    document.querySelectorAll(".table-scroll").forEach((wrapper) => {
      const table = wrapper.querySelector(".data-table");
      // Measured off the table, not the wrapper: the wrapper only reports a
      // scrollable width while it is already a scroll container, and whether it
      // should be one is exactly the question.
      const capped = getComputedStyle(wrapper).maxHeight !== "none";
      const tooWide = !!table && table.scrollWidth > wrapper.clientWidth + 1;
      const scrolls = capped || tooWide;
      wrapper.classList.toggle("is-pane", scrolls);
      wrapper.classList.toggle("is-fitted", !!table && !scrolls);
      if (scrolls) {
        if (!wrapper.hasAttribute("tabindex")) wrapper.tabIndex = 0;
        if (!wrapper.hasAttribute("role")) wrapper.setAttribute("role", "region");
        if (!wrapper.hasAttribute("aria-label")) {
          const caption = wrapper.querySelector("caption")?.textContent?.trim();
          wrapper.setAttribute("aria-label", caption || "Scrollable table");
        }
      } else {
        wrapper.removeAttribute("tabindex");
        wrapper.removeAttribute("role");
        wrapper.removeAttribute("aria-label");
      }
    });
  }

  // A row can say more than it shows: a clamped name, an ellipsis, a list
  // folded behind "+5 more". Such a row gets one toggle that shows all of it,
  // every cut-off value in full and every folded part open, and puts it back.
  // The parts keep their own toggles too; this is the row's, not a
  // replacement. Measured, not declared: a row that fits gets no toggle, and a
  // resize that makes it fit takes the toggle away.
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
  // dropped. An empty one says nothing, so it is not something held back.
  const droppedCells = (row) =>
    [...row.children].filter(
      (cell) =>
        cell.tagName === "TD"
        && getComputedStyle(cell).display === "none"
        && cell.textContent.trim() !== ""
        && !cell.querySelector(":scope > .empty-value:only-child"),
    );

  // Within a cell that is showing, what a narrow table holds back.
  const heldBack = (cell) =>
    [...cell.querySelectorAll(".narrow-more")].filter(
      (el) => getComputedStyle(el).display === "none",
    );

  // Every table sorts. A list whose server sorts it already has links in its
  // headings and is left to them; any other table with headings gets the same
  // control here, working on the rows that are on the page. A press sorts
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
  // A table fits its box, at any width, by measurement: no screen width is
  // asked. It takes the box's width and wraps. Each column is given a floor,
  // the least it can be and still be read: its own one-line width, or a few
  // words' worth, whichever is less, and more for the column that names the
  // row. The browser lays the table out above those floors, taking room from
  // the columns with room to give. Only when the floors themselves do not fit
  // does a column leave: first any marked `optional-col`, then from the far
  // end, never the one that names the row, one marked `key-col`, or the
  // closing figure of a table that marks none. What leaves is in the row's
  // own toggle. A window that grows gets its columns back.
  const READABLE = 120;
  const READABLE_NAME = 200;
  // Below this a table's box is narrow, whatever the screen: a phone, or half
  // a page beside a chart.
  const NARROW_BOX = 640;
  const fitTable = (table) => {
    const wrapper = table.parentElement;
    if (!wrapper?.classList.contains("table-scroll")) return;
    // Every table is told when its box is narrow, a drawing's table too: what
    // it does about it is its own rule's.
    if (wrapper.clientWidth < NARROW_BOX) table.dataset.narrow = "";
    else delete table.dataset.narrow;
    if (wrapper.hasAttribute("data-chart") || wrapper.parentElement?.classList.contains("chart-data")) return;
    // Held open or pinned across a refresh: its columns are not to move.
    if (table.style.tableLayout === "fixed") return;
    table.dataset.fit = "";
    delete table.dataset.drop;
    delete table.dataset.wrap;
    const overflows = () => table.getBoundingClientRect().width > wrapper.clientWidth + 1;
    const all = [...table.querySelectorAll(":scope > thead > tr:last-child > th")];
    all.forEach((heading) => heading.style.removeProperty("--column-floor"));
    const headings = all.filter((heading) => getComputedStyle(heading).display !== "none");
    // Only the headings showing: a table whose own rule has already dropped
    // columns in a narrow box (a matrix of periods) is fitted from what is left.
    if (headings.length) {
      // What each column would take with every value on one line.
      table.dataset.measure = "";
      const wanted = headings.map((heading) => heading.getBoundingClientRect().width);
      delete table.dataset.measure;
      const named = headings[0].classList.contains("select-column") ? 1 : 0;
      headings.forEach((heading, index) => {
        const style = getComputedStyle(heading);
        const inset = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
        const floor = Math.min(wanted[index], index === named ? READABLE_NAME : READABLE);
        heading.style.setProperty("--column-floor", `${Math.max(0, Math.floor(floor - inset))}px`);
      });
      if (overflows() && headings.length >= 3 && !table.querySelector(":scope > colgroup")) {
        const last = headings.length - 1;
        const marked = headings.some((heading) => heading.classList.contains("key-col"));
        const figures = [...table.querySelectorAll(":scope > tbody > tr:not(.row-group):not(.row-detail)")]
          .map((row) => sortValue(row.children[headings[last].cellIndex]))
          .filter((value) => value !== null);
        const endsInFigure = !marked && figures.length > 0
          && figures.filter((value) => typeof value === "number").length * 2 > figures.length;
        const spare = headings.filter((heading, index) => index > named
          && !(endsInFigure && index === last)
          && !heading.classList.contains("key-col"));
        // Popped from the end: the far end first, and before any of those,
        // whatever the page said it can do without.
        spare.sort((left, right) =>
          left.classList.contains("optional-col") - right.classList.contains("optional-col"));
        const dropped = [];
        while (spare.length && overflows()) {
          dropped.push(spare.pop().cellIndex + 1);
          table.dataset.drop = dropped.join(" ");
        }
      }
    }
    // Still wider than its box with nothing left to give up.
    if (overflows()) table.dataset.wrap = "tight";
  };
  const fitTables = () => {
    document.querySelectorAll(".table-scroll > .data-table").forEach(fitTable);
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
        parts.push([name, nodes]);
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
    if (!parts.length) return;
    const list = document.createElement("dl");
    list.className = "row-columns";
    parts.forEach(([name, nodes]) => {
      const term = document.createElement("dt");
      term.textContent = name;
      const value = document.createElement("dd");
      value.append(...nodes);
      list.append(term, value);
    });
    const cell = document.createElement("td");
    cell.colSpan = cells.length;
    cell.append(list);
    const detail = document.createElement("tr");
    detail.className = "row-detail";
    // Named by attribute as well, for anything that has to tell a record from
    // the line beneath it without knowing how either is styled.
    detail.dataset.rowDetail = "";
    detail.append(cell);
    restore.set(detail, undo);
    row.after(detail);
  };

  const rowSaysMore = (row) =>
    row.querySelector(":is(td, th) .row-more") !== null
    || droppedCells(row).length > 0
    || [...row.children].some((cell) => heldBack(cell).length > 0)
    || [...row.querySelectorAll(":is(td, th) *")].some(isCut)
    || row.querySelectorAll(":is(td, th) details").length >= 2;

  // The row's first cell, header or data: the toggle sits beside what names
  // the row, not in whichever column happens to be the first <td>.
  // The same column in every row: the first that is not the tick-box column,
  // read off the headings, so a row with no tick box does not put its toggle
  // one column to the left of its neighbours'.
  const toggleCell = (row) => {
    const heads = [...row.closest("table").querySelectorAll(":scope > thead > tr:last-child > th")];
    const column = heads.findIndex((head) => !head.classList.contains("select-column") && !head.querySelector("input[type=checkbox]"));
    if (column > 0 && row.children[column] && row.children.length === heads.length) return row.children[column];
    return [...row.children].find((cell) => !cell.querySelector("input[type=checkbox]"));
  };

  function markExpandableRows() {
    document.querySelectorAll(".data-table > tbody > tr:not(.row-detail)").forEach((row) => {
      if (row.classList.contains("is-expanded")) return;
      // A drawing's table is read as a whole, column against column: a period
      // it drops in a narrow box is not something one row holds back.
      const box = row.closest("table").parentElement;
      if (box?.hasAttribute("data-chart") || box?.parentElement?.classList.contains("chart-data")) return;
      const toggle = row.querySelector(".row-expand");
      const expandable = rowSaysMore(row);
      if (expandable && !toggle) {
        // A child of the cell itself, never of anything in it: the stylesheet
        // stands it in the cell's gutter, clear of the cell's own layout and
        // of any control there.
        const cell = toggleCell(row);
        if (!cell) return;
        const button = document.createElement("button");
        button.dataset.rowToggle = "";
        button.type = "button";
        button.className = "row-expand";
        button.setAttribute("aria-expanded", "false");
        button.setAttribute("aria-label", "Show all of this row");
        // The nav's caret, pointing right until the row is open.
        const svg = "http://www.w3.org/2000/svg";
        const mark = document.createElementNS(svg, "svg");
        mark.setAttribute("viewBox", "0 0 6 10");
        mark.setAttribute("width", "7");
        mark.setAttribute("height", "11");
        mark.setAttribute("aria-hidden", "true");
        const path = document.createElementNS(svg, "path");
        path.setAttribute("d", "M1 1l4 4-4 4");
        mark.append(path);
        button.append(mark);
        // Last in the cell, not first: it is placed by the stylesheet either
        // way, and a rule that asks what a cell starts with should get the
        // same answer whether or not the row has a toggle.
        cell.append(button);
      } else if (!expandable && toggle) {
        toggle.remove();
      }
    });
  }

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
    if (!table.querySelector(":scope > tbody > tr.is-expanded")) releaseColumns(table);
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

  // A toggle stands in a gutter of its own at the start of the cell that names
  // the row, and every row of a table that has one keeps the gutter, the
  // heading too: names start on one line down the column whether or not a
  // given row has anything to open.
  function markGutters() {
    document.querySelectorAll(".data-table").forEach((table) => {
      const rows = [...table.querySelectorAll(":scope > tbody > tr:not(.row-detail):not(.row-group)")];
      const any = rows.some((row) => row.querySelector(".row-expand"));
      const cells = rows.map(toggleCell).filter(Boolean);
      const first = cells[0];
      const heading = first
        ? table.querySelector(`:scope > thead > tr:last-child > th:nth-child(${first.cellIndex + 1})`)
        : null;
      [...cells, heading].forEach((cell) => cell?.classList.toggle("row-gutter", any));
    });
  }

  const cameIn = new WeakMap();
  const SORT_MARK = { ascending: "\u2191\ufe0e", descending: "\u2193\ufe0e", none: "\u2195\ufe0e" };

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

  function markSortable() {
    document.querySelectorAll(".table-scroll > .data-table").forEach((table) => {
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
        mark.textContent = SORT_MARK.none;
        button.append(mark);
        button.setAttribute("aria-label", `Sort by ${name}`);
        heading.append(button);
      });
    });
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-sort-local]");
    if (!button) return;
    const heading = button.closest("th");
    const table = heading.closest("table");
    const next = { none: "ascending", ascending: "descending", descending: "none" }[heading.getAttribute("aria-sort")] || "ascending";
    table.querySelectorAll(":scope > thead th[data-sort-ready]").forEach((other) => {
      other.setAttribute("aria-sort", "none");
      other.querySelector(".sort-indicator").textContent = SORT_MARK.none;
    });
    heading.setAttribute("aria-sort", next);
    button.querySelector(".sort-indicator").textContent = SORT_MARK[next];
    sortRows(table, heading.cellIndex, next);
  });

  let rowsTimer = null;
  // A window that changes size changes what fits, so widths held for the old
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
      fitTable(table);
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
    // Twice: a toggle and its gutter take room in the column that names the
    // row, which can be the room a table that only just fitted did not have.
    for (let pass = 0; pass < 2; pass++) {
      fitTables();
      markScrollRegions();
      markExpandableRows();
      markGutters();
    }
  };
  const markTablesSoon = () => {
    window.clearTimeout(rowsTimer);
    rowsTimer = window.setTimeout(markTables, 120);
  };


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

  markTables();
  window.addEventListener("resize", markTablesSoon);
  // Everything above is measured in the face the text is drawn in. Measured
  // before that face has loaded, a column's floor is the fallback's width and
  // a word the real face draws wider wraps. So it is measured again when the
  // faces are in.
  document.fonts?.ready.then(markTables);

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

  // A refresh replaces the toolbar, which would otherwise steal focus from
  // the search box mid-typing. Remember what was focused and the live value,
  // the user may have typed past what this response's server render reflects.
  function captureFocus() {
    const active = document.activeElement;
    if (!active || !active.name || !active.closest("[data-table-toolbar]")) return null;
    return {
      name: active.name,
      value: active.value,
      isText: active.type === "search" || active.type === "text",
      start: active.selectionStart,
      end: active.selectionEnd,
    };
  }

  function restoreFocus(memo) {
    if (!memo) return;
    const revived = document.querySelector(
      `[data-table-toolbar] [name="${CSS.escape(memo.name)}"]`,
    );
    if (!revived) return;
    if (memo.isText) revived.value = memo.value;
    revived.focus({ preventScroll: true });
    if (memo.isText && typeof memo.start === "number") {
      revived.setSelectionRange(memo.start, memo.end);
    }
  }

  // Auto table layout recomputes column widths from whichever rows are
  // visible, so sorting or paging makes columns jitter. Pin the incoming
  // table to the current widths; a full page load re-derives natural widths.
  function pinColumnWidths(next) {
    const currentTables = document.querySelectorAll(".table-scroll table");
    const nextTables = next.querySelectorAll(".table-scroll table");
    currentTables.forEach((table, tableIndex) => {
      const nextTable = nextTables[tableIndex];
      if (!nextTable) return;
      const currentHeads = table.querySelectorAll("thead th");
      const nextHeads = nextTable.querySelectorAll("thead th");
      if (!currentHeads.length || currentHeads.length !== nextHeads.length) return;
      // The incoming table gives up the same columns the current one has.
      ["fit", "drop", "wrap", "narrow"].forEach((key) => {
        if (key in table.dataset) nextTable.dataset[key] = table.dataset[key];
      });
      currentHeads.forEach((th, index) => {
        const width = th.getBoundingClientRect().width;
        if (width) nextHeads[index].style.width = `${width}px`;
      });
      nextTable.style.tableLayout = "fixed";
    });
  }

  function tableUrl(form) {
    const params = new URLSearchParams(new FormData(form));
    return `${window.location.pathname}?${params.toString()}`;
  }

  async function refreshTable(url, { history = "push" } = {}) {
    requestController?.abort();
    const controller = new AbortController();
    requestController = controller;
    const main = document.querySelector("main");
    main.setAttribute("aria-busy", "true");
    try {
      const response = await window.hqFetch(url, { signal: controller.signal });
      if (!response.ok) throw new Error(`Table request failed: ${response.status}`);
      const next = hqParseDocument(await response.text());
      const focusMemo = captureFocus();
      preserveDisclosureState(next, url);
      pinColumnWidths(next);
      const selectors = [
        "[data-table-toolbar]",
        "[data-table-selection]",
        ".table-scroll",
        ".pagination",
        "[data-search-results]",
      ];
      // Replace every match pairwise so pages with more than one table
      // (e.g. control_plane resource list) swap all of them, not just the first.
      selectors.forEach((selector) => {
        const nextNodes = next.querySelectorAll(selector);
        document.querySelectorAll(selector).forEach((currentNode, index) => {
          const nextNode = nextNodes[index];
          if (nextNode) currentNode.replaceWith(nextNode);
          else currentNode.remove();
        });
      });
      document.title = next.title;
      if (history === "push") window.history.pushState({}, "", url);
      if (history === "replace") window.history.replaceState({}, "", url);
      tableLocation = `${window.location.pathname}${window.location.search}`;
      initializeSelection();
      restoreFocus(focusMemo);
      markTables();
    } catch (error) {
      if (error.name !== "AbortError") window.location.assign(url);
    } finally {
      if (requestController === controller) main.removeAttribute("aria-busy");
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
    initializeSelection();
    markTables();
  });
})();
