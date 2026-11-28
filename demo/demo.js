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
  // HQ's answer for a word nothing matches (the search page, the command
  // center's results), marked as a word the demo has no search for: the
  // word asked in its box, and the demo's line where HQ says nothing matches.
  const marked = (root, asked, nothing) => {
    root.querySelectorAll('input[name="q"]').forEach((box) => {
      if (box.value !== nothing) return;
      box.setAttribute("value", asked);
      box.value = asked;
    });
    root.querySelectorAll("[data-command-center-empty], [data-empty-state]").forEach((empty) => {
      const line = empty.firstElementChild;
      line.textContent = `The demo has no saved search for “${asked}”.`;
      empty.replaceChildren(line);
    });
  };
  // The search last answered as one nothing matches, marked as each part of
  // its answer is placed, before it is drawn.
  let unsearched = null;
  document.addEventListener("hq:fragment", (event) => {
    if (unsearched) marked(event.target, unsearched.asked, unsearched.nothing);
  });
  // The address this document was shown at. A list's navigation answered in
  // place moves the address before it asks for its answer, so the address
  // bar is not the page itself.
  const shownAt = new URL(location.href);
  // A control that asks for a reading again (ask.js: a row's Refresh, a
  // dashboard panel's, a page's readings as it opens) is answered as HQ
  // answers it, with the page it was asked on: the saved copy, each control
  // of that ask, found by its id, drawn idle and saying why (`data-ask-said`).
  // Its address may be a region's own too (a panel's Refresh posts to the
  // strip), whose saved copy is no answer to an ask. The copy is marked as
  // text: only fragment.js may parse markup (its Trusted Types policy).
  const READ_AGAIN = "The demo can't read it again: this is as it was saved.";
  const asksAt = (url) => [...document.querySelectorAll("[data-ask][id]")].filter((control) =>
    [...control.querySelectorAll("[data-ask-button], form[data-ask-auto]")]
      .some((asker) => (asker.formAction || asker.action) === url.href));
  const readAgain = async (url) => {
    let page = await (await plainFetch(demo.served)).text();
    asksAt(url).forEach((control) => {
      page = page.replace(` id="${control.id}"`, ` id="${control.id}" data-ask-said="${READ_AGAIN}"`);
    });
    return new Response(page, { headers: { "Content-Type": "text/html" } });
  };
  window.fetch = async (input, init = {}) => {
    // A request is asked by a string, a URL (the command center's) or a Request.
    const url = new URL(input instanceof Request ? input.url : String(input), location.href);
    const method = (init.method || "GET").toUpperCase();
    const headers = new Headers(init.headers || {});
    const partial = headers.has("X-Fragment") || headers.get("X-Requested-With") === "XMLHttpRequest";
    // A region that posts (the dashboard asking for its due readings) is
    // answered as it was drawn: nothing changed, and an empty answer would
    // empty the region.
    if (method !== "GET" && !partial) return new Response("", { status: 200 });
    if (partial && method !== "GET" && asksAt(url).length) return readAgain(url);
    if (partial) {
      // This page is being replaced by its address's saved copy
      // (demo-theme.js), and a request it sent would be cut off with it: it
      // is never answered, as hqFetch leaves one a renewal takes away.
      if (demo.leaving) return new Promise(() => {});
      const name = headers.get("X-Fragment") || "";
      const [saved, found] = await Promise.all([answers(), demo.routes()]);
      const answer = saved[`${demo.key(url, found)}|${name}`];
      if (answer) return plainFetch(answer);
      // A search, in the command center or the search page's own box: the
      // nearest saved word's answer, else HQ's answer for a word nothing
      // matches, saying the demo has no search for the word asked.
      const asked = url.pathname === "/search/" ? url.searchParams.get("q")?.trim() : "";
      const word = asked && demo.searchKey(asked, found);
      const near = word && (saved[`${word}|${name}`] || found.variants[word]);
      if (word) unsearched = demo.nearest(asked, found) === found.nothing ? { asked, nothing: found.nothing } : null;
      if (near) return plainFetch(near);
      // The page itself: the file it was served from, with the headers HQ sent.
      if (demo.key(url, found) === demo.key(shownAt, found)) {
        return new Response(await (await plainFetch(demo.served)).text(), { headers: await sent() });
      }
      // Another address with a query: its saved copy, a whole page the
      // region is found in; else nothing, and the link it enhanced is followed.
      const copy = found.variants[demo.key(url, found)];
      if (copy) return plainFetch(copy);
      // Any other post with no saved answer is answered with nothing changed, as above.
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
    // The empty info notice the page drew for a script to fill (base.html).
    const note = document.querySelector('template[data-notice="info"]').content.firstElementChild.cloneNode(true);
    note.textContent = words;
    const item = document.createElement("li");
    item.append(note);
    list.replaceChildren(item);
    main.scrollIntoView({ block: "start" });
  };

  // A list's own search asks HQ as each word is typed, and the demo has none
  // of those saved: it says so as typing starts, rather than reloading the
  // page under the reader's hands. Enter still asks, and is answered as any
  // query with no saved copy. The search page's box searches everything, and
  // is answered as the command center is.
  const SEARCHING_A_LIST = "Searching within a list isn't saved in the demo. Find anything, at the top of the page, searches everything.";
  window.addEventListener("input", (event) => {
    const box = event.target.closest?.("[data-table-toolbar] input[type=search]");
    if (!box || new URL(box.form.action, location.href).pathname === "/search/") return;
    event.stopImmediatePropagation();
    if (document.querySelector("#content > .messages")?.textContent !== SEARCHING_A_LIST) say(SEARCHING_A_LIST);
  }, true);

  // A query the demo has no saved copy of: the page is shown without it, and
  // says what was left out. demo/build.py saves no sorts, the first pages of
  // a list (_MOST_PAGES_OF_A_LIST), every filter a page offers one at a
  // time, what is linked up to two copies from a page (_COPY_DEPTH), so the
  // days around today, and no search within a list.
  const unanswered = () => {
    const kind = demo.unanswered;
    if (kind === "sort") say("Sorting isn't saved in the demo, so the list is shown in its usual order.");
    else if (kind === "page") say("Only the first pages of a list are saved in the demo, so this shows the first.");
    else if (kind === "day") say("Only the days around today are saved in the demo, so this is shown without the one asked for.");
    else if (kind === "search") say(SEARCHING_A_LIST);
    else say("This filter isn't in the demo, so the page is shown without it.");
  };
  // A search answered as the saved search that finds nothing keeps the
  // word asked in its box, and says the demo has no search for it.
  const answeredAs = async () => {
    const { nothing } = await demo.routes();
    if (demo.answeredAs !== nothing) {
      say(`The demo has no saved search for that, so this shows results for “${demo.answeredAs}”.`);
      return;
    }
    marked(document, new URLSearchParams(location.search).get("q") || "", nothing);
  };

  document.addEventListener("DOMContentLoaded", () => {
    if (demo.unanswered) unanswered();
    if (demo.answeredAs) answeredAs();

    // Re-apply saved dismissals.
    const state = read();
    document.querySelectorAll("[data-attention-item]").forEach((row) => {
      const key = row.querySelector(':scope > .row-actions button[name="key"]')?.value;
      if (key && state.dismissed.includes(key)) {
        row.dataset.dismissed = `Dismissed: ${row.querySelector("[data-attention-title]")?.textContent.trim() || ""}`;
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

  // Save dismissals and un-dismissals.
  document.addEventListener("click", (event) => {
    // The row's own Dismiss or Restore, as app.js finds it.
    const button = event.target.closest('[data-attention-item] .row-actions button[name="key"][formaction]');
    if (!button) return;
    const state = read();
    const dismissing = /set-aside/.test(button.getAttribute("formaction"));
    state.dismissed = dismissing ? [...new Set([...state.dismissed, button.value])] : state.dismissed.filter((key) => key !== button.value);
    write(state);
  }, true);
  // Every other form that posts shows the note instead. A filter or a search
  // is a GET, answered from its saved copy like any address with a query. A
  // post to an address the demo saved an answer from (a Refresh) asks for a
  // reading again, and says the demo can't; an ask's own control says so
  // where it was pressed. A read command's form (a lookup) has nothing to
  // save, and says the demo can't run it. Prevented here, before app.js
  // hears it, the form is never marked as sending.
  document.addEventListener("submit", async (event) => {
    if (event.defaultPrevented) return;
    if (event.submitter?.matches("[data-ask-button]")) return;
    if ((event.target.getAttribute("method") || "get").toLowerCase() === "get") return;
    event.preventDefault();
    // A button's formAction is the page's own address when it names none.
    const button = event.submitter?.hasAttribute("formaction") ? event.submitter : null;
    const action = new URL(button?.formAction || event.target.action, location.href).pathname;
    const [saved, found] = await Promise.all([answers(), demo.routes()]);
    if (Object.keys(saved).some((address) => address.split(/[?|]/)[0] === action)) say(READ_AGAIN);
    else if (found.reads.includes(action)) say("The demo can't run a read: it holds only the answers saved with it.");
    else say("Demo only: nothing was saved.");
  });
})();
