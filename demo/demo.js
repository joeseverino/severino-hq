// Added to every demo page by demo/build.py. HQ itself never loads it.
//
// The demo has no server, so this script:
// - serves partial page loads from the copies in __fragments__/ and stops polling;
// - saves dismissals in localStorage and shows "Demo only: nothing was saved"
//   for any other form;
// - adds "Reset demo" to the user menu, which clears localStorage.
(() => {
  const STORE = "hq-demo";
  const read = () => {
    try { return JSON.parse(localStorage.getItem(STORE)) || { dismissed: [] }; } catch { return { dismissed: [] }; }
  };
  const write = (state) => {
    try { localStorage.setItem(STORE, JSON.stringify(state)); } catch { /* private window */ }
  };

  // Partial page loads come from the saved copies.
  let saved = null;
  const answers = () => (saved ??= window.fetch.call(window, "/__fragments__/index.json").then((r) => r.json()));
  const plainFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    const method = (init.method || "GET").toUpperCase();
    const headers = new Headers(init.headers || {});
    if (method !== "GET") return new Response("", { status: 200 });
    if (headers.has("X-Fragment") || headers.get("X-Requested-With") === "XMLHttpRequest") {
      const found = (await answers())[`${new URL(url, location.href).pathname}|${headers.get("X-Fragment") || ""}`];
      if (found) return plainFetch(found);
    }
    return plainFetch(input, init);
  };

  const say = (words) => {
    const note = document.createElement("p");
    note.className = "demo-note";
    note.setAttribute("role", "status");
    note.textContent = words;
    document.body.append(note);
    setTimeout(() => note.remove(), 4000);
  };

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-fragment-poll]").forEach((region) => region.removeAttribute("data-fragment-poll"));

    // Re-apply saved dismissals.
    const state = read();
    document.querySelectorAll("[data-attention-item]").forEach((row) => {
      const key = row.querySelector("[data-triage] button")?.value;
      if (key && state.dismissed.includes(key)) {
        row.dataset.dismissed = `Dismissed: ${row.querySelector(".attention-title")?.textContent.trim() || ""}`;
      }
    });

    // Reset demo, at the bottom of the user menu.
    const menu = document.querySelector(".user-menu-panel");
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "menu-item demo-reset";
    reset.textContent = "Reset demo";
    reset.addEventListener("click", () => { try { localStorage.removeItem(STORE); } catch { /* private window */ } location.assign("/"); });
    (menu || document.querySelector(".site-header"))?.append(reset);
  });

  // Save dismissals and un-dismissals.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-triage] button[formaction]");
    if (!button) return;
    const state = read();
    const dismissing = /set-aside/.test(button.getAttribute("formaction"));
    state.dismissed = dismissing ? [...new Set([...state.dismissed, button.value])] : state.dismissed.filter((key) => key !== button.value);
    write(state);
  }, true);
  // Every other form shows the note instead of posting.
  document.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    event.preventDefault();
    say("Demo only: nothing was saved.");
  });
})();
