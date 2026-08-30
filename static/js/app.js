"use strict";

// The one place in HQ where a string becomes markup.
//
// Progressive enhancement here is server-rendered HTML: a fragment is fetched
// and swapped in, so parsing a response is unavoidable -- and
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
const hqFetch = async (input, options = {}) => {
  const headers = new Headers(options.headers);
  headers.set("X-Requested-With", "XMLHttpRequest");
  const response = await window.fetch(input, { ...options, headers });
  const refreshUrl =
    response.status === 403 ? response.headers.get("refresh_url") : "";
  if (refreshUrl) {
    window.location.assign(refreshUrl);
    return new Promise(() => {});
  }
  return response;
};
window.hqFetch = hqFetch;

// Disclosure menus that dismiss on an outside click or Escape. One selector
// covers every such menu, so adding another is handled by construction rather
// than by remembering to extend a hardcoded query. That query was extended
// twice, once per menu somebody added and then found stayed open over the top
// of the next one -- so a menu now says for itself that it dismisses, and the
// third case fixed itself before anyone noticed it.
const DISMISSIBLE_MENUS = "details[data-menu]";
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
  // The clicked menu is left alone -- the browser handles its own summary
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
document.querySelectorAll("details[data-action-count-url]").forEach((menu) => {
  menu.addEventListener("toggle", async () => {
    const badge = menu.querySelector("[data-action-count]");
    if (!menu.open || menu.dataset.actionCountLoaded || !badge) return;
    if (!badge.hidden) {
      menu.dataset.actionCountLoaded = "true";
      return;
    }
    menu.dataset.actionCountLoaded = "loading";
    try {
      const response = await hqFetch(menu.dataset.actionCountUrl, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`Action count returned ${response.status}`);
      const payload = await response.json();
      badge.textContent = String(payload.count);
      badge.hidden = false;
      menu.dataset.actionCountLoaded = "true";
    } catch (_error) {
      delete menu.dataset.actionCountLoaded;
    }
  });
});

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
// breakpoint -- the header's padding changes on narrow screens, and anything
// positioned against the stylesheet's static fallback sat a few pixels below
// it with a gap showing through. Once per load and per resize; nothing reads
// layout while scrolling.
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
    // section being read, and the local map should say so.
    if (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) {
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
// this only intercepts when there is in fact a dialog here to open -- otherwise
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
      // replaced while the operator types; `pointerenter` promoted whatever
      // new row happened to land under the cursor and made Enter navigate when
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
      if (!next) return;
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

// At-a-glance readings are intentionally cold until requested. One click
// rings the controller doorbell, then this performs a short bounded follow-up
// for the reported observation; there is no page-lifetime polling loop.
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
    currentPanels.replaceWith(nextPanels);
    return current;
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    let current = root;
    current.classList.add("is-loading");
    current.setAttribute("aria-busy", "true");
    try {
      current = await replace(
        current,
        await hqFetch(form.action, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
        }),
      );
      current.classList.add("is-loading");
      current.setAttribute("aria-busy", "true");
      for (
        let attempt = 0;
        attempt < 12 && current.querySelector("[data-refreshing]");
        attempt += 1
      ) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        current = await replace(
          current,
          await hqFetch(current.dataset.source, { credentials: "same-origin" }),
        );
        current.classList.add("is-loading");
        current.setAttribute("aria-busy", "true");
      }
    } catch (_error) {
      const status = current.querySelector("[data-dashboard-glance-status]");
      if (status) status.textContent = "At-a-glance refresh failed.";
    } finally {
      current.classList.remove("is-loading");
      current.removeAttribute("aria-busy");
    }
  });
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

// Chart tooltips. The SVG <title> element is the browser's own tooltip: it
// waits a second or two, cannot be styled, and does not follow the pointer --
// long enough that reading a chart stops feeling like reading. This shows the
// same text immediately, tracking the cursor.
(() => {
  // Pointer devices only, for the same reason the nav menus are. A touch drag
  // across a chart fires pointermove but never pointerleave, so a tooltip
  // raised by a scroll gesture stayed on screen with nothing to dismiss it.
  // Touch already has the chart's data table, which is always rendered.
  if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;

  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  document.body.append(tip);

  const place = (event) => {
    // Measured after the text is set, so a tooltip near an edge flips to the
    // side with room instead of being clipped by the viewport.
    const { width, height } = tip.getBoundingClientRect();
    const gap = 14;
    const left = Math.min(
      Math.max(gap, event.clientX + gap),
      window.innerWidth - width - gap,
    );
    const above = event.clientY - height - gap;
    tip.style.left = `${left}px`;
    tip.style.top = `${above < gap ? event.clientY + gap : above}px`;
  };

  // Delegated from the document rather than bound per chart: a calendar that
  // pages to another month is replaced in place, and per-element listeners
  // would have gone with the node it swapped out.
  document.addEventListener("pointermove", (event) => {
    const mark = event.target.closest("[data-chart] [data-tip]");
    if (!mark) {
      tip.hidden = true;
      return;
    }
    if (tip.textContent !== mark.dataset.tip) tip.textContent = mark.dataset.tip;
    tip.hidden = false;
    place(event);
  });
  document.addEventListener("pointerleave", () => {
    tip.hidden = true;
  });
})();

