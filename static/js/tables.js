(() => {
  let requestController = null;
  let searchTimer = null;

  function initializeSelection(root = document) {
    root.querySelectorAll("[data-selectable-table]").forEach((table) => {
      if (table.dataset.selectionReady) return;
      table.dataset.selectionReady = "true";
      const rows = [...table.querySelectorAll("[data-row-select]")];
      const selectAll = table.querySelector("[data-select-all]");
      const bar = table.closest("main").querySelector("[data-table-selection]");
      if (!rows.length || !selectAll || !bar) return;

      const update = () => {
        const selected = rows.filter((input) => input.checked);
        bar.hidden = selected.length === 0;
        bar.querySelector("[data-selection-count]").textContent = selected.length;
        selectAll.checked = selected.length === rows.length;
        selectAll.indeterminate = selected.length > 0 && selected.length < rows.length;
      };
      selectAll.addEventListener("change", () => {
        rows.forEach((input) => { input.checked = selectAll.checked; });
        update();
      });
      rows.forEach((input) => input.addEventListener("change", update));
      bar.querySelector("[data-clear-selected]").addEventListener("click", () => {
        rows.forEach((input) => { input.checked = false; });
        update();
      });
      bar.querySelector("[data-copy-selected]").addEventListener("click", async () => {
        const ids = rows.filter((input) => input.checked).map((input) => input.value);
        await navigator.clipboard.writeText(ids.join("\n"));
      });
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
      const response = await fetch(url, {
        headers: { "X-HQ-Table-Request": "1" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Table request failed: ${response.status}`);
      const next = new DOMParser().parseFromString(await response.text(), "text/html");
      const selectors = [
        "[data-table-toolbar]",
        "[data-table-selection]",
        ".table-scroll",
        ".pagination",
      ];
      selectors.forEach((selector) => {
        const currentNode = document.querySelector(selector);
        const nextNode = next.querySelector(selector);
        if (currentNode && nextNode) currentNode.replaceWith(nextNode);
        else if (currentNode && !nextNode) currentNode.remove();
      });
      document.title = next.title;
      if (history === "push") window.history.pushState({}, "", url);
      if (history === "replace") window.history.replaceState({}, "", url);
      initializeSelection();
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
    searchTimer = window.setTimeout(
      () => refreshTable(tableUrl(input.form), { history: "replace" }),
      250,
    );
  });

  document.addEventListener("change", (event) => {
    const control = event.target.closest(
      "[data-table-toolbar] input[type=checkbox], [data-table-toolbar] select",
    );
    if (control) refreshTable(tableUrl(control.form));
  });

  document.addEventListener("click", (event) => {
    const link = event.target.closest(".table-sort-link, .pagination a");
    if (!link) return;
    event.preventDefault();
    refreshTable(link.href);
  });

  window.addEventListener("popstate", () => {
    refreshTable(window.location.href, { history: "none" });
  });

  document.addEventListener("DOMContentLoaded", () => initializeSelection());
})();
