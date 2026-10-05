"use strict";

// The one place in HQ where a string becomes markup.
//
// Progressive enhancement here is server-rendered HTML: a fragment is fetched
// and swapped in, so parsing a response is unavoidable, and
// `DOMParser.parseFromString` is a Trusted Types sink, which is exactly the
// point. The content policy names one policy and does not allow duplicates,
// so this is the only one that can ever exist on the page. Every other sink
// still throws, and script that gets itself onto the page cannot mint the
// permission to reach one, because the name is already taken and the object
// below is not reachable from anywhere else.
//
// What it accepts is narrow by construction rather than by inspection: every
// caller passes the body of a same-origin response HQ itself rendered. A
// string from anywhere else has no route to here.
const hqParseDocument = (() => {
  let policy = { createHTML: (html) => html };
  try {
    policy = window.trustedTypes.createPolicy("hq-fragment", {
      createHTML: (html) => html,
    });
  } catch {
    // A browser without Trusted Types, or a policy already created. Parsing
    // still has to work either way; where the browser does enforce, the
    // pass-through is what the sink refuses.
  }
  return (html) =>
    new DOMParser().parseFromString(policy.createHTML(html), "text/html");
})();

// Every enhanced request crosses the session boundary the same way. When an
// OIDC session needs renewal, the server returns the provider URL instead of
// letting fetch follow a cross-origin redirect that CSP correctly blocks.
// `renewSession` decides whether a request is allowed to take the page away.
// A request the operator made can: they asked for something, and renewing is
// the way to get it. A background one must not: the provider returns to the
// address that triggered the renewal, so a renewing background fetch would
// land the operator on the JSON that fetch wanted.
const hqFetch = async (input, options = {}) => {
  const { renewSession = true, ...rest } = options;
  const headers = new Headers(rest.headers);
  headers.set("X-Requested-With", "XMLHttpRequest");
  const response = await window.fetch(input, { ...rest, headers });
  const refreshUrl =
    response.status === 403 ? response.headers.get("refresh_url") : "";
  if (refreshUrl && renewSession) {
    window.location.assign(refreshUrl);
    return new Promise(() => {});
  }
  return response;
};
window.hqFetch = hqFetch;

// Disclosure menus that dismiss on an outside click or Escape. One selector
// covers every such menu, so adding another is handled by construction rather
// than by remembering to extend a hardcoded query: a menu says for itself
// that it dismisses.
const DISMISSIBLE_MENUS = "details[data-menu]";

// A panel that hangs from a disclosure stays on screen. Each is placed from
// its own edge in the stylesheet, so one near the right of a row (the last
// filter in a toolbar) can overflow a phone. Measured when it opens: a panel that
// would cross the viewport's right edge hangs from its disclosure's right
// edge instead. One rule for every panel, so a new one needs nothing here.
const EDGE_MARGIN = 8;
document.addEventListener(
  "toggle",
  (event) => {
    const details = event.target;
    if (!(details instanceof HTMLDetailsElement)) return;
    const panel = [...details.children].find((child) => child.tagName !== "SUMMARY");
    if (!panel || getComputedStyle(panel).position !== "absolute") return;
    delete panel.dataset.edge;
    if (!details.open) return;
    if (panel.getBoundingClientRect().right > document.documentElement.clientWidth - EDGE_MARGIN) {
      panel.dataset.edge = "end";
    }
  },
  true,
);
const sectionMenuOpen = document.querySelector(".nav-toggle-open");
const sectionMenuClose = document.querySelector(".nav-toggle-close");
const sectionMenuBackdrop = document.querySelector(".nav-backdrop");

function setSectionMenu(open, { restoreFocus = false } = {}) {
  document.body.classList.toggle("nav-is-open", open);
  // Both, not just the one that opens: CSS shows exactly one of them at a
  // time, so a state written to only one half is a state written to whichever
  // control happens to be hidden.
  [sectionMenuOpen, sectionMenuClose].forEach((control) =>
    control?.setAttribute("aria-expanded", String(open)),
  );
  if (restoreFocus) {
    const control = open ? sectionMenuClose : sectionMenuOpen;
    control?.focus({ preventScroll: true });
  }
}

sectionMenuOpen?.addEventListener("click", (event) => {
  event.preventDefault();
  closeMenus(null);
  // A pointer already communicates where the interaction happened. Move
  // focus only for keyboard activation, otherwise mobile Safari paints a
  // persistent focus ring around the replacement close control.
  setSectionMenu(true, { restoreFocus: event.detail === 0 });
});

[sectionMenuClose, sectionMenuBackdrop].forEach((control) => {
  control?.addEventListener("click", (event) => {
    event.preventDefault();
    setSectionMenu(false, {
      restoreFocus: control === sectionMenuClose && event.detail === 0,
    });
  });
});

function closeMenus(except) {
  document.querySelectorAll(DISMISSIBLE_MENUS).forEach((menu) => {
    if (menu.open && menu !== except) {
      menu.removeAttribute("open");
      delete menu.dataset.pinned;
    }
  });
}

document.addEventListener("click", (event) => {
  const menu = event.target.closest(DISMISSIBLE_MENUS);
  // A click pins the menu open, so a hover-opened panel does not evaporate the
  // moment the pointer leaves on its way to the item being clicked.
  if (menu && event.target.closest("summary")) {
    menu.dataset.pinned = "true";
    // Category disclosures live inside the mobile section drawer. They must
    // be allowed to open without dismissing their own parent; peer menus such
    // as the user menu still dismiss the drawer.
    if (!menu.closest(".primary-nav")) setSectionMenu(false);
  }
  // The clicked menu is left alone: the browser handles its own summary
  // toggle. Every other open menu closes, so two panels are never stacked.
  closeMenus(menu);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeMenus(null);
    if (document.body.classList.contains("nav-is-open")) {
      setSectionMenu(false, { restoreFocus: true });
    }
  }
});

// The queue spans every installed domain and may include remote reads. Asking
// for its count in the base context would make every page pay that cost before
// first paint. The dashboard and queue already know it; everywhere else loads
// it only when the operator opens the menu that displays it.
// The unread count on the header's action items button. Fetched after the page
// so no page pays for it while rendering, and remembered for a minute so
// moving between pages does not ask again. The action items page renders the
// count itself and hands it over, which is what makes marking read show at once.
const actionMenu = document.querySelector("[data-action-count-url]");
if (actionMenu) {
  const STORE_KEY = "hq.actionCount";
  const TTL_MS = 60_000;
  const paint = (count) => {
    document.querySelectorAll("[data-action-count]").forEach((badge) => {
      badge.textContent = String(count);
      badge.hidden = count === 0;
    });
  };
  const remember = (count) => {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({ count, at: Date.now() }));
    } catch (_error) {
      // Storage refused: the next page asks again.
    }
  };
  const recalled = () => {
    try {
      const stored = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
      return stored && Date.now() - stored.at < TTL_MS ? stored.count : null;
    } catch (_error) {
      return null;
    }
  };

  if (actionMenu.dataset.actionCountFresh !== undefined) {
    remember(Number(actionMenu.dataset.actionCountFresh));
  } else if (recalled() !== null) {
    paint(recalled());
  } else {
    hqFetch(actionMenu.dataset.actionCountUrl, {
      headers: { Accept: "application/json" },
      renewSession: false,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (payload) {
          paint(payload.count);
          remember(payload.count);
        }
      })
      .catch(() => {});
  }
}

