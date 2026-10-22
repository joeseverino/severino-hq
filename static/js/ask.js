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
//                     page is loaded again
//   [data-ask-button] the button that asks; a form[data-ask-auto] asks when
//                     the page is showing, with nobody pressing anything
//   data-ask-outcome  the page draws how the work ended, failure included
//   [data-ask-note]   the live region: it changes only when the state does
//   [data-ask-elapsed] ticks beside it, outside the region
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

  // What a control says is drawn under it, over the page. Near an edge it is
  // moved back onto the screen, by however much of it would be off.
  const EDGE = 14;
  const keepOnScreen = (said) => {
    said.style.removeProperty("--ask-shift");
    const box = said.getBoundingClientRect();
    if (!box.width) return;
    const width = document.documentElement.clientWidth;
    const shift = Math.min(0, width - EDGE - box.right) || Math.max(0, EDGE - box.left);
    if (shift) said.style.setProperty("--ask-shift", `${Math.round(shift)}px`);
  };
  const placeSaid = () => document.querySelectorAll(".ask-said").forEach(keepOnScreen);
  new MutationObserver((changes) => {
    new Set(changes.map((change) => change.target.parentElement?.closest(".ask-said") || change.target.closest?.(".ask-said")))
      .forEach((said) => said && keepOnScreen(said));
  }).observe(document.body, { subtree: true, childList: true, characterData: true });
  window.addEventListener("resize", placeSaid);
  placeSaid();

  const say = (control, text) => {
    const note = control.querySelector("[data-ask-note]");
    if (!note) return;
    // Written only when it changes: a live region reads out every write.
    if (note.textContent !== text) note.textContent = text;
    if (control.matches(".ask-quiet")) note.hidden = !text;
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
    say(control, standing.note || "");
    tick(control);
    const label = control.querySelector("[data-ask-label]");
    if (label && standing.label) label.textContent = standing.label;
    const bar = control.querySelector("[data-ask-bar]");
    if (bar) {
      const known = standing.percent !== null && standing.percent !== undefined;
      bar.parentElement.classList.toggle("job-bar-unknown", !known);
      if (known) {
        bar.style.setProperty("--at", `${standing.percent}%`);
        bar.parentElement.setAttribute("aria-valuenow", String(standing.percent));
      } else {
        bar.parentElement.removeAttribute("aria-valuenow");
      }
    }
  };

  // sessionStorage can be unavailable; then the page never reloads itself.
  const reloadedRecently = () => {
    try {
      return Date.now() - Number(window.sessionStorage.getItem(reloadedKey) || 0) < RELOAD_GAP_MS;
    } catch (_error) {
      return true;
    }
  };
  // The whole page again, so everything on it is the same reading. Not under
  // somebody's hands, and never twice running: then it says the result is in
  // and leaves showing it to them.
  const reload = (control) => {
    const offer = () => {
      const note = control.querySelector("[data-ask-note]");
      if (!note) return;
      const link = document.createElement("a");
      link.href = window.location.href;
      link.textContent = "Show it";
      note.replaceChildren("A newer reading is in. ", link);
      note.hidden = false;
    };
    if (touched || !showing() || reloadedRecently()) return offer();
    try {
      window.sessionStorage.setItem(reloadedKey, String(Date.now()));
    } catch (_error) {
      return offer();
    }
    window.location.reload();
  };

  // The named part of this page as the server draws it now, swapped in place.
  // Nobody pressed anything for it, so it never takes the page away.
  const refresh = (selector) => hqFragment.swap([selector], { renew: false });

  const land = async (control, standing) => {
    draw(control, standing);
    // A failure is said where it was asked. A surface that draws the outcome
    // itself (`data-ask-outcome`) is loaded again however the work ended.
    if (standing.state !== "done" && !("askOutcome" in control.dataset)) return;
    const selector = control.dataset.askRefresh;
    if (!selector) return reload(control);
    try {
      await refresh(selector);
    } catch (_error) {
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
    if (!quiet) draw(control, { state: "queued", live: true, note: "Asking…", seconds: 0 });
    try {
      const response = await hqFetch(action, {
        method: "POST",
        body,
        credentials: "same-origin",
        renewSession: !quiet,
      });
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
    } catch (_error) {
      if (!quiet) draw(control, { state: "failed", live: false, note: "The request could not be sent." });
    }
  };

  document.addEventListener("submit", (event) => {
    const button = event.submitter;
    const control = button?.closest?.("[data-ask]");
    if (!control || !button.matches("[data-ask-button]")) return;
    event.preventDefault();
    const body = new FormData(event.target);
    if (button.name) body.set(button.name, button.value);
    ask(control, button.formAction, body);
  });

  // Only while the page is showing: a tab in the background asks for nothing,
  // and asks again when it is brought back, which costs nothing if HQ finds
  // the readings still fresh. The server decides what is read and whether it
  // is due; this only says the page is being looked at.
  const askAuto = () => {
    if (!showing()) return;
    document.querySelectorAll("[data-ask] form[data-ask-auto]").forEach((form) => {
      ask(form.closest("[data-ask]"), form.action, new FormData(form), { quiet: true });
    });
  };

  resume(document);
  askAuto();
  document.addEventListener("visibilitychange", askAuto);
  // A part of the page drawn again may hold work already under way.
  document.addEventListener("hq:fragment", () => resume(document));
})();
