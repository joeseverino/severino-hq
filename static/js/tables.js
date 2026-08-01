document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-selectable-table]").forEach((table) => {
    const rows = [...table.querySelectorAll("[data-row-select]")];
    const selectAll = table.querySelector("[data-select-all]");
    const bar = table.parentElement.parentElement.querySelector("[data-table-selection]");
    if (!rows.length || !bar) return;

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
});