// Hover-to-open, for pointers only. On touch there is no hover: the first tap
// would open a menu and the second would be needed to follow a link, so those
// devices keep plain click behaviour.
if (window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
  // Small delays absorb the pointer crossing a menu on its way somewhere else,
  // and the gap between the summary and its panel.
  const OPEN_DELAY_MS = 90;
  const CLOSE_DELAY_MS = 240;

  document.querySelectorAll("details.nav-group").forEach((menu) => {
    let timer;

    menu.addEventListener("mouseenter", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        menu.setAttribute("open", "");
        closeMenus(menu);
      }, OPEN_DELAY_MS);
    });

    menu.addEventListener("mouseleave", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        // A menu the operator deliberately clicked stays put until they
        // dismiss it; only hover-opened menus close themselves.
        if (!menu.dataset.pinned) menu.removeAttribute("open");
      }, CLOSE_DELAY_MS);
    });
  });
}

document.addEventListener("change", (event) => {
  const control = event.target.closest("[data-submit-on-change]");
  if (control?.form) control.form.requestSubmit();
});

// Dense pages provide only stable section metadata and ordinary fragment
// targets. HQ measures its own chrome and marks the section currently being
// read; links and history still work as plain HTML when this enhancement is
// unavailable.
// The height of the chrome is a fact about every page, not only the ones with
// a section nav. Measured here so `--site-header-height` is true at every
// breakpoint: the header's padding changes on narrow screens, and the
// stylesheet's static fallback is only right at one of them. Once per load
// and per resize; nothing reads layout while scrolling.
(() => {
  const header = document.querySelector(".site-header");
  if (!header) return;
  const measureHeader = () => {
    document.documentElement.style.setProperty(
      "--site-header-height",
      `${header.getBoundingClientRect().height}px`,
    );
  };
  measureHeader();
  window.addEventListener("resize", measureHeader);
  window.addEventListener("load", measureHeader);
})();

(() => {
  const navigation = document.querySelector("[data-page-navigation]");
  if (!navigation) return;

  const links = [...navigation.querySelectorAll("[data-page-nav-link]")];
  const sections = links
    .map((link) => document.getElementById(link.hash.slice(1)))
    .filter(Boolean);
  let frame = null;

  const measure = () => {
    const header = document.querySelector(".site-header");
    document.documentElement.style.setProperty(
      "--site-header-height",
      `${header?.getBoundingClientRect().height || 0}px`,
    );
    document.documentElement.style.setProperty(
      "--page-nav-height",
      `${navigation.getBoundingClientRect().height}px`,
    );
  };

  const update = () => {
    frame = null;
    measure();
    const threshold =
      parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--site-header-height"))
      + navigation.getBoundingClientRect().height + 16;
    let current = sections[0];
    sections.forEach((section) => {
      if (section.getBoundingClientRect().top <= threshold) current = section;
    });
    // The last section often cannot reach the reading line because the footer
    // leaves no page below it. At the document end it is nevertheless the
    // section being read, and the local map should say so. Only once the page
    // has moved: a page short enough to show whole is at its end before
    // anybody scrolls, and the reader is at the top of it.
    if (window.scrollY > 0
      && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) {
      current = sections.at(-1);
    }
    links.forEach((link) => {
      if (current && link.hash === `#${current.id}`) {
        link.setAttribute("aria-current", "location");
        const list = link.closest(".page-nav-list");
        const linkRect = link.getBoundingClientRect();
        const listRect = list.getBoundingClientRect();
        if (linkRect.left < listRect.left) list.scrollLeft -= listRect.left - linkRect.left;
        if (linkRect.right > listRect.right) list.scrollLeft += linkRect.right - listRect.right;
      } else {
        link.removeAttribute("aria-current");
      }
    });
  };

  const schedule = () => {
    if (frame === null) frame = window.requestAnimationFrame(update);
  };
  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule);
  window.addEventListener("hashchange", schedule);
  schedule();
})();

// Dismissing a queue row, or restoring one, happens in place: the row's own
// button posts as it would without script, and the row answers at once
// instead of the whole queue being composed and drawn again. A dismissed row
// becomes one quiet line with the way back; the counts over it and the
// header's follow. "Dismiss all" and a family's "Dismiss these" stay ordinary
// posts, since what they cover is the server's to say.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-triage] button[formaction]");
  if (!button || event.defaultPrevented) return;
  const row = button.closest("[data-attention-item]");
  const form = document.getElementById(button.getAttribute("form"));
  if (!row || !form) return;
  event.preventDefault();
  const was = { action: button.getAttribute("formaction"), label: button.textContent };
  const other = button.dataset.other ? JSON.parse(button.dataset.other) : null;
  const dismissing = !("dismissed" in row.dataset) && !row.closest(".action-items-aside");
  const body = new FormData(form);
  body.set(button.name, button.value);
  button.disabled = true;
  hqFetch(was.action, { method: "POST", body, credentials: "same-origin" })
    .then((response) => {
      if (!response.ok) throw new Error(String(response.status));
      // Counts over the row: its family's, its domain's, its section's.
      const step = dismissing ? -1 : 1;
      const counts = new Set();
      for (let box = row.parentElement; box; box = box.parentElement) {
        const count = box.querySelector(":scope > summary [data-queue-count], :scope > h3 [data-queue-count], :scope > .section-head [data-queue-count]");
        if (count) counts.add(count);
      }
      counts.forEach((count) => { count.textContent = String(Math.max(0, Number(count.textContent) + step)); });
      if (!row.closest(".action-items-aside")) {
        document.querySelectorAll("[data-action-count]").forEach((badge) => {
          const next = Math.max(0, Number(badge.textContent || 0) + step);
          badge.textContent = String(next);
          badge.hidden = next === 0;
        });
        try { sessionStorage.removeItem("hq.actionCount"); } catch (_error) { /* asked again next page */ }
      }
      // The button becomes its opposite, posting to the other route.
      if (dismissing) row.dataset.dismissed = `Dismissed: ${row.querySelector(".attention-title")?.textContent.trim() || ""}`;
      else delete row.dataset.dismissed;
      button.dataset.other = JSON.stringify(was);
      if (other) {
        button.setAttribute("formaction", other.action);
        button.textContent = other.label;
      } else {
        const routes = document.querySelector("[data-triage-routes]")?.dataset;
        button.setAttribute("formaction", dismissing ? routes?.restore : routes?.dismiss);
        button.textContent = dismissing ? "Undo" : "Dismiss";
      }
    })
    // Whatever went wrong, the plain form still works.
    .catch(() => { button.setAttribute("form", form.id); form.requestSubmit ? form.requestSubmit(button) : form.submit(); })
    .finally(() => { button.disabled = false; });
});

// Long-running forms stay ordinary HTML forms: uploads and commands still work
// without JavaScript and keep Django's redirect/error semantics. Enhancement
// only makes the committed state explicit, prevents accidental double-submit,
// and gives assistive technology a live status while the server works.
document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-submit-busy]");
  if (!form) return;

  form.setAttribute("aria-busy", "true");
  form.querySelectorAll("button[type=submit], input[type=submit]").forEach((control) => {
    control.disabled = true;
    if (control instanceof HTMLButtonElement && form.dataset.submitLabel) {
      control.textContent = form.dataset.submitLabel;
    }
  });

  const status = form.querySelector("[data-submit-status]");
  if (status) status.hidden = false;
});

