"use strict";

// Asked-for work: one behavior for every "do this now".
//
// A request never does the work. It stores the ask and answers at once with
// how the work stands and the address of a status resource (202 while it is
// live); the controller or a job does the work, and the control follows that
// resource until it ends. A read the controller takes, a job, and the readings
// a page asks for when it is opened are all this: `[data-ask]`, drawn by
// partials/_ask.html, _job_progress.html and _visit_refresh.html.
//
//   data-ask-status   the status resource, present while the work is live, so
//                     a page loaded mid-flight resumes following it
//   data-ask-refresh  a selector for the part of the page the result shows
//                     in, fetched again when the work ends; without one the
//                     page's content, a region of its own (_page_main.html).
//                     A part around the control that names its own address
//                     (`data-fragment`, a dialog's panel) is fetched from
//                     there. Only a part that cannot be had loads the page
//   [data-ask-button] the button that asks; a form[data-ask-auto] asks when
//                     the page is showing, with nobody pressing anything
//   data-ask-outcome  the page draws how the work ended, failure included
//   [data-ask-note]   the live region: it changes only when the state does.
//                     One drawn apart from its control is an <output> naming
//                     the control's form (`form=`), as a page's note does
//   [data-ask-cut]    a note its line may cut, which holds its whole
//                     sentence in its title
//   [data-ask-elapsed] ticks beside it, outside the region
//   [data-reading-age] a page head's reading line, "Last read …", which a
//                     control in that head says how its work stands over
//
// Polling rather than a socket: one small question every couple of seconds
// while something is live, asked by the one loop every poll shares (`hqEvery`).
// A persistent connection would be a second transport to run and secure for a
// question that fits in a query.
(() => {
  const POLL_MS = 2000;
  // A tab nobody is looking at should not spend the night asking.
  const HIDDEN_POLL_MS = 15000;
  // Past this the work's own status has long since said how it ended.
  const FOLLOW_LIMIT_MS = 60 * 60 * 1000;
  const RELOAD_GAP_MS = 120_000;
  // Where the result shows when the control names no part.
  const CONTENT = "#content";
  const reloadedKey = `hq.ask.reloaded:${window.location.pathname}`;
  // Status resource -> the controls following it. A control drawn twice (a
  // head's button and its copy in the narrow menu) is one poll.
  const following = new Map();

  let touched = false;
  ["pointerdown", "keydown", "wheel", "touchstart"].forEach((type) =>
    document.addEventListener(type, () => { touched = true; }, { once: true, passive: true, capture: true }),
  );
  const showing = () => document.visibilityState === "visible";

  const elapsed = (seconds) => {
    if (seconds < 60) return `${seconds} s`;
    return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} s`;
  };
  const tick = (control) => {
    const slot = control.querySelector("[data-ask-elapsed]");
    if (!slot) return;
    const since = Number(control.dataset.askSince || 0);
    slot.textContent = since ? elapsed(Math.max(0, Math.round((Date.now() - since) / 1000))) : "";
  };
  hqEvery(() => document.querySelectorAll("[data-ask][data-ask-since]").forEach(tick), { ms: 1000 });

  // A note that says a word (`data-ask-word`, partials/_ask.html) says the
  // state's word and its sentence, of which a table cell shows the word: the
  // sentence is the button's title as well, which its reason is otherwise.
  const part = (name, text) => {
    const span = document.createElement("span");
    span.className = name;
    span.textContent = text;
    return span;
  };
  // A note drawn apart from its control (a page's, _visit_refresh.html) is
  // found through the form it names, so a note drawn again finds it anew.
  const noteOf = (control) =>
    control.querySelector("[data-ask-note]") ||
    [...control.querySelectorAll("form")].flatMap((form) => [...form.elements]).find((field) => field.matches("[data-ask-note]"));

  const say = (control, text, word = "") => {
    const note = noteOf(control);
    if (!note) return;
    // The server writes what the button is for apart from its title, which
    // holds the work's last note while there is one (`data-ask-reason`,
    // action_tags.ask_button).
    control.querySelectorAll("[data-ask-button]").forEach((button) => {
      const title = text || button.dataset.askReason || "";
      if (title) button.title = title;
      else button.removeAttribute("title");
    });
    // Work with nothing to say beyond its word says the word alone, wherever
    // the note stands (asks.restates, _ask.html).
    const worded = "askWord" in note.dataset;
    const alone = worded && !text ? word : text;
    // Written only when it changes: a live region reads out every write.
    const said = worded && text && word ? `${word}${text}` : alone;
    if (note.textContent === said) return;
    if (said === alone) note.textContent = alone;
    else note.replaceChildren(part("ask-word", word), part("ask-sentence", text));
    // A note its line may cut (`data-ask-cut`, a page's) holds the whole
    // sentence in its title.
    if ("askCut" in note.dataset) {
      if (said) note.title = said;
      else note.removeAttribute("title");
    }
    // A line of the head's own keeps its room whether or not it says anything.
    if (control.matches(".ask-quiet") && control.contains(note)) note.hidden = !said;
    if (worded) sayBelow(control, text && word ? `${word}: ${text}` : "");
    echo(control, text, word);
  };

  // A control in a page's head says how its work stands over the head's
  // reading line (partials/_reading_age.html), whose own words stay drawn
  // and keep its room, so nothing in the head grows or moves. Under way it
  // says one word, the same for every kind of work; a failure says its
  // sentence, cut to the line, whole in its title. The note stays the live
  // region, heard rather than seen there (pages.css). Once the result lands
  // the head is drawn again, its line saying the new reading's age.
  const echo = (control, text, word) => {
    const line = control.closest(".page-head")?.querySelector("[data-reading-age]");
    if (!line) return;
    const state = control.dataset.askState;
    const said = state === "failed" ? text || word : state === "idle" ? "" : document.body.dataset.askRefreshing;
    line.dataset.askState = state;
    if (said) {
      line.dataset.askSaid = said;
      line.title = said;
    } else {
      delete line.dataset.askSaid;
      line.removeAttribute("title");
    }
  };
  // A head the server drew with work under way or failed says so on its line.
  const echoDrawn = (root) => {
    root.querySelectorAll(".page-head [data-ask]").forEach((control) => {
      const note = noteOf(control);
      if (!note || control.dataset.askState === "idle") return;
      const word = note.querySelector(".ask-word")?.textContent ?? note.textContent;
      echo(control, note.querySelector(".ask-sentence")?.textContent ?? "", word);
    });
  };

  // A table's row shows the note's word alone, and a phone has no title to
  // hover: the row's open line says the word and the sentence, under the
  // button's label. The server draws it there (`data-ask-why`, _ask.html,
  // through table_tags._detail_row) when the work already stood somewhere;
  // otherwise it is drawn here as that line draws it, and opens the row's
  // toggle by being there (tables.css).
  const sayBelow = (control, said) => {
    const row = control.closest(".data-table > tbody > tr:not(.row-detail)");
    const button = control.querySelector("[data-ask-button]");
    if (!row || !button) return;
    const key = button.getAttribute("formaction") || "";
    let detail = row.nextElementSibling?.matches("[data-row-detail]") ? row.nextElementSibling : null;
    const copies = [control, detail].filter(Boolean)
      .flatMap((root) => [...root.querySelectorAll("[data-ask-why]")].filter((why) => why.dataset.askWhy === key));
    if (!said) {
      copies.forEach((why) => {
        const term = why.closest("dd")?.previousElementSibling;
        if (term?.matches("dt")) term.remove();
        (why.closest(".row-columns > dd") || why).remove();
      });
      return;
    }
    if (copies.length) {
      copies.forEach((why) => { why.textContent = said; });
      return;
    }
    const why = part("ask-why row-below", said);
    why.dataset.askWhy = key;
    why.dataset.label = button.textContent.trim();
    control.append(why);
    if (!detail) {
      detail = document.createElement("tr");
      detail.className = "row-detail";
      detail.dataset.rowDetail = "";
      detail.insertCell().colSpan = [...row.cells].reduce((width, cell) => width + cell.colSpan, 0);
      row.after(detail);
    }
    const cell = detail.cells[0];
    let list = cell.querySelector(":scope > .row-columns");
    if (!list) {
      list = document.createElement("dl");
      list.className = "row-columns";
      cell.append(list);
    }
    const term = document.createElement("dt");
    const value = document.createElement("dd");
    term.dataset.label = "";
    value.dataset.label = "";
    term.textContent = why.dataset.label;
    value.append(why.cloneNode(true));
    list.append(term, value);
  };

  // Draws one standing on a control. Everything a control shows comes through
  // here, from the server's first answer to the last poll.
  const draw = (control, standing) => {
    control.dataset.askState = standing.state;
    if (standing.live) {
      control.setAttribute("aria-busy", "true");
      if (!control.dataset.askSince) {
        control.dataset.askSince = String(Date.now() - (standing.seconds || 0) * 1000);
      }
    } else {
      control.removeAttribute("aria-busy");
      delete control.dataset.askSince;
      delete control.dataset.askStatus;
    }
    control.querySelectorAll("[data-ask-button]").forEach((button) => {
      // Busy, not disabled: a disabled button drops the focus it holds.
      if (standing.live) button.setAttribute("aria-disabled", "true");
      else button.removeAttribute("aria-disabled");
    });
    say(control, standing.note || "", standing.label || "");
    tick(control);
    const label = control.querySelector("[data-ask-label]");
    if (label && standing.label) label.textContent = standing.label;
    const bar = control.querySelector("[data-ask-bar]");
    if (bar) {
      // A <progress> with no value is indeterminate, which is what work that
      // cannot say how far along it is should draw.
      if (standing.percent !== null && standing.percent !== undefined) bar.value = standing.percent;
      else bar.removeAttribute("value");
    }
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
      const link = document.createElement("a");
      link.href = window.location.href;
      // HQ writes the words (asks.NEWER_READING), so a page's note keeps
      // their room (_visit_refresh.html).
      const { askNewer, askShow } = document.body.dataset;
      link.textContent = askShow;
      note.replaceChildren(part("ask-sentence", `${askNewer} `), link);
      note.hidden = false;
      note.closest(".visually-hidden")?.classList.remove("visually-hidden");
      control.dataset.askState = "idle";
      echo(control, "", "");
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
    if (region?.dataset.fragment) return hqFragment.swap(region, { renew: false });
    return hqFragment.swap([selector], { renew: false });
  };

  const land = async (control, standing) => {
    draw(control, standing);
    // A failure is said where it was asked. A surface that draws the outcome
    // itself (`data-ask-outcome`) is loaded again however the work ended.
    if (standing.state !== "done" && !("askOutcome" in control.dataset)) return;
    const selector = control.dataset.askRefresh || CONTENT;
    try {
      await refresh(control, selector);
    } catch {
      reload(control);
    }
  };

  const follow = (url) => {
    const controls = () => following.get(url) || new Set();
    const end = () => {
      controls().forEach((control) => draw(control, { state: "idle", live: false, note: "" }));
      following.delete(url);
      return false;
    };
    hqEvery(
      async () => {
        const response = await hqFetch(url, { credentials: "same-origin", renewSession: false });
        // Gone or refused is an end; anything else is a container restarting
        // under us, which will answer again.
        if (response.status === 404 || response.status === 403) return end();
        if (!response.ok) throw new Error(String(response.status));
        // eslint-disable-next-line no-restricted-syntax -- how asked-for work stands is data every copy of its control draws, not a region
        const standing = await response.json();
        if (standing.live) {
          controls().forEach((control) => draw(control, standing));
          return true;
        }
        const waiting = [...controls()];
        following.delete(url);
        // One control lands it, so one fetch refreshes the page; the rest
        // only say so.
        waiting.slice(1).forEach((control) => draw(control, standing));
        if (waiting.length) await land(waiting[0], standing).catch(() => {});
        return false;
      },
      { ms: POLL_MS, hiddenMs: HIDDEN_POLL_MS, limitMs: FOLLOW_LIMIT_MS, expired: end },
    );
  };

  const watch = (control, url) => {
    control.dataset.askStatus = url;
    const controls = following.get(url);
    if (controls) {
      controls.add(control);
      return;
    }
    following.set(url, new Set([control]));
    follow(url);
  };

  // A control the server drew mid-flight is followed from where it stands.
  const resume = (root) => {
    root.querySelectorAll("[data-ask][data-ask-status]").forEach((control) => {
      const seconds = Number(control.querySelector("[data-ask-elapsed]")?.dataset.seconds || 0);
      if (!control.dataset.askSince) control.dataset.askSince = String(Date.now() - seconds * 1000);
      tick(control);
      if (!following.get(control.dataset.askStatus)?.has(control)) watch(control, control.dataset.askStatus);
    });
  };

  // Posts an ask and takes up its answer. `quiet` is an ask nobody pressed:
  // it says nothing until there is something to wait for, and never takes the
  // page away to sign in.
  const ask = async (control, action, body, { quiet = false } = {}) => {
    if (control.getAttribute("aria-busy") === "true") return;
    if (!quiet) draw(control, { state: "queued", label: "Waiting", live: true, note: "Asking…", seconds: 0 });
    try {
      const response = await hqFetch(action, {
        method: "POST",
        body,
        credentials: "same-origin",
        renewSession: !quiet,
      });
      // eslint-disable-next-line no-restricted-syntax -- the ask's answer is how the work stands, data every copy of its control draws
      const standing = response.ok ? await response.json() : null;
      if (!standing) throw new Error("no answer");
      if (standing.live && standing.status) {
        // Every copy of the control on the page follows the same work.
        document.querySelectorAll("[data-ask]").forEach((other) => {
          const button = other.querySelector("[data-ask-button]");
          const same = other === control || (button && button.formAction === action && !quiet);
          if (!same) return;
          draw(other, standing);
          watch(other, standing.status);
        });
        return;
      }
      if (quiet && standing.state === "idle") return;
      await land(control, standing);
    } catch {
      if (!quiet) draw(control, { state: "failed", label: "Failed", live: false, note: "The request could not be sent." });
    }
  };

  document.addEventListener("submit", (event) => {
    const button = event.submitter;
    const control = button?.closest?.("[data-ask]");
    if (!control || !button.matches("[data-ask-button]")) return;
    event.preventDefault();
    ask(control, button.formAction, new FormData(event.target, button));
  });

  // Only while the page is showing: a tab in the background asks for nothing,
  // and asks again when it is brought back, which costs nothing if HQ finds
  // the readings still fresh. The server decides what is read and whether it
  // is due; this only says the page is being looked at.
  // One inside a shut dialog is not being looked at: it asks when the dialog
  // opens, or when the part holding it arrives in an open one.
  const askAuto = (root = document) => {
    if (!showing()) return;
    root.querySelectorAll("[data-ask] form[data-ask-auto]").forEach((form) => {
      if (form.closest("dialog:not([open])")) return;
      ask(form.closest("[data-ask]"), form.action, new FormData(form), { quiet: true });
    });
  };

  resume(document);
  echoDrawn(document);
  askAuto();
  document.addEventListener("visibilitychange", () => askAuto());
  // A part of the page drawn again may hold work already under way, or an
  // ask of its own.
  document.addEventListener("hq:fragment", (event) => {
    resume(document);
    echoDrawn(event.target);
    askAuto(event.target);
  });
  // `toggle` does not bubble.
  document.addEventListener(
    "toggle",
    (event) => {
      if (event.target instanceof HTMLDialogElement && event.target.open) askAuto(event.target);
    },
    true,
  );
})();
