(() => {
  let requestController = null;
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
  // the search box mid-typing. Remember what was focused and the live value —
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
      currentHeads.forEach((th, index) => {
        nextHeads[index].style.width = `${th.getBoundingClientRect().width}px`;
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
      const response = await fetch(url, { signal: controller.signal });
      // An expired session 302s to the login page; fetch follows it and would
      // otherwise splice login-page fragments into the table. Hand the whole
      // navigation to the browser instead.
      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }
      if (!response.ok) throw new Error(`Table request failed: ${response.status}`);
      const next = new DOMParser().parseFromString(await response.text(), "text/html");
      const focusMemo = captureFocus();
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
      initializeSelection();
      restoreFocus(focusMemo);
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
    const link = event.target.closest(".table-sort-link, .pagination a");
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
    refreshTable(window.location.href, { history: "none" });
  });

  document.addEventListener("DOMContentLoaded", () => initializeSelection());
})();