// A command preview reads controls already on the page. It performs no fetch,
// creates no second command model, and leaves the server-rendered fallback in
// place; the deployed capability remains the only source of execution truth.
document.querySelectorAll("form[data-command-form]").forEach((form) => {
  const preview = form.parentElement?.querySelector("[data-command-preview]");
  if (!preview) return;

  const update = () => {
    preview.querySelectorAll("[data-command-value]").forEach((output) => {
      const control = form.elements.namedItem(output.dataset.commandValue);
      if (!(control instanceof HTMLInputElement
        || control instanceof HTMLTextAreaElement
        || control instanceof HTMLSelectElement)) return;
      let value = control.value.trim();
      if (control instanceof HTMLSelectElement && value) {
        value = control.selectedOptions[0]?.textContent?.trim() || value;
      }
      output.textContent = value || output.dataset.empty;
    });
  };
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  const target = form.elements.namedItem("__target");
  if (form.hasAttribute("data-command-hydrate-target")
    && target instanceof HTMLSelectElement) {
    target.addEventListener("change", () => {
      if (!target.value) return;
      const url = new URL(window.location.href);
      url.searchParams.set("target", target.value);
      window.location.assign(url);
    });
  }
  update();
});

// Modals. A trigger is always a real link to a page that does the same job, so
// this only intercepts when there is in fact a dialog here to open, otherwise
// the link is followed and the operator lands on the full page instead.
document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-modal-open]");
  if (opener) {
    const dialog = document.getElementById(`modal-${opener.dataset.modalOpen}`);
    if (typeof dialog?.showModal === "function") {
      event.preventDefault();
      dialog.showModal();
    }
    return;
  }
  if (event.target.closest("[data-modal-close]")) {
    event.target.closest("dialog")?.close();
  }
});

document.querySelectorAll("dialog.modal").forEach((dialog) => {
  // The dialog element is the backdrop's hit target; its content sits in an
  // inner panel, so a click landing on the dialog itself was outside the panel.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

// The command center is an operator palette over the same authorization-aware
// discovery used by the full search page. The normal GET form remains the
// fallback: without JavaScript, or with no palette choice active, Enter searches
// every record scope as usual.
document.querySelectorAll("[data-command-center-form]").forEach((form) => {
  const dialog = form.closest("dialog");
  const input = form.querySelector("[data-command-center-input]");
  let results = form.querySelector("[data-command-center-results]");
  let options = [];
  let activeIndex = -1;
  let requestController;
  let debounceTimer;

  const setActive = (index, { scroll = true } = {}) => {
    if (!options.length) index = -1;
    if (index >= options.length) index = 0;
    if (index < -1) index = options.length - 1;
    activeIndex = index;
    options.forEach((option, optionIndex) => {
      const active = optionIndex === activeIndex;
      option.classList.toggle("is-active", active);
      option.setAttribute("aria-selected", active ? "true" : "false");
    });
    const active = options[activeIndex];
    if (active) {
      input.setAttribute("aria-activedescendant", active.id);
      if (scroll) active.scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
    form.classList.toggle("has-active-option", Boolean(active));
  };

  const bindResults = () => {
    options = [...results.querySelectorAll("[data-command-center-option]")];
    setActive(-1, { scroll: false });
    options.forEach((option, index) => {
      // Movement, not merely appearing under a stationary pointer. Results are
      // replaced while the operator types, so `pointerenter` would promote
      // whatever new row lands under the cursor and make Enter navigate when
      // the operator intended the full search fallback.
      option.addEventListener("pointermove", () => setActive(index, { scroll: false }));
      option.addEventListener("focus", () => setActive(index, { scroll: false }));
    });
  };

  const load = async () => {
    requestController?.abort();
    requestController = new AbortController();
    const url = new URL(form.action, window.location.href);
    if (input.value.trim()) url.searchParams.set("q", input.value.trim());
    form.classList.add("is-loading");
    try {
      const response = await hqFetch(url, {
        credentials: "same-origin",
        headers: { "X-Command-Center": "palette" },
        signal: requestController.signal,
      });
      const next = hqParseDocument(await response.text()).querySelector(
        "[data-command-center-results]",
      );
      if (!next) {
        // Not a results fragment: almost always the sign-in page after the
        // session ended underneath an open tab. Say so rather than sit on
        // "Loading".
        results.replaceChildren();
        const message = document.createElement("p");
        message.className = "notice notice-attention";
        const signIn =
          response.redirected &&
          new URL(response.url).pathname.startsWith("/accounts/login/");
        if (signIn) {
          message.textContent = "Your session has ended. ";
          const link = document.createElement("a");
          link.href = response.url;
          link.textContent = "Sign in again";
          message.append(link);
        } else {
          message.textContent = "Could not load results. Press Enter to search.";
        }
        results.append(message);
        return;
      }
      results.replaceWith(next);
      results = next;
      bindResults();
    } catch (error) {
      if (error.name !== "AbortError") {
        results.replaceChildren();
        const message = document.createElement("p");
        message.className = "notice notice-attention";
        message.textContent = "Could not load results. Press Enter to search.";
        results.append(message);
      }
    } finally {
      form.classList.remove("is-loading");
    }
  };

  const open = () => {
    if (typeof dialog?.showModal !== "function") return false;
    if (!dialog.open) dialog.showModal();
    input.setAttribute("aria-expanded", "true");
    input.focus();
    input.select();
    load();
    return true;
  };

  // The header search is the visible doorway into this same search surface.
  // It stays a normal GET form for progressive enhancement; with JS, clicking
  // it opens the richer palette and carries over any query already present.
  document.querySelectorAll("[data-command-center-open]").forEach((opener) => {
    opener.addEventListener("click", (event) => {
      const openerInput = opener.querySelector("input[name=q]");
      if (openerInput?.value.trim()) input.value = openerInput.value;
      if (!open()) return;
      event.preventDefault();
    });
  });

  document.addEventListener("keydown", (event) => {
    if (!(event.key?.toLowerCase() === "k" && (event.metaKey || event.ctrlKey))) return;
    if (!open()) return;
    event.preventDefault();
  });
  dialog?.addEventListener("close", () => {
    input.setAttribute("aria-expanded", "false");
    requestController?.abort();
    clearTimeout(debounceTimer);
    setActive(-1, { scroll: false });
  });
  input.addEventListener("input", () => {
    setActive(-1, { scroll: false });
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(load, 110);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActive(activeIndex + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Escape") {
      event.preventDefault();
      dialog?.close();
    }
  });
  form.addEventListener("submit", (event) => {
    const active = options[activeIndex];
    if (!active) return;
    event.preventDefault();
    window.location.assign(active.href);
  });
});

// One muted line in a connection-style row list: the failure row every loader
// here shows.
const hqNoteRow = (text) => {
  const row = document.createElement("div");
  row.className = "conn-row";
  const note = document.createElement("span");
  note.className = "conn-row-note";
  note.textContent = text;
  row.append(note);
  return row;
};

// A panel whose content comes from somewhere slow is fetched after the page, so
// the page never waits on it; one inside a closed disclosure is fetched when
// the disclosure opens, so the page never carries what nobody opened. An empty
// answer leaves nothing behind. A failed one shows the slot's
// `data-deferred-failure` line if it has one, and otherwise leaves whatever the
// slot held for a page without script.
const hqLoadDeferred = async (slot) => {
  if (slot.dataset.deferredLoading) return;
  slot.dataset.deferredLoading = "true";
  const failure = slot.dataset.deferredFailure;
  try {
    const response = await hqFetch(slot.dataset.deferred, {
      credentials: "same-origin",
      renewSession: false,
    });
    if (!response.ok) {
      if (failure) slot.replaceChildren(hqNoteRow(failure));
      return;
    }
    const panel = hqParseDocument(await response.text()).body;
    slot.replaceWith(...panel.childNodes);
  } catch (_error) {
    if (failure) slot.replaceChildren(hqNoteRow(failure));
    else if (!slot.childElementCount) slot.remove();
  }
};
document.querySelectorAll("[data-deferred]").forEach((slot) => {
  if (!slot.closest("details:not([open])")) hqLoadDeferred(slot);
});
document.addEventListener(
  "toggle",
  (event) => {
    const disclosure = event.target;
    if (!(disclosure instanceof HTMLDetailsElement) || !disclosure.open) return;
    disclosure.querySelectorAll("[data-deferred]").forEach((slot) => {
      if (slot.closest("details:not([open])") === null) hqLoadDeferred(slot);
    });
  },
  true,
);

// At-a-glance readings are refreshed on request. The button posts a refresh of
// every panel; opening the page on a due reading posts one for the due
// panels only. Either way the current reading stays up, marked as refreshing,
// and this follows the controller's answer in place until it lands, the page
// is hidden, or a few minutes pass. Reading the glance never requests anything.
const GLANCE_POLL_MS = 3000;
const GLANCE_POLL_LIMIT_MS = 180_000;
const hqBindDashboardGlance = (root) => {
  const form = root.querySelector("[data-dashboard-glance-refresh]");
  if (!form || form.dataset.bound === "true") return;
  form.dataset.bound = "true";

  const replace = async (current, response) => {
    const next = hqParseDocument(await response.text()).querySelector(
      "[data-dashboard-glance]",
    );
    if (!next) return current;
    const currentPanels = current.querySelector(".glance-panels");
    const nextPanels = next.querySelector(".glance-panels");
    if (!currentPanels || !nextPanels) return current;
    // Only a panel that changed is swapped, so a poll that finds nothing new
    // leaves the page exactly as it was, and an open popover stays open.
    const shown = new Map(
      [...currentPanels.querySelectorAll("[data-panel]")].map((panel) => [panel.dataset.panel, panel]),
    );
    const order = (panels) => [...panels.querySelectorAll("[data-panel]")].map((panel) => panel.dataset.panel).join();
    const markup = (panel) => {
      const copy = panel.cloneNode(true);
      copy.removeAttribute("open");
      return copy.outerHTML;
    };
    nextPanels.querySelectorAll("[data-panel]").forEach((panel) => {
      const was = shown.get(panel.dataset.panel);
      if (was) panel.open = was.open;
    });
    if (order(currentPanels) !== order(nextPanels)) {
      currentPanels.replaceWith(nextPanels);
      return current;
    }
    nextPanels.querySelectorAll("[data-panel]").forEach((panel) => {
      const was = shown.get(panel.dataset.panel);
      if (markup(was) !== markup(panel)) was.replaceWith(panel);
    });
    return current;
  };

  // Only a refresh someone asked for spins the button; the one a due reading
  // starts on load shows on the panel's own dot.
  const follow = async (firstResponse, { asked = false } = {}) => {
    let current = root;
    if (asked) current.classList.add("is-loading");
    current.setAttribute("aria-busy", "true");
    try {
      current = await replace(current, await firstResponse());
      const until = Date.now() + GLANCE_POLL_LIMIT_MS;
      while (
        current.querySelector("[data-refreshing]") &&
        Date.now() < until &&
        document.visibilityState === "visible"
      ) {
        await new Promise((resolve) => window.setTimeout(resolve, GLANCE_POLL_MS));
        current = await replace(
          current,
          // A tick is not a request to leave the page.
          await hqFetch(current.dataset.source, {
            credentials: "same-origin",
            renewSession: false,
          }),
        );
      }
    } catch (_error) {
      const status = current.querySelector("[data-dashboard-glance-status]");
      if (status) status.textContent = "At-a-glance refresh failed.";
    } finally {
      current.classList.remove("is-loading");
      current.removeAttribute("aria-busy");
    }
  };

  const post = (fields, options) => {
    const body = new FormData(form);
    Object.entries(fields).forEach(([name, value]) => body.set(name, value));
    return hqFetch(form.action, { method: "POST", body, credentials: "same-origin", ...options });
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    follow(() => post({}), { asked: true });
  });

  if (root.querySelector(".glance-panel.is-due")) {
    // Opening the page is not a request to stay signed in.
    follow(() => post({ scope: "stale" }, { renewSession: false }));
  }
};

document.querySelectorAll("[data-dashboard-glance]").forEach(hqBindDashboardGlance);

// Dropzones. The file input already is the click and drop target, so this adds
// only what the browser will not: the drag highlight, and telling the operator
// which files are staged before they commit to importing them.
document.querySelectorAll("[data-dropzone]").forEach((zone) => {
  const input = zone.querySelector("input[type=file]");
  const chosen = zone.querySelector("[data-dropzone-files]");
  if (!input) return;

  const highlight = (on) => zone.classList.toggle("is-dragging", on);
  ["dragenter", "dragover"].forEach((type) =>
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      highlight(true);
    }),
  );
  zone.addEventListener("dragleave", (event) => {
    // Crossing between the zone and its own children fires dragleave; only a
    // pointer that has actually left should drop the highlight.
    if (!zone.contains(event.relatedTarget)) highlight(false);
  });
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    highlight(false);
    input.files = event.dataTransfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });

  if (!chosen) return;
  input.addEventListener("change", () => {
    const files = Array.from(input.files || []);
    chosen.replaceChildren(
      ...files.map((file) => {
        const row = document.createElement("li");
        row.textContent = `${file.name} · ${Math.max(1, Math.round(file.size / 1024))} KB`;
        return row;
      }),
    );
    chosen.hidden = files.length === 0;
  });
});