// Calendar paging without a page load. The links work on their own -- this
// only replaces the card in place so the rest of the page, and the scroll
// position, stay where they were.
(() => {
  const swap = async (link) => {
    const card = link.closest(".calendar-card");
    if (!card || !card.id) return false;
    // X-Fragment lets the view skip everything it would otherwise rebuild to
    // redraw one grid, and return the card on its own.
    const response = await hqFetch(link.href, {
      credentials: "same-origin",
      headers: { "X-Fragment": "calendar" },
    });
    if (!response.ok) return false;
    const parsed = hqParseDocument(await response.text());
    const next = parsed.getElementById(card.id);
    if (!next) return false;
    card.replaceWith(next);
    // replaceState, not pushState: paging months is not a place worth putting
    // between the operator and the Back button.
    history.replaceState(null, "", link.href);
    return true;
  };

  document.addEventListener("click", (event) => {
    const link = event.target.closest(".calendar-nav a");
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    // Any failure falls through to the ordinary navigation the link already is.
    swap(link)
      .then((done) => {
        if (!done) window.location.href = link.href;
      })
      .catch(() => {
        window.location.href = link.href;
      });
  });
})();

// Job progress. A job runs off the request thread, so the page that started
// it has to ask how it is going.
//
// Polling rather than a socket: one small question every couple of seconds,
// for a minute or two, a few times a week. A persistent connection would be a
// second transport to run and secure for a question that fits in a query.
(() => {
  const panel = document.querySelector(".job-progress[data-job]");
  if (!panel) return;

  const bar = panel.querySelector("[data-job-bar]");
  const note = panel.querySelector("[data-job-note]");
  const state = panel.querySelector("[data-job-state]");
  // Backs off when the tab is hidden: a phone left on this page overnight
  // should not spend the night asking.
  const interval = () => (document.hidden ? 15000 : 2000);
  let stop = false;

  const tick = async () => {
    if (stop) return;
    try {
      const response = await hqFetch(panel.dataset.job, {
        credentials: "same-origin",
      });
      if (response.ok) {
        const job = await response.json();
        state.textContent = job.label;
        if (job.note) note.textContent = job.note;
        if (job.percent === null) {
          panel.querySelector(".job-bar").classList.add("job-bar-unknown");
        } else {
          panel.querySelector(".job-bar").classList.remove("job-bar-unknown");
          bar.style.setProperty("--at", `${job.percent}%`);
        }
        panel.dataset.state = job.state;
        if (!job.live) {
          stop = true;
          // One reload, so the surface renders the outcome its own way. The
          // partial does not know what a finished job has to say.
          window.location.reload();
          return;
        }
      }
    } catch {
      // A failed poll is not a failed job. Keep asking: the usual cause is
      // the container being restarted under us, and it will answer again.
    }
    window.setTimeout(tick, interval());
  };
  window.setTimeout(tick, interval());
})();


// A list field's own controls. The rows are real inputs whether or not this
// runs -- one spare row is always rendered -- so this only adds the
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
      const menu = form.closest("details[data-menu]");
      if (menu) menu.removeAttribute("open");
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
  const menu = cancel.closest("details[data-menu]");
  if (menu) menu.removeAttribute("open");
});

// Reachability, answered in place. The form is a real GET to a page that
// renders the same answer, so it works without this; what this adds is not
// losing the dialog, the machine behind it and the scroll position every time
// somebody asks a second question.
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
// control on every page -- so paying for it on every page render would be
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
      ...runs.slice(-12).map((run) => {
        const bar = document.createElement("i");
        bar.style.setProperty("--at", `${Math.max(12, (run / worst) * 100)}`);
        return bar;
      }),
    );
  };

  const start = (slot, endpoint) => {
    const runs = [];
    const tick = () =>
      sample(endpoint)
        .then((run) => {
          runs.push(run);
          render(slot, runs);
        })
        .catch(() => {
          slot.textContent = "could not measure";
          stop();
        });
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
      const row = document.createElement("div");
      row.className = "conn-row";
      const note = document.createElement("span");
      note.className = "conn-row-note";
      note.textContent = "The response could not be read back.";
      row.append(note);
      slot.replaceChildren(row);
    });
};

// What the public internet says about the address this session is riding over,
// fetched when the disclosure is opened and never before. The same rule the
// rest of this page keeps: drawing the connection must not depend on asking
// anybody anything.
const hqShowPublicAddress = (disclosure) => {
  const slot = disclosure.querySelector("[data-peering-source]");
  if (!slot || slot.dataset.loaded || !window.fetch) return;
  slot.dataset.loaded = "true";
  hqFetch(slot.dataset.peeringSource, { credentials: "same-origin" })
    .then((response) => (response.ok ? response.text() : Promise.reject(response)))
    .then((html) => {
      const rows = hqParseDocument(html).body;
      slot.replaceChildren(...rows.childNodes);
    })
    .catch(() => {
      const row = document.createElement("div");
      row.className = "conn-row";
      const note = document.createElement("span");
      note.className = "conn-row-note";
      note.textContent = "The lookup could not be reached.";
      row.append(note);
      slot.replaceChildren(row);
    });
};

