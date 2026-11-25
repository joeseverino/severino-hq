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

  // A list whose server sorts it has links in its headings; any other table
  // whose rows are worth sorting has buttons that sort the rows on the page.
  // Both are drawn by the server (`table_heading`), in an extension's table
  // too (hand_drawn_tables). A press sorts ascending, the next descending,
  // the third puts the rows back as they came.
  // Rows keep to their own section, a section's heading row stays on top of
  // it, and an open row takes the line beneath it along. A cell sorts by the
  // value the server wrote on it where that is not its words (`data-sort`,
  // `Cell.sort_value`), else by the words it shows; one that says nothing
  // (`data-blank`) sorts last.
  const sortValue = (cell) => {
    if (!cell || cell.hasAttribute("data-blank")) return null;
    const given = cell.dataset.sort;
    if (given !== undefined) {
      const number = Number(given);
      return Number.isNaN(number) ? given.toLowerCase() : number;
    }
    const copy = cell.cloneNode(true);
    copy.querySelectorAll(".visually-hidden, [data-row-toggle], [aria-hidden=true]").forEach((part) => part.remove());
    return copy.textContent.trim().replace(/\s+/g, " ").toLowerCase() || null;
  };
  const compare = (left, right) => {
    if (typeof left === "number" && typeof right === "number") return left - right;
    return String(left).localeCompare(String(right), undefined, { numeric: true });
  };

  // The server writes each cell once (table_layout), and a row's open line
  // takes what the row holds back from the row as it first opens: each cell
  // of a ranked column (its `data-drop` copied, so the stylesheet still says
  // when it shows) and each cell holding a part only the open line shows
  // (`row-more`, `line-rest`; `data-holds`, shown at every width, what the
  // row's line already shows of it boxed as `data-row-shows`), under its
  // column's name, cloned from the region's `data-open-line` frame with no id
  // the row already draws.
  const HELD = ".row-more, .line-rest";
  const heldBack = (cell) =>
    !cell.hasAttribute("data-blank") &&
    !cell.hasAttribute("colspan") &&
    (cell.hasAttribute("data-drop") || Boolean(cell.querySelector(HELD)));
  const columnName = (row, cell) => {
    if (cell.dataset.label) return cell.dataset.label;
    const heading = row.closest("table").tHead?.rows;
    if (!heading?.length) return "";
    let place = 0;
    for (const each of row.cells) {
      if (each === cell) break;
      place += each.colSpan;
    }
    let at = 0;
    for (const each of heading[heading.length - 1].cells) {
      if (at === place) return each.textContent.trim().replace(/\s+/g, " ");
      at += each.colSpan;
    }
    return "";
  };
  const filled = new WeakSet();
  const fill = (row) => {
    const region = row.closest(".table-scroll") || row;
    const frameOf = region.querySelector("template[data-open-line]");
    if (!frameOf || filled.has(row)) return;
    filled.add(row);
    const held = [...row.cells].filter(heldBack);
    if (!held.length) return;
    const line = frameOf.content.firstElementChild.cloneNode(true);
    const list = line.querySelector(".row-columns");
    const frame = list.firstElementChild;
    const shows = frame.querySelector("[data-row-shows]");
    shows.remove();
    frame.remove();
    held.forEach((cell) => {
      const entry = frame.cloneNode(true);
      const holds = Boolean(cell.querySelector(HELD));
      if (cell.dataset.drop) entry.dataset.drop = cell.dataset.drop;
      entry.toggleAttribute("data-holds", holds);
      entry.querySelector("dt").textContent = columnName(row, cell);
      const value = entry.querySelector("dd");
      let box = null;
      [...cell.childNodes].forEach((node) => {
        if (node.matches?.("[data-row-toggle]")) return;
        const copy = node.cloneNode(true);
        if (!holds || node.matches?.(HELD) || node.querySelector?.(HELD)) {
          value.append(copy);
          box = null;
          return;
        }
        if (!box) {
          box = shows.cloneNode();
          value.append(box);
        }
        box.append(copy);
      });
      // A copy names no row and draws no id twice.
      value.querySelectorAll("[id]").forEach((each) => each.removeAttribute("id"));
      value.querySelectorAll('[data-cell~="name"]').forEach((each) => {
        const roles = each.dataset.cell.split(" ").filter((role) => role !== "name");
        if (roles.length) each.dataset.cell = roles.join(" ");
        else delete each.dataset.cell;
      });
      list.append(entry);
    });
    const detail = row.nextElementSibling?.matches("[data-row-detail]") ? row.nextElementSibling : null;
    if (!detail) {
      line.cells[0].colSpan = [...row.cells].reduce((width, cell) => width + cell.colSpan, 0);
      row.after(line);
      return;
    }
    const drawn = detail.cells[0].querySelector(":scope > .row-columns");
    if (drawn) drawn.prepend(...list.children);
    else detail.cells[0].append(list);
  };
  // A row the server draws open (its editor open) is filled as it arrives,
  // with the page or in the region that answered, before it is painted.
  const fillOpen = (root) => root.querySelectorAll(".data-table > tbody > tr.is-expanded").forEach(fill);
  fillOpen(document);
  document.addEventListener("hq:fragment", (event) => fillOpen(event.target));

  // A row opens to all it holds: a column its region let go, a value cut at
  // its column, a part that waits for it. How it shows them is the
  // stylesheet's (tables.css), which lays them out on lines of their own
  // under the row, so opening moves no column. `button` is its toggle, which
  // a row that heads a fold has none of.
  const setExpanded = (row, expanding, button = null) => {
    if (expanding) fill(row);
    row.classList.toggle("is-expanded", expanding);
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

  // A row that heads rows folded under it (`data-folds`, its rows naming it
  // in `data-folded`) opens and closes them from anywhere on it but its own
  // controls. Closing it closes any of its rows that were open, so no line is
  // left beneath a row that is not there. Its fold stands where its own
  // toggle would, so the head opens with its rows.
  const foldedUnder = (head) =>
    head.closest("tbody").querySelectorAll(`:scope > tr[data-folded="${CSS.escape(head.id)}"]`);
  const setFold = (head, opening) => {
    head.querySelector("[data-fold-toggle]")?.setAttribute("aria-expanded", String(opening));
    if (head.classList.contains("is-expanded") !== opening) setExpanded(head, opening);
    foldedUnder(head).forEach((row) => {
      if (!opening && row.classList.contains("is-expanded")) row.querySelector(".row-expand")?.click();
      row.toggleAttribute("data-open", opening);
    });
  };
  document.addEventListener("click", (event) => {
    const head = event.target.closest("tr[data-folds]");
    // A Cmd- or Ctrl-click asks for what raised the row (app.js), never its fold.
    if (!head || event.defaultPrevented || event.metaKey || event.ctrlKey) return;
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
  // bar is the navigation's, already at `url`.
  async function refreshTable(url) {
    try {
      await hqFragment.swap(TABLE_PARTS, {
        url,
        strict: false,
        busy: document.querySelector("main"),
        title: true,
      });
      initializeSelection();
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

  window.navigation.addEventListener("navigate", (event) => {
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
      if (form) window.navigation.navigate(tableUrl(form), { history: "replace", info: TABLE });
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
    landOnRow();
  });
})();