// Tooltips, for chart marks and anything else carrying data-tip. The SVG
// <title> element is the browser's own tooltip: it waits a second or two,
// cannot be styled, and does not follow the pointer, long enough that reading
// a chart stops feeling like reading. This shows the same text immediately,
// tracking the cursor, and beside an element that takes keyboard focus.
(() => {
  // Pointer devices only, for the same reason the nav menus are. A touch drag
  // across a chart fires pointermove but never pointerleave, so a tooltip
  // raised by a scroll gesture would have nothing to dismiss it.
  // Touch already has the chart's data table, which is always rendered.
  if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;

  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  document.body.append(tip);

  const place = ({ clientX, clientY }) => {
    // Measured after the text is set, so a tooltip near an edge flips to the
    // side with room instead of being clipped by the viewport.
    const { width, height } = tip.getBoundingClientRect();
    const gap = 14;
    const left = Math.min(
      Math.max(gap, clientX + gap),
      window.innerWidth - width - gap,
    );
    const above = clientY - height - gap;
    tip.style.left = `${left}px`;
    tip.style.top = `${above < gap ? clientY + gap : above}px`;
  };

  // What only this browser knows. A page is served over TLS or not at all, so
  // on the very name a certificate is served for, this page loading in a
  // secure context is the browser having trusted that name's certificate.
  const textOf = (mark) => {
    const host = mark.dataset.tipHost;
    const trusted = host && window.isSecureContext && location.hostname === host;
    if (!trusted) return mark.dataset.tip;
    const [verdict, ...rest] = mark.dataset.tip.split("\n");
    return [`${verdict} · trusted by this browser`, ...rest].join("\n");
  };

  const show = (mark, at) => {
    const text = textOf(mark);
    if (tip.textContent !== text) tip.textContent = text;
    tip.hidden = false;
    place(at);
  };

  // Delegated from the document rather than bound per chart: a calendar that
  // pages to another month is replaced in place, and per-element listeners
  // would have gone with the node it swapped out.
  document.addEventListener("pointermove", (event) => {
    const mark = event.target.closest("[data-tip]");
    if (!mark) {
      if (!tip.hidden && !document.activeElement?.matches("[data-tip]")) tip.hidden = true;
      return;
    }
    show(mark, event);
  });
  document.addEventListener("pointerleave", () => {
    tip.hidden = true;
  });
  document.addEventListener("focusin", (event) => {
    const mark = event.target.closest("[data-tip]");
    if (!mark) return;
    const box = mark.getBoundingClientRect();
    show(mark, { clientX: box.left, clientY: box.top });
  });
  document.addEventListener("focusout", () => {
    tip.hidden = true;
  });
})();

