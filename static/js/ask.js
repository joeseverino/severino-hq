"use strict";

// Asked-for work: one behavior for every "do this now", as docs/Design.md's
// "Asked-for work is an ask" tells it. A press is a post fragment.js answers
// in place, and the server draws the control from what it stores: while the
// work is live the control is a region the page asks for again as what the
// work is drawn from changes (`data-fragment-live`). This lands the work once
// a control drawn live is drawn ended. Drawn by partials/_ask.html,
// _job_progress.html and _visit_refresh.html as `[data-ask]`:
//
//   data-ask-refresh  a selector for the part of the page the result shows
//                     in, fetched again when the work ends; without one the
//                     page's content, a region of its own (_page_main.html).
//                     A part around the control that names its own address
//                     (`data-fragment`, a dialog's panel) is fetched from
//                     there. Only a part that cannot be had loads the page
//   form[data-ask-auto] asks when the page is showing, with nobody pressing
//                     anything
//   data-ask-outcome  the page draws how the work ended, failure included
//   data-ask-said     what an ask nobody pressed says of its work, in the
//                     tone `data-ask-tone` names, which it says in its note
//   [data-ask-note]   the live region. One drawn apart from its control is
//                     an <output> naming the control's form (`form=`), as a
//                     page's note does
//   [data-ask-elapsed] ticks beside it, outside the region
//   [data-reading-age] a page head's reading line, "Last read …", which a
//                     control in that head says how its work stands over
(() => {
  const RELOAD_GAP_MS = 120_000;
  // Where the result shows when the control names no part.
  const CONTENT = "#content";
  const reloadedKey = `hq.ask.reloaded:${window.location.pathname}`;
  // The controls drawn live, by id: one drawn ended in its place has landed.
  const live = new Set();

  let touched = false;
  ["pointerdown", "keydown", "wheel", "touchstart"].forEach((type) =>
    document.addEventListener(type, () => { touched = true; }, { once: true, passive: true, capture: true }),
  );
  const showing = () => document.visibilityState === "visible";
  // An ask nobody pressed (_visit_refresh.html) has nobody waiting on it, so
  // it says only a failure: its result shows as the part it reads drawn again.
  const unpressed = (control) => control.matches(".ask-quiet");
  const isLive = (control) => ["queued", "running"].includes(control.dataset.askState);

  // The server's `moments.Span` under a day: the largest whole unit alone.
  const elapsed = (seconds) => {
    const [count, unit] = [[3600, "hour"], [60, "minute"], [1, "second"]]
      .map(([size, name]) => [Math.floor(seconds / size), name])
      .find(([whole]) => whole >= 1) ?? [0, "second"];
    return `${count}\u00a0${unit}${count === 1 ? "" : "s"}`;
  };
  const tick = (control) => {
    const slot = control.querySelector("[data-ask-elapsed]");
    if (!slot) return;
    const since = Number(control.dataset.askSince || 0);
    slot.textContent = since ? elapsed(Math.max(0, Math.round((Date.now() - since) / 1000))) : "";
  };
  hqEvery(() => document.querySelectorAll("[data-ask][data-ask-since]").forEach(tick), { ms: 1000 });

  const frame = (selector) => document.querySelector(selector).content.cloneNode(true);
  // A note drawn apart from its control (a page's, _visit_refresh.html) is
  // found through the form it names, so a note drawn again finds it anew.
  const noteOf = (control) =>
    control.querySelector("[data-ask-note]") ||
    [...control.querySelectorAll("form")].flatMap((form) => [...form.elements]).find((field) => field.matches("[data-ask-note]"));

  // A note and a label take the state's tone class, as the server draws them
  // (`tone_class`, work_runs.TONES), and no color of their own.
  const retone = (element, tone) => {
    const toned = [...element.classList].filter((name) => name.startsWith("tone-"));
    element.classList.remove(...toned);
    if (tone) element.classList.add(`tone-${tone}`);
  };
  const toneOf = (element) =>
    [...(element?.classList ?? [])].find((name) => name.startsWith("tone-"))?.slice("tone-".length) ?? "";

  // A control in a page's head says how its work stands over the head's
  // reading line (partials/_reading_age.html), whose own words stay drawn
  // and keep its room, so nothing in the head grows or moves. Under way it
  // says one word, the same for every kind of work; a failure says its
  // sentence, cut to the line, whole in its title. The note stays the live
  // region, heard rather than seen there (pages.css). Once the result lands
  // the head is drawn again, its line saying the new reading's age.
  // The line is the nearest one around the control inside the head, so a
  // control of one reading in a head of several (the dashboard's strip)
  // speaks over that reading's line alone.
  const lineOf = (control) => {
    const head = control.closest(".page-head");
    for (let scope = control.parentElement; head?.contains(scope); scope = scope.parentElement) {
      const line = scope.querySelector("[data-reading-age]");
      if (line || scope === head) return line;
    }
    return null;
  };
  const echo = (control, said) => {
    const line = lineOf(control);
    if (!line) return;
    const state = control.dataset.askState;
    const shown =
      state === "failed" ? said : state === "idle" || unpressed(control) ? "" : document.body.dataset.askRefreshing;
    // Speaking for the work, the line takes its note's tone; speaking for its
    // reading again, the tone the server drew it with.
    if (!("drawnTone" in line.dataset)) line.dataset.drawnTone = toneOf(line);
    retone(line, shown ? (state === "failed" ? toneOf(noteOf(control)) : "") : line.dataset.drawnTone);
    if (shown) {
      line.dataset.askSaid = shown;
      line.title = shown;
    } else {
      delete line.dataset.askSaid;
      line.removeAttribute("title");
    }
  };

  // A table's row shows the note's word alone, and a phone has no title to
  // hover: the row's open line says the word and the sentence, under the
  // button's label. The server draws it there (`data-ask-why`, _ask.html,
  // through table_tags._detail_row) when the row is drawn; a control drawn
  // again apart from its row clones its entry, or the whole line, from the
  // frame `{% open_line %}` draws (_ask.html), once, and the row is marked as
  // holding a block there (`data-below`), which shows its toggle (tables.css).
  const sayBelow = (control, said) => {
    const row = control.closest(".data-table > tbody > tr:not(.row-detail)");
    const button = control.querySelector("[data-ask-button]");
    if (!row || !button) return;
    const key = button.getAttribute("formaction") || "";
    const detail = row.nextElementSibling?.matches("[data-row-detail]") ? row.nextElementSibling : null;
    const copies = [control, detail].filter(Boolean)
      .flatMap((root) => [...root.querySelectorAll("[data-ask-why]")].filter((why) => why.dataset.askWhy === key));
    if (!said) {
      copies.forEach((why) => (why.closest(".row-columns > *") || why).remove());
      row.toggleAttribute("data-below", Boolean(detail?.querySelector(".row-below")));
      return;
    }
    if (copies.length) {
      copies.forEach((why) => { why.textContent = said; });
      return;
    }
    const line = frame("template[data-ask-below]").firstElementChild;
    const why = line.querySelector("[data-ask-why]");
    why.textContent = said;
    why.dataset.askWhy = key;
    why.dataset.label = button.textContent.trim();
    const entry = why.closest(".row-columns > *");
    entry.querySelector("dt").textContent = why.dataset.label;
    row.toggleAttribute("data-below", true);
    if (!detail) {
      line.cells[0].colSpan = [...row.cells].reduce((width, cell) => width + cell.colSpan, 0);
      row.after(line);
      return;
    }
    const cell = detail.cells[0];
    const list = cell.querySelector(":scope > .row-columns");
    if (list) list.append(entry);
    else cell.append(entry.parentElement);
  };

  // What a control the server drew says beyond its own markup: the head's
  // line, the row's open line, the note an unpressed ask names, its clock.
  const drawn = (control) => {
    const note = noteOf(control);
    if ("askSaid" in control.dataset && note) {
      note.textContent = control.dataset.askSaid;
      retone(note, control.dataset.askTone || "");
      if ("askCut" in note.dataset) {
        if (control.dataset.askSaid) note.title = control.dataset.askSaid;
        else note.removeAttribute("title");
      }
    }
    const word = note?.querySelector(".ask-word")?.textContent ?? "";
    const sentence = note?.querySelector(".ask-sentence")?.textContent ?? "";
    const said = control.dataset.askState === "idle" ? "" : (word && sentence ? `${word}: ${sentence}` : note?.textContent ?? "");
    echo(control, sentence || said);
    if (!unpressed(control)) sayBelow(control, sentence ? said : "");
    if (isLive(control)) {
      const seconds = Number(control.querySelector("[data-ask-elapsed]")?.dataset.seconds || 0);
      if (!control.dataset.askSince) control.dataset.askSince = String(Date.now() - seconds * 1000);
    }
    tick(control);
  };

  // sessionStorage can be unavailable; then the page never reloads itself.
  const reloadedRecently = () => {
    try {
      return Date.now() - Number(window.sessionStorage.getItem(reloadedKey) || 0) < RELOAD_GAP_MS;
    } catch {
      return true;
    }
  };
  // The whole page again, so everything on it is the same reading. Not under
  // somebody's hands, and never twice running: then it says the result is in
  // and leaves showing it to them.
  const reload = (control) => {
    const offer = () => {
      const note = noteOf(control);
      if (!note) return;
      // HQ writes the words (asks.NEWER_READING), so a page's note keeps
      // their room (_visit_refresh.html).
      const offered = frame("template[data-ask-newer]");
      offered.querySelector("a").href = window.location.href;
      note.replaceChildren(offered);
      retone(note, "");
      note.hidden = false;
      note.closest(".visually-hidden")?.classList.remove("visually-hidden");
      control.dataset.askState = "idle";
      echo(control, "");
    };
    if (touched || !showing() || reloadedRecently()) return offer();
    try {
      window.sessionStorage.setItem(reloadedKey, String(Date.now()));
    } catch {
      return offer();
    }
    window.location.reload();
  };

  // The named part of this page as the server draws it now, swapped in place.
  // Nobody pressed anything for it, so it never takes the page away.
  const refresh = (control, selector) => {
    const region = control.closest(selector);
    if (region?.dataset.fragment !== undefined && region !== control) return hqFragment.swap(region, { renew: false });
    return hqFragment.swap([selector], { renew: false });
  };

  // A failure is said where it was asked. A surface that draws the outcome
  // itself (`data-ask-outcome`) is loaded again however the work ended.
  const land = async (control) => {
    if (control.dataset.askState === "failed" && !("askOutcome" in control.dataset)) return;
    try {
      await refresh(control, control.dataset.askRefresh || CONTENT);
    } catch {
      reload(control);
    }
  };

  // Each control placed: said, and landed once one drawn live is drawn ended.
  // Copies of one control (a head's and its menu's) land once.
  const placed = (root) => {
    const landing = new Map();
    [...(root.matches?.("[data-ask]") ? [root] : []), ...root.querySelectorAll("[data-ask]")].forEach((control) => {
      drawn(control);
      const id = control.id.replace(/-menu$/, "");
      if (!id) return;
      if (isLive(control)) live.add(id);
      else if (live.delete(id)) landing.set(control.dataset.askRefresh || CONTENT, control);
    });
    landing.forEach((control) => land(control));
  };

  // Only while the page is showing: a tab in the background asks for nothing,
  // and asks again when it is brought back, which costs nothing if HQ finds
  // the readings still fresh. The server decides what is read and whether it
  // is due; this only says the page is being looked at, and is answered with
  // the control drawn again.
  // One inside a shut dialog is not being looked at: it asks when the dialog
  // opens, or when the part holding it arrives in an open one. Each asks once
  // while the page is showing, so the control it is answered with, placed
  // again, does not ask again.
  const asked = new Set();
  const askAuto = (root = document) => {
    if (!showing()) return;
    root.querySelectorAll("[data-ask] form[data-ask-auto]").forEach((form) => {
      if (form.closest("dialog:not([open])")) return;
      const control = form.closest("[data-ask]");
      if (isLive(control) || asked.has(control.id)) return;
      if (control.id) asked.add(control.id);
      hqFragment
        .swap(control, { url: form.action, method: "POST", body: new FormData(form), renew: false })
        .catch(() => {});
    });
  };

  // A press refused at once (fragment.js) stored nothing to draw, so the
  // control says it as it says a failure the server drew: its sentence, in
  // the words and tone of the frame, the button's title, the row's open line.
  document.addEventListener("hq:refused", (event) => {
    const control = event.target.closest("[data-ask]");
    const note = control && noteOf(control);
    if (!note) return;
    event.preventDefault();
    const failed = frame("template[data-ask-refused]").firstElementChild;
    const said = event.detail.join(" ");
    failed.querySelector(".ask-sentence").textContent = said;
    note.replaceChildren(...failed.childNodes);
    retone(note, toneOf(failed));
    control.dataset.askState = "failed";
    control.querySelector("[data-ask-button]")?.setAttribute("title", said);
    drawn(control);
  });

  placed(document);
  askAuto();
  document.addEventListener("visibilitychange", () => {
    asked.clear();
    askAuto();
  });
  // A part of the page drawn again may hold work under way, ended, or an ask
  // of its own.
  document.addEventListener("hq:fragment", (event) => {
    placed(event.target);
    askAuto(event.target);
  });
  // `toggle` does not bubble.
  document.addEventListener(
    "toggle",
    (event) => {
      if (!(event.target instanceof HTMLDialogElement && event.target.open)) return;
      event.target.querySelectorAll("[data-ask]").forEach((control) => asked.delete(control.id));
      askAuto(event.target);
    },
    true,
  );
})();