document.addEventListener(
  "toggle",
  (event) => {
    if (!event.target.matches?.("[data-peering-detail]") || !event.target.open) return;
    hqShowPublicAddress(event.target);
  },
  true,
);

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
  control.open = true;
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

// The topology is useful HTML before this runs: native disclosures expose
// detail and every action is a normal link or form. This enhancement makes the
// same projection explorable by filtering and isolating a node's immediate
// neighborhood, while creating no client-side topology state of its own.
document.querySelectorAll("[data-topology]").forEach((workspace) => {
  const nodes = [...workspace.querySelectorAll("[data-topology-node]")];
  const lanes = [...workspace.querySelectorAll("[data-topology-lane]")];
  const ledger = document.getElementById(workspace.dataset.topologyLedger);
  const edges = [...(ledger?.querySelectorAll("[data-topology-edge]") || [])];
  const search = workspace.querySelector("[data-topology-search]");
  const kindControls = [...workspace.querySelectorAll("[data-topology-kind]")];
  const status = workspace.querySelector("[data-topology-status]");
  const reset = workspace.querySelector("[data-topology-reset]");
  let focused = nodes.some((node) => node.dataset.topologyNode === workspace.dataset.focus)
    ? workspace.dataset.focus : "";

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

  // Whether the toolbar alone would show this node, ignoring any focus. Split
  // out so a caller can ask before rendering rather than reading it back off
  // the DOM afterwards: dropping a focus the filter just hid used to mean
  // rendering a state that existed only until the next line replaced it.
  const passesToolbar = (node) => {
    const terms = (search?.value || "").trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const shownKinds = new Set(kindControls.filter((control) => control.checked).map((control) => control.value));
    return shownKinds.has(node.dataset.topologyNodeKind)
      && terms.every((term) => node.dataset.topologySearchText.toLocaleLowerCase().includes(term));
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
    });
    lanes.forEach((lane) => {
      const visible = [...lane.querySelectorAll("[data-topology-node]")]
        .filter((node) => !node.hidden);
      lane.hidden = visible.length === 0;
      const count = lane.querySelector("[data-topology-count]");
      if (count) count.textContent = visible.length;
    });
    edges.forEach((edge) => {
      const ends = edge.dataset.topologyEdge.split(" ");
      const touchesFocus = Boolean(selected) && ends.includes(focused);
      edge.hidden = Boolean(selected) && !touchesFocus;
      edge.classList.toggle("is-related", touchesFocus);
    });
    if (status) {
      const visible = nodes.filter((node) => !node.hidden).length;
      status.textContent = selected
        ? `${selected.querySelector("strong")?.textContent || "Selected"}: ${related.size} direct relationship${related.size === 1 ? "" : "s"}.`
        : `${visible} of ${nodes.length} nodes shown. Select one to isolate its immediate relationships.`;
    }
  };

  const focus = (nodeId, remember = true) => {
    focused = nodes.some((node) => node.dataset.topologyNode === nodeId) ? nodeId : "";
    render();
    if (remember) rememberFocus(focused);
    if (focused && remember) {
      nodes.find((node) => node.dataset.topologyNode === focused)?.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "nearest",
        inline: "center",
      });
    }
  };

  const filter = () => {
    // Decide, then draw — once. A focus the toolbar has just excluded is
    // dropped before anything is painted, so the explorer never shows a
    // neighbourhood it is about to discard.
    const selected = nodes.find((node) => node.dataset.topologyNode === focused);
    if (selected && !passesToolbar(selected)) {
      focused = "";
      rememberFocus("");
    }
    render();
  };

  // A node title is a link inside a <summary>, so one click can mean two
  // things: follow it, and toggle the disclosure. Blink suppresses the toggle
  // for a click on an interactive descendant; other engines do both, which
  // navigates away from a node it just expanded. preventDefault cancels the
  // toggle and the navigation together, so the navigation is reissued here --
  // and only for the plain click, leaving middle-click and modified clicks to
  // the browser, where opening a new tab was the whole intent.
  workspace.addEventListener("click", (event) => {
    const link = event.target.closest?.("summary a[href]");
    if (!link) return;
    event.stopPropagation();
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    window.location.assign(link.href);
  });

  nodes.forEach((node) => {
    node.addEventListener("toggle", () => {
      if (node.open) {
        nodes.forEach((other) => {
          if (other !== node) other.removeAttribute("open");
        });
        focus(node.dataset.topologyNode);
      } else if (focused === node.dataset.topologyNode) {
        focus("");
      }
    });
  });
  search?.addEventListener("input", filter);
  kindControls.forEach((control) => control.addEventListener("change", filter));
  reset?.addEventListener("click", () => {
    focus("");
    nodes.forEach((node) => node.removeAttribute("open"));
  });
  search?.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      search.value = "";
      filter();
    } else if (event.key === "Enter") {
      const first = nodes.find((node) => !node.hidden);
      if (first) {
        first.open = true;
        first.querySelector("summary")?.focus();
      }
    }
  });
  render();
});