// Asked-for work: one behaviour for every "do this now".
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
// while something is live. A persistent connection would be a second transport
// to run and secure for a question that fits in a query.
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
  const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

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
  window.setInterval(() => document.querySelectorAll("[data-ask][data-ask-since]").forEach(tick), 1000);

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
  const refresh = async (selector) => {
    const response = await hqFetch(window.location.href, { credentials: "same-origin", renewSession: false });
    if (!response.ok) throw new Error("no page");
    const next = hqParseDocument(await response.text());
    const fresh = [...next.querySelectorAll(selector)];
    const shown = [...document.querySelectorAll(selector)];
    if (!fresh.length || fresh.length !== shown.length) throw new Error("no region");
    shown.forEach((region, index) => region.replaceWith(fresh[index]));
    resume(document);
  };

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

  const follow = async (url) => {
    const until = Date.now() + FOLLOW_LIMIT_MS;
    const controls = () => following.get(url) || new Set();
    try {
      while (Date.now() < until) {
        await wait(showing() ? POLL_MS : HIDDEN_POLL_MS);
        let standing = null;
        try {
          const response = await hqFetch(url, { credentials: "same-origin", renewSession: false });
          // Gone or refused is an end; anything else is a container
          // restarting under us, which will answer again.
          if (response.status === 404 || response.status === 403) break;
          if (response.ok) standing = await response.json();
        } catch (_error) {
          standing = null;
        }
        if (!standing) continue;
        if (standing.live) {
          controls().forEach((control) => draw(control, standing));
          continue;
        }
        const waiting = [...controls()];
        following.delete(url);
        // One control lands it, so one fetch refreshes the page; the rest
        // only say so.
        waiting.slice(1).forEach((control) => draw(control, standing));
        if (waiting.length) await land(waiting[0], standing);
        return;
      }
      controls().forEach((control) => draw(control, { state: "idle", live: false, note: "" }));
    } finally {
      following.delete(url);
    }
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
})();

// Calendar paging without a page load. The links and forms work on their own:
// this only replaces the calendar in place so the rest of the page, the scroll
// position and the keyboard focus stay where they were. A card swaps itself;
// the calendar page swaps its whole region, so the month, the day and the
// list of calendars always agree.
(() => {
  const region = (element) => element.closest("[data-calendar], .calendar-card");

  const swap = async (element, url, init = {}) => {
    const current = region(element);
    if (!current || !current.id) return false;
    // X-Fragment asks the view for the card alone, without building the rest
    // of the page.
    const response = await hqFetch(url, {
      credentials: "same-origin",
      headers: { "X-Fragment": "calendar" },
      ...init,
    });
    if (!response.ok) return false;
    const parsed = hqParseDocument(await response.text());
    const next = parsed.getElementById(current.id);
    if (!next) return false;
    const focused = document.activeElement;
    const key = focused && current.contains(focused)
      ? focused.getAttribute("href") || focused.closest("form")?.getAttribute("action")
      : null;
    current.replaceWith(next);
    // replaceState, not pushState: paging months is not a place worth putting
    // between the operator and the Back button.
    history.replaceState(null, "", response.redirected ? response.url : url);
    if (key) {
      next.querySelector(`[href="${CSS.escape(key)}"], form[action="${CSS.escape(key)}"] button`)?.focus();
    }
    return true;
  };

  const fallback = (url) => {
    window.location.href = url;
  };

  document.addEventListener("click", (event) => {
    const link = event.target.closest(".calendar-nav a, [data-calendar] a[href]");
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey) return;
    // Within the calendar, only what opens the calendar again opens in place:
    // a record's own page is a page.
    if (link.closest("[data-calendar]") && new URL(link.href).pathname !== window.location.pathname) return;
    event.preventDefault();
    // Any failure falls through to the ordinary navigation the link already is.
    swap(link, link.href)
      .then((done) => {
        if (!done) fallback(link.href);
      })
      .catch(() => fallback(link.href));
  });

  // Checking a calendar: the form posts, the server redirects back to the
  // month, and that page is what replaces this one.
  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-calendar] form[data-calendar-source]");
    if (!form) return;
    event.preventDefault();
    swap(form, form.action, { method: "POST", body: new FormData(form) })
      .then((done) => {
        if (!done) form.submit();
      })
      .catch(() => form.submit());
  });
})();

// A field shown only when another field's value calls for it:
// data-when="repeat" for any value, data-when="repeat=weekly|daily" for those.
// Without this script every field shows, which is still a working form.
const hqBindWhen = (root) => {
  root.querySelectorAll("[data-when]").forEach((field) => {
    const [name, wanted] = field.dataset.when.split("=");
    const control = field.closest("form")?.elements.namedItem(name);
    if (!control || field.dataset.whenBound) return;
    field.dataset.whenBound = "true";
    const update = () => {
      field.hidden = wanted === undefined ? !control.value : !wanted.split("|").includes(control.value);
    };
    control.addEventListener("change", update);
    update();
  });
};
hqBindWhen(document);

// A key that works a control: any link or button carrying data-hotkey, while
// nothing is being typed. The control stays the source of truth, so a key
// does exactly what clicking it would.
document.addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey || event.defaultPrevented) return;
  if (event.target.closest("input, textarea, select, [contenteditable], dialog[open]")) return;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  // A control may answer several keys: data-hotkey="ArrowRight j n".
  const control = document.querySelector(`[data-hotkey~="${CSS.escape(key)}"]`);
  if (!control) return;
  event.preventDefault();
  control.click();
});

// A list field's own controls. The rows are real inputs whether or not this
// runs (one spare row is always rendered) so this only adds the
// convenience of more rows and of dropping one without clearing it by hand.
document.addEventListener("click", (event) => {
  const add = event.target.closest("[data-name-list-add]");
  if (add) {
    const list = add.closest("[data-name-list]");
    const rows = list.querySelectorAll(".name-list-row");
    const last = rows[rows.length - 1];
    const row = last.cloneNode(true);
    row.querySelector("input").value = "";
    last.after(row);
    row.querySelector("input").focus();
    return;
  }
  const remove = event.target.closest("[data-name-list-remove]");
  if (!remove) return;
  const list = remove.closest("[data-name-list]");
  const row = remove.closest(".name-list-row");
  // Never leave nothing to type in: the last row empties rather than going.
  if (list.querySelectorAll(".name-list-row").length > 1) row.remove();
  else row.querySelector("input").value = "";
});


