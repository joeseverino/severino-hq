// Added to every demo page by demo/build.py. HQ itself never loads it.
//
// The demo has no server, so this script:
// - serves partial page loads from the copies in __fragments__/, and one with a
//   query from the saved copy of that address (demo-theme.js), and stops polling;
//   one that posts is answered with nothing changed;
// - says so, by kind (a sort, a page, a filter), on a page whose query the demo
//   has no copy of;
// - answers the connection panel's header read with the headers HQ sent at build;
// - saves dismissals in localStorage and shows "Demo only: nothing was saved"
//   for any other form that posts;
// - keeps the chosen theme in localStorage, as HQ keeps it on the account;
// - locks the Demo data switch on;
// - adds "Reset demo" to the user menu, which clears localStorage.
(() => {
  const STORE = "hq-demo";
  const read = () => {
    try { return JSON.parse(localStorage.getItem(STORE)) || { dismissed: [] }; } catch { return { dismissed: [] }; }
  };
  const write = (state) => {
    try { localStorage.setItem(STORE, JSON.stringify(state)); } catch { /* private window */ }
  };

  // The theme on every choice.
  const applyTheme = (theme) => {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    document.querySelectorAll(".menu-theme-option").forEach((option) => {
      option.setAttribute("aria-pressed", String(option.value === (theme || "system")));
    });
  };
  // demo-theme.js has set the theme already; this marks the chosen button.
  if (read().theme) applyTheme(read().theme);
  document.addEventListener("submit", (event) => {
    if (!event.target.matches(".menu-theme")) return;
    event.preventDefault();
    const state = read();
    state.theme = event.submitter?.value || "system";
    write(state);
    applyTheme(state.theme);
  }, true);

  // Partial page loads come from the saved copies. A script's read of the page
  // itself (the connection panel's) gets the headers HQ sent when the demo was
  // built, since GitHub Pages can't send them.
  const plainFetch = window.fetch.bind(window);
  const once = (path) => { let kept; return () => (kept ??= plainFetch(path).then((r) => r.json())); };
  const answers = once("/__fragments__/index.json");
  const sent = once("/__fragments__/headers.json");
  const demo = window.hqDemo;
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const method = (init.method || "GET").toUpperCase();
    const headers = new Headers(init.headers || {});
    const partial = headers.has("X-Fragment") || headers.get("X-Requested-With") === "XMLHttpRequest";
    // A region that posts (the dashboard asking for its due readings) is
    // answered as it was drawn: nothing changed, and an empty answer would
    // empty the region.
    if (method !== "GET" && !partial) return new Response("", { status: 200 });
    if (partial) {
      const name = headers.get("X-Fragment") || "";
      const saved = await answers();
      const found = saved[`${demo.key(url)}|${name}`];
      if (found) return plainFetch(found);
      const asked = url.pathname === "/search/" ? url.searchParams.get("q") : "";
      if (asked && saved[`${demo.searchKey(asked, await demo.routes())}|${name}`]) {
        return plainFetch(saved[`${demo.searchKey(asked, await demo.routes())}|${name}`]);
      }
      // The page itself: the file it was served from, with the headers HQ sent.
      if (demo.key(url) === demo.key(new URL(location.href))) {
        return new Response(await (await plainFetch(demo.served)).text(), { headers: await sent() });
      }
      // Another address with a query: its saved copy, a whole page the
      // region is found in; else nothing, and the link it enhanced is followed.
      const copy = (await demo.routes()).variants[demo.key(url)];
      if (copy) return plainFetch(copy);
      // A post with no saved answer (a page asking for its readings as it
      // opens) is answered with nothing changed, as above.
      if (method !== "GET") return new Response("", { status: 200 });
      if (url.search) return new Response("", { status: 404 });
    }
    return plainFetch(input, init);
  };

  // Said the way HQ says a save happened: a message at the top of the page.
  const say = (words) => {
    const main = document.getElementById("content");
    let list = main.querySelector(":scope > .messages");
    if (!list) {
      list = document.createElement("ul");
      list.className = "messages";
      main.prepend(list);
    }
    const note = document.createElement("li");
    note.className = "msg msg-info";
    note.setAttribute("role", "status");
    note.textContent = words;
    list.replaceChildren(note);
    main.scrollIntoView({ block: "start" });
  };

  // A query the demo has no saved copy of: the page is shown without it, and
  // says what was left out. demo/build.py saves no sorts, the first pages of
  // a list (_MOST_PAGES_OF_A_LIST) and every filter a page offers.
  const unanswered = () => {
    const asked = new URLSearchParams(location.search);
    if (asked.has("sort")) say("Sorting isn't saved in the demo, so the list is shown in its usual order.");
    else if (asked.has("page")) say("Only the first pages of a list are saved in the demo, so this shows the first.");
    else say("This filter isn't in the demo, so the page is shown without it.");
  };
  const answeredAs = () => say(`The demo has no saved search for that, so this shows results for “${demo.answeredAs}”.`);
  document.addEventListener("hq-demo:unanswered", () => {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", unanswered);
    else unanswered();
  });

  document.addEventListener("DOMContentLoaded", () => {
    if (!demo.answered) unanswered();
    if (demo.answeredAs) answeredAs();
    document.querySelectorAll("[data-fragment-poll]").forEach((region) => region.removeAttribute("data-fragment-poll"));

    // Re-apply saved dismissals.
    const state = read();
    document.querySelectorAll("[data-attention-item]").forEach((row) => {
      const key = row.querySelector("[data-triage] button")?.value;
      if (key && state.dismissed.includes(key)) {
        row.dataset.dismissed = `Dismissed: ${row.querySelector(".attention-title")?.textContent.trim() || ""}`;
      }
    });

    // Demo data is always on here.
    const demoSwitch = document.querySelector('form[action="/demo/"] [role="switch"]');
    if (demoSwitch) {
      demoSwitch.disabled = true;
      demoSwitch.title = "Always on in the public demo";
    }

    // Reset demo, among the user menu's places, before Sign out.
    const places = document.querySelector(".menu-places");
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "menu-item demo-reset";
    reset.textContent = "Reset demo";
    reset.addEventListener("click", () => { try { localStorage.removeItem(STORE); } catch { /* private window */ } location.assign("/"); });
    if (places) places.insertBefore(reset, places.querySelector(":scope > form"));
    else document.querySelector(".site-header")?.append(reset);
  });

  // Say where the connection panel's headers came from.
  const relabel = () => {
    const note = document.querySelector("[data-response-headers]")?.previousElementSibling;
    if (note?.classList.contains("conn-note")) {
      note.textContent = "Recorded from HQ when this demo was built. GitHub Pages, which serves the demo, can't send them.";
    }
  };
  document.addEventListener("DOMContentLoaded", relabel);
  document.addEventListener("hq:fragment", relabel);

  // Save dismissals and un-dismissals.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-triage] button[formaction]");
    if (!button) return;
    const state = read();
    const dismissing = /set-aside/.test(button.getAttribute("formaction"));
    state.dismissed = dismissing ? [...new Set([...state.dismissed, button.value])] : state.dismissed.filter((key) => key !== button.value);
    write(state);
  }, true);
  // Every other form that posts shows the note instead. A filter or a search
  // is a GET, answered from its saved copy like any address with a query.
  document.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    if ((event.target.getAttribute("method") || "get").toLowerCase() === "get") return;
    event.preventDefault();
    say("Demo only: nothing was saved.");
  });
})();