// A form that is about to do something says so on the button that does it.
// The consequence is declared by the provider and rendered onto the field, so
// this reads it rather than knowing which fields matter.
document.querySelectorAll("form.form").forEach((form) => {
  const effects = form.querySelectorAll("[data-change-effect]");
  if (!effects.length) return;
  const submit = form.querySelector('button[type="submit"], .form-actions button');
  if (!submit) return;
  const original = submit.textContent.trim();
  const initial = new Map();
  const inputs = () =>
    [...effects].flatMap((field) => [...field.querySelectorAll("input, textarea, select")]);
  inputs().forEach((input, index) => initial.set(input, input.value));

  const review = () => {
    // Rows can be added and removed, so a changed *count* is a change even
    // when every surviving row still holds what it held.
    const current = inputs();
    const changed =
      current.length !== initial.size ||
      current.some((input) => !initial.has(input) || initial.get(input) !== input.value);
    submit.textContent = changed ? "Save and apply" : original;
    form.classList.toggle("form-will-act", changed);
  };
  form.addEventListener("input", review);
  form.addEventListener("click", () => setTimeout(review, 0));
});

// A form that saves where it stands. The markup submits normally without this
// file, so the feature is the reload it removes rather than the saving itself:
// a preference panel that navigates the whole page to record two checkboxes
// throws away the scroll position and everything else on screen.
//
// One selector, so the next such form is handled by construction. What it
// replaces on success is named by the form, not assumed, because "the thing
// this edits" is not derivable from the form itself.
document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-live-form]");
  if (!form || !window.fetch) return;
  event.preventDefault();
  const status = form.querySelector("[data-live-status]");
  const buttons = form.querySelectorAll("button");
  buttons.forEach((button) => (button.disabled = true));
  if (status) status.textContent = "Saving…";
  hqFetch(form.action, {
    method: "POST",
    body: new FormData(form),
    credentials: "same-origin",
  })
    .then((response) => {
      if (!response.ok) throw new Error(String(response.status));
      return response.text();
    })
    .then((html) => {
      // The server answers with the panel it just changed, so the page shows
      // what was stored rather than what the browser believes was stored.
      const parsed = hqParseDocument(html);
      const fresh = parsed.querySelector(".ext-links");
      const current = document.querySelector(".ext-links");
      if (fresh && current) current.replaceWith(fresh);
      form.closest("details[data-menu]")?.removeAttribute("open");
      form.closest("dialog")?.close();
      if (status) status.textContent = "";
    })
    .catch(() => {
      // Saying so and leaving the panel open, rather than closing over a
      // change that did not happen.
      if (status) status.textContent = "Could not save.";
    })
    .finally(() => buttons.forEach((button) => (button.disabled = false)));
});

// Cancel restores what was stored and closes, without asking the server for a
// page it already has.
document.addEventListener("click", (event) => {
  const cancel = event.target.closest("[data-live-cancel]");
  if (!cancel) return;
  const form = cancel.closest("form");
  if (form) form.reset();
  const dialog = cancel.closest("dialog");
  // In a dialog, closing is the whole of cancelling; the link is for the page.
  if (dialog) {
    event.preventDefault();
    dialog.close();
  }
  cancel.closest("details[data-menu]")?.removeAttribute("open");
});

// Reachability, answered in place. The form is a real GET to a page that
// renders the same answer, so it works without this; what this adds is not
// losing the dialog, the machine behind it and the scroll position on every
// further question.
document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-whatif]");
  if (!form || !window.fetch) return;
  const slot = form.parentElement.querySelector("[data-whatif-result]");
  if (!slot) return;
  event.preventDefault();
  const query = new URLSearchParams(new FormData(form)).toString();
  slot.setAttribute("aria-busy", "true");
  hqFetch(`${form.action}?${query}`, {
    credentials: "same-origin",
  })
    .then((response) => (response.ok ? response.text() : Promise.reject(response)))
    .then((html) => {
      const parsed = hqParseDocument(html);
      const fresh = parsed.querySelector("[data-whatif-result]");
      if (fresh) slot.replaceWith(fresh);
    })
    .catch(() => {
      // The answer is a page away either way, so a failure here submits for
      // real rather than leaving the question looking unanswered.
      form.removeAttribute("data-whatif");
      form.submit();
    });
});

// The connection panel, fetched the first time it is opened. It reads the
// tailnet inventory and evaluates the access policy, and it sits behind a
// control on every page, so paying for it on every page render would be
// paying for it almost always to go unread.
document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-connection-source]");
  if (!opener || !window.fetch) return;
  const dialog = document.getElementById("modal-connection");
  const slot = dialog?.querySelector("[data-connection-slot]");
  if (!slot) return;
  hqFetch(opener.dataset.connectionSource, {
    credentials: "same-origin",
  })
    .then((response) => (response.ok ? response.text() : Promise.reject(response)))
    .then((html) => {
      const panel = hqParseDocument(html).querySelector(
        "[data-connection-panel]",
      );
      if (!panel) return;
      slot.replaceWith(panel);
      hqWatchRoundTrip(panel);
      hqShowResponseHeaders(panel);
    })
    .catch(() => {
      // The same answer is a page away, and a dialog stuck on "reading" is
      // worse than a navigation.
      window.location.assign(opener.dataset.connectionSource);
    });
});

// Round trip, actually measured rather than reported. Everything else in that
// panel is read from the last sweep and says so; this one is taken now, from
// the browser reading it, which is the only place the number means anything.
// It keeps sampling while the panel is open, because a peering is a live thing
// and a single figure printed once reads like a stored one.
const hqRoundTrip = (() => {
  let timer = null;

  const sample = (endpoint) => {
    const started = performance.now();
    // A response with no body and no database behind it, so what is measured
    // is the path rather than what HQ did after arriving.
    return hqFetch(`${endpoint}?t=${started}`, {
      cache: "no-store",
      credentials: "same-origin",
    }).then(() => performance.now() - started);
  };

  const render = (slot, runs) => {
    const best = Math.min(...runs);
    const worst = Math.max(...runs);
    let live = slot.querySelector(".conn-live");
    if (!live) {
      live = document.createElement("span");
      live.className = "conn-live";
      const pulse = document.createElement("span");
      pulse.className = "conn-pulse";
      const value = document.createElement("strong");
      const samples = document.createElement("span");
      samples.className = "conn-samples";
      live.append(pulse, value, samples);
      slot.replaceChildren(live);
    }
    live.querySelector("strong").textContent = `${best.toFixed(1)} ms`;
    const samples = live.querySelector(".conn-samples");
    // Each bar relative to the slowest sample, so the shape shows variation
    // rather than an absolute scale nobody can read at this size.
    samples.replaceChildren(
      ...runs.map((run) => {
        const bar = document.createElement("i");
        bar.style.setProperty("--at", `${Math.max(12, (run / worst) * 100)}`);
        return bar;
      }),
    );
  };

  // The figures describe the recent path, not the whole visit.
  const WINDOW = 12;

  const start = (slot, endpoint) => {
    const runs = [];
    const tick = () => {
      // A hidden tab has nobody reading the figure.
      if (document.visibilityState !== "visible") return Promise.resolve();
      return sample(endpoint)
        .then((run) => {
          runs.push(run);
          if (runs.length > WINDOW) runs.splice(0, runs.length - WINDOW);
          render(slot, runs);
        })
        .catch(() => {
          slot.textContent = "could not measure";
          stop();
        });
    };
    tick();
    timer = window.setInterval(tick, 3000);
  };

  const stop = () => {
    if (timer !== null) window.clearInterval(timer);
    timer = null;
  };

  return { start, stop };
})();

// Measured as soon as the panel exists, and stopped when it goes away. Nothing
// is polling while the dialog is shut.
const hqWatchRoundTrip = (root) => {
  const slot = root.querySelector("[data-rtt]");
  const endpoint = root.querySelector("[data-rtt-measure]")?.dataset.rttMeasure;
  if (slot && endpoint) hqRoundTrip.start(slot, endpoint);
};

document.addEventListener("DOMContentLoaded", () => {
  const panel = document.querySelector("[data-connection-panel]");
  if (panel && !panel.closest("dialog")) hqWatchRoundTrip(panel);
});

document.getElementById("modal-connection")?.addEventListener("close", () => {
  hqRoundTrip.stop();
});

// What HQ sent back, taken from a real response rather than from the settings
// meant to produce one. A same-origin fetch exposes every response header, so
// the browser reading the panel is the honest place to ask what it was given.
const HQ_RESPONSE_HEADERS = [
  ["x-served-by", "Which reverse-proxy hostname actually served this response"],
  ["x-request-id", "Joins this browser response to HQ's structured request log"],
  ["content-security-policy", "Which origins may load script, style and frames"],
  ["strict-transport-security", "Refuses plain HTTP for this host from now on"],
  ["x-content-type-options", "Stops the browser guessing a type it was not sent"],
  ["x-frame-options", "Refuses to be framed by another site"],
  ["referrer-policy", "How much of this URL travels to anywhere you click"],
  ["cross-origin-opener-policy", "Keeps other origins out of this browsing context"],
  ["cross-origin-resource-policy", "Refuses to be loaded as a subresource elsewhere"],
  ["permissions-policy", "Which device capabilities this page may ask for"],
  ["reporting-endpoints", "Where the browser sends a policy it refused to follow"],
];

// The directives worth calling out by name, because the policy is one long
// header and the interesting parts of it are the two that stop a class of bug
// rather than naming an origin. Read from the policy the browser was actually
// sent, so a directive dropped in configuration shows as absent here.
const HQ_POLICY_DIRECTIVES = [
  ["require-trusted-types-for", "Assigning a string to a DOM sink throws instead of parsing"],
  ["frame-ancestors 'none'", "Nothing may frame this page"],
  ["object-src 'none'", "No plugins or embedded objects"],
  ["base-uri 'self'", "Injected markup cannot re-point every relative URL"],
  ["form-action 'self'", "A form cannot be made to submit somewhere else"],
];

const hqShowResponseHeaders = (root) => {
  const slot = root.querySelector("[data-response-headers]");
  if (!slot || !window.fetch) return;
  const disclosure = slot.closest("[data-connection-protocol]");
  if (disclosure && !disclosure.open) return;
  if (slot.dataset.loaded === "true") return;
  slot.dataset.loaded = "true";
  const hqEvidenceRow = (label, present, purpose, chipText) => {
    const row = document.createElement("div");
    row.className = "conn-row conn-row-wide";
    const shown = document.createElement("code");
    shown.textContent = label;
    const chip = document.createElement("span");
    chip.className = `conn-kind ${present ? "conn-kind-read" : "conn-kind-elsewhere"}`;
    chip.textContent = chipText;
    const note = document.createElement("span");
    note.className = "conn-row-note";
    note.textContent = purpose;
    row.append(shown, chip, note);
    return row;
  };

  hqFetch(window.location.href, {
    credentials: "same-origin",
    cache: "no-store",
  })
    .then((response) => {
      const rows = HQ_RESPONSE_HEADERS.map(([name, purpose]) => {
        const value = response.headers.get(name);
        return hqEvidenceRow(
          value
            ? `${name}: ${value.length > 76 ? `${value.slice(0, 76)}…` : value}`
            : name,
          value,
          purpose,
          value ? "sent" : "absent",
        );
      });
      // The policy is one header long enough to be truncated above, and its
      // most consequential directives are the ones a reader would never spot
      // in it. Named individually, and read back from the same response, so
      // "the policy says so" is checkable rather than asserted.
      const policy = response.headers.get("content-security-policy") || "";
      for (const [directive, purpose] of HQ_POLICY_DIRECTIVES) {
        const present = policy.includes(directive);
        rows.push(
          hqEvidenceRow(directive, present, purpose, present ? "enforced" : "absent"),
        );
      }
      slot.replaceChildren(...rows);
    })
    .catch(() => {
      slot.replaceChildren(hqNoteRow("The response could not be read back."));
    });
};

// The address fragment loads through hqLoadDeferred when its disclosure opens
// and serves only what HQ holds. Its button posts the lookup and swaps the
// answer in place.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest?.("form[data-public-address-lookup]");
  const panel = form?.closest("[data-public-address]");
  if (!panel) return;
  event.preventDefault();
  form.querySelector("button")?.setAttribute("disabled", "");
  try {
    const response = await hqFetch(form.action, {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
    });
    const next = response.ok
      ? hqParseDocument(await response.text()).querySelector("[data-public-address]")
      : null;
    if (!next) throw new Error("no answer");
    panel.replaceWith(next);
  } catch (_error) {
    panel.replaceChildren(hqNoteRow("The lookup could not be reached."));
  }
});

// The compact admission rail is an index into the evidence below it. A normal
// link remains the no-script fallback; when enhanced, open the exact control
// inside this copy of the shared panel (page or dialog) before scrolling.
document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-connection-control]");
  if (!link) return;
  const panel = link.closest("[data-connection-panel]");
  const control = panel?.querySelector(
    `[data-connection-layer="${CSS.escape(link.dataset.connectionControl)}"]`,
  );
  if (!control) return;
  event.preventDefault();
  // A control sits inside its hop's disclosure: open the way down to it.
  for (let up = control; up && up !== panel; up = up.parentElement) {
    if (up.tagName === "DETAILS") up.open = true;
  }
  control.querySelector("summary")?.focus({ preventScroll: true });
  control.scrollIntoView({ block: "center" });
});

document.addEventListener("DOMContentLoaded", () => {
  const panel = document.querySelector("[data-connection-panel]");
  if (panel && !panel.closest("dialog")) hqShowResponseHeaders(panel);
});

document.addEventListener("toggle", (event) => {
  const disclosure = event.target;
  if (!disclosure.matches?.("[data-connection-protocol]") || !disclosure.open) return;
  const panel = disclosure.closest("[data-connection-panel]");
  if (panel) hqShowResponseHeaders(panel);
}, true);

// A pinned save bar offers to save only once there is something to save.
document.querySelectorAll("form:has(.save-bar)").forEach((form) => {
  form.dataset.clean = "";
});
document.addEventListener("input", (event) => {
  const form = event.target.form;
  if (form) delete form.dataset.clean;
}, true);

// The topology is useful HTML before this runs: every card's select control
// is a link that focuses the node, and a focused page draws its details. This
// enhancement filters, isolates a node's immediate neighbourhood, and loads
// the selected node's details into the one panel beside the lanes, creating
// no client-side topology state of its own.
document.querySelectorAll("[data-topology]").forEach((workspace) => {
  const nodes = [...workspace.querySelectorAll("[data-topology-node]")];
  const lanes = [...workspace.querySelectorAll("[data-topology-lane]")];
  const map = workspace.querySelector(".topology-map");
  const stage = workspace.querySelector("[data-topology-stage]");
  const detail = workspace.querySelector("[data-topology-detail]");
  const ledger = document.getElementById(workspace.dataset.topologyLedger);
  const edges = [...(ledger?.querySelectorAll("[data-topology-edge]") || [])];
  const search = workspace.querySelector("[data-topology-search]");
  const kindControls = [...workspace.querySelectorAll("[data-topology-kind]")];
  const status = workspace.querySelector("[data-topology-status]");
  const reset = workspace.querySelector("[data-topology-reset]");
  let focused = nodes.some((node) => node.dataset.topologyNode === workspace.dataset.focus)
    ? workspace.dataset.focus : "";
  let detailRequest = 0;

  const rememberFocus = (nodeId) => {
    const url = new URL(window.location.href);
    if (nodeId) url.searchParams.set("focus", nodeId);
    else {
      url.searchParams.delete("focus");
      url.searchParams.delete("direction");
      url.searchParams.delete("depth");
    }
    window.history.replaceState({}, "", url);
  };

  // Whether the toolbar alone would show this node, ignoring any focus. Asked
  // before rendering, so a focus the filter hides is dropped before painting.
  const passesToolbar = (node) => {
    const terms = (search?.value || "").trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const shownKinds = new Set(kindControls.filter((control) => control.checked).map((control) => control.value));
    return shownKinds.has(node.dataset.topologyNodeKind)
      && terms.every((term) => node.dataset.topologySearchText.toLocaleLowerCase().includes(term));
  };

  const showDetail = (shown) => {
    if (!detail) return;
    detail.hidden = !shown;
    stage?.classList.toggle("has-detail", shown);
  };

  // The selected node's body, from the same view a focused page renders.
  // Only the newest request lands, so quick selections never interleave.
  const loadDetail = async (node) => {
    if (!detail) return;
    if (!node) {
      detail.dataset.topologyDetail = "";
      detail.replaceChildren();
      showDetail(false);
      return;
    }
    if (detail.dataset.topologyDetail === node.dataset.topologyNode && detail.childElementCount) {
      showDetail(true);
      return;
    }
    const request = ++detailRequest;
    detail.dataset.topologyDetail = node.dataset.topologyNode;
    detail.setAttribute("aria-busy", "true");
    showDetail(true);
    try {
      const response = await hqFetch(node.dataset.topologyBody, {
        credentials: "same-origin",
        renewSession: false,
      });
      if (request !== detailRequest) return;
      if (!response.ok) throw new Error(String(response.status));
      const body = hqParseDocument(await response.text()).body;
      if (request !== detailRequest) return;
      detail.replaceChildren(...body.childNodes);
    } catch (_error) {
      if (request !== detailRequest) return;
      const fallback = document.createElement("p");
      const link = document.createElement("a");
      link.href = node.querySelector("[data-topology-select]")?.href || "#map";
      link.textContent = "Open details";
      fallback.className = "muted";
      fallback.append("Details did not load. ", link);
      detail.replaceChildren(fallback);
    } finally {
      if (request === detailRequest) detail.removeAttribute("aria-busy");
    }
  };

  const render = () => {
    const selected = nodes.find((node) => node.dataset.topologyNode === focused);
    const related = new Set(selected?.dataset.topologyNeighbors.split(" ").filter(Boolean) || []);
    workspace.classList.toggle("has-focus", Boolean(selected));
    nodes.forEach((node) => {
      const id = node.dataset.topologyNode;
      const matchesFilter = passesToolbar(node);
      const inNeighborhood = !selected || id === focused || related.has(id);
      node.hidden = !matchesFilter || !inNeighborhood;
      node.classList.toggle("is-selected", id === focused);
      node.classList.toggle("is-related", related.has(id));
      const select = node.querySelector("[data-topology-select]");
      if (id === focused) select?.setAttribute("aria-current", "true");
      else select?.removeAttribute("aria-current");
    });
    lanes.forEach((lane) => {
      const visible = [...lane.querySelectorAll("[data-topology-node]")]
        .filter((node) => !node.hidden);
      lane.hidden = visible.length === 0;
      const count = lane.querySelector("[data-topology-count]");
      if (count) {
        count.textContent = visible.length;
        const label = `${visible.length} shown`;
        count.title = label;
        count.setAttribute("aria-label", label);
      }
    });
    edges.forEach((edge) => {
      const ends = edge.dataset.topologyEdge.split(" ");
      const touchesFocus = Boolean(selected) && ends.includes(focused);
      edge.hidden = Boolean(selected) && !touchesFocus;
      edge.classList.toggle("is-related", touchesFocus);
    });
    if (ledger) ledger.hidden = edges.length > 0 && edges.every((edge) => edge.hidden);
    if (status) {
      const visible = nodes.filter((node) => !node.hidden).length;
      status.textContent = selected
        ? `${selected.querySelector("strong")?.textContent || "Selected"}: ${related.size} direct relationship${related.size === 1 ? "" : "s"}.`
        : `${visible} of ${nodes.length} nodes shown. Select one to isolate its immediate relationships.`;
    }
  };

  // Isolating a node shrinks the lanes under whatever the map was scrolled
  // to, which leaves the first cards under the sticky headers. Start from
  // the top, then bring the selection into view below them.
  const reveal = () => {
    const selected = nodes.find((node) => node.dataset.topologyNode === focused);
    if (!map || !selected) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    map.scrollTo({ top: 0, behavior: "auto" });
    selected.scrollIntoView({
      behavior: reduce ? "auto" : "smooth",
      block: "nearest",
      inline: "center",
    });
  };

  const focus = (nodeId, remember = true) => {
    focused = nodes.some((node) => node.dataset.topologyNode === nodeId) ? nodeId : "";
    render();
    if (remember) rememberFocus(focused);
    loadDetail(nodes.find((node) => node.dataset.topologyNode === focused));
    if (focused && remember) reveal();
  };

  const filter = () => {
    // Decide, then draw, once. A focus the toolbar has just excluded is
    // dropped before anything is painted.
    const selected = nodes.find((node) => node.dataset.topologyNode === focused);
    if (selected && !passesToolbar(selected)) {
      focused = "";
      rememberFocus("");
      loadDetail(null);
    }
    render();
  };

  // A card selects its node: the select link, or a click anywhere on the
  // card that is not another link. Selecting the selected node clears it.
  // Middle and modified clicks on a link are left to the browser.
  workspace.addEventListener("click", (event) => {
    const node = event.target.closest?.("[data-topology-node]");
    if (!node) return;
    const link = event.target.closest("a[href]");
    if (link && !link.matches("[data-topology-select]")) return;
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const id = node.dataset.topologyNode;
    focus(focused === id ? "" : id);
  });

  search?.addEventListener("input", filter);
  kindControls.forEach((control) => control.addEventListener("change", filter));
  reset?.addEventListener("click", () => focus(""));
  search?.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      search.value = "";
      filter();
    } else if (event.key === "Enter") {
      const first = nodes.find((node) => !node.hidden);
      if (first) {
        focus(first.dataset.topologyNode);
        first.querySelector("[data-topology-select]")?.focus();
      }
    }
  });
  render();
  if (focused) {
    showDetail(Boolean(detail?.childElementCount));
    map?.scrollTo({ top: 0, behavior: "auto" });
  }
});

// A block meant to be pasted somewhere else: its button copies it. Shown only
// where the browser allows writing the clipboard; the block itself is plain
// selectable text either way.
document.addEventListener("DOMContentLoaded", () => {
  if (!navigator.clipboard) return;
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    const source = document.getElementById(button.dataset.copyTarget);
    if (!source) return;
    button.hidden = false;
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(source.textContent);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Select it to copy";
      }
    });
  });
});
