"use strict";

// Disclosure menus that dismiss on an outside click or Escape. One selector
// covers every such menu, so adding another is handled by construction rather
// than by remembering to extend a hardcoded query. That query was extended
// twice, once per menu somebody added and then found stayed open over the top
// of the next one -- so a menu now says for itself that it dismisses, and the
// third case fixed itself before anyone noticed it.
const DISMISSIBLE_MENUS = "details[data-menu]";

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
  if (menu && event.target.closest("summary")) menu.dataset.pinned = "true";
  // The clicked menu is left alone -- the browser handles its own summary
  // toggle. Every other open menu closes, so two panels are never stacked.
  closeMenus(menu);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeMenus(null);
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

// The command center is global, so its shortcut is global too. The header
// field remains a normal GET form; this only removes the click needed to reach
// it and leaves local table search's `/` shortcut untouched.
document.addEventListener("keydown", (event) => {
  if (!(event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey))) return;
  if (event.target.closest("input, textarea, select, [contenteditable]")) return;
  const search = document.querySelector(".global-search input[type=search]");
  if (!search) return;
  event.preventDefault();
  search.focus();
  search.select();
});

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
    const response = await fetch(link.href, {
      credentials: "same-origin",
      headers: { "X-Fragment": "calendar" },
    });
    if (!response.ok) return false;
    const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
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
      const response = await fetch(panel.dataset.job, {
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
  fetch(form.action, {
    method: "POST",
    body: new FormData(form),
    headers: { "X-Requested-With": "fetch" },
    credentials: "same-origin",
  })
    .then((response) => {
      if (!response.ok) throw new Error(String(response.status));
      return response.text();
    })
    .then((html) => {
      // The server answers with the panel it just changed, so the page shows
      // what was stored rather than what the browser believes was stored.
      const parsed = new DOMParser().parseFromString(html, "text/html");
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
  fetch(`${form.action}?${query}`, {
    headers: { "X-Requested-With": "fetch" },
    credentials: "same-origin",
  })
    .then((response) => (response.ok ? response.text() : Promise.reject(response)))
    .then((html) => {
      const parsed = new DOMParser().parseFromString(html, "text/html");
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
  fetch(opener.dataset.connectionSource, {
    headers: { "X-Requested-With": "fetch" },
    credentials: "same-origin",
  })
    .then((response) => (response.ok ? response.text() : Promise.reject(response)))
    .then((html) => {
      const panel = new DOMParser()
        .parseFromString(html, "text/html")
        .querySelector("[data-connection-panel]");
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
    return fetch(`${endpoint}?t=${started}`, {
      cache: "no-store",
      credentials: "same-origin",
    }).then(() => performance.now() - started);
  };

  const render = (slot, runs) => {
    const best = Math.min(...runs);
    const worst = Math.max(...runs);
    // Each bar relative to the slowest sample, so the shape shows variation
    // rather than an absolute scale nobody can read at this size.
    const bars = runs
      .slice(-12)
      .map((run) => `<i style="--at: ${Math.max(12, (run / worst) * 100)}"></i>`)
      .join("");
    slot.innerHTML =
      `<span class="conn-live"><span class="conn-pulse"></span>` +
      `<strong>${best.toFixed(1)} ms</strong>` +
      `<span class="conn-samples">${bars}</span></span>`;
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
  ["content-security-policy", "Which origins may load script, style and frames"],
  ["strict-transport-security", "Refuses plain HTTP for this host from now on"],
  ["x-content-type-options", "Stops the browser guessing a type it was not sent"],
  ["x-frame-options", "Refuses to be framed by another site"],
  ["referrer-policy", "How much of this URL travels to anywhere you click"],
  ["cross-origin-opener-policy", "Keeps other origins out of this browsing context"],
  ["permissions-policy", "Which device capabilities this page may ask for"],
];

const hqShowResponseHeaders = (root) => {
  const slot = root.querySelector("[data-response-headers]");
  if (!slot || !window.fetch) return;
  fetch(window.location.href, { credentials: "same-origin", cache: "no-store" })
    .then((response) => {
      slot.innerHTML = HQ_RESPONSE_HEADERS.map(([name, purpose]) => {
        const value = response.headers.get(name);
        const shown = value
          ? `<code>${name}: ${value.length > 76 ? `${value.slice(0, 76)}…` : value}</code>`
          : `<code>${name}</code>`;
        const chip = value
          ? `<span class="conn-kind conn-kind-read">sent</span>`
          : `<span class="conn-kind conn-kind-elsewhere">absent</span>`;
        return `<div class="conn-row conn-row-wide">${shown}${chip}<span class="conn-row-note">${purpose}</span></div>`;
      }).join("");
    })
    .catch(() => {
      slot.innerHTML =
        '<div class="conn-row"><span class="conn-row-note">The response could not be read back.</span></div>';
    });
};

document.addEventListener("DOMContentLoaded", () => {
  const panel = document.querySelector("[data-connection-panel]");
  if (panel && !panel.closest("dialog")) hqShowResponseHeaders(panel);
});

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
  let focused = "";

  const rememberFocus = (nodeId) => {
    const url = new URL(window.location.href);
    if (nodeId) url.searchParams.set("focus", nodeId);
    else url.searchParams.delete("focus");
    window.history.replaceState({}, "", url);
  };

  const focus = (nodeId, remember = true) => {
    focused = nodes.some((node) => node.dataset.topologyNode === nodeId) ? nodeId : "";
    const selected = nodes.find((node) => node.dataset.topologyNode === focused);
    const related = new Set(selected?.dataset.topologyNeighbors.split(" ").filter(Boolean) || []);
    workspace.classList.toggle("has-focus", Boolean(selected));
    nodes.forEach((node) => {
      const id = node.dataset.topologyNode;
      node.classList.toggle("is-selected", id === focused);
      node.classList.toggle("is-related", related.has(id));
      node.classList.toggle(
        "is-dimmed",
        Boolean(selected) && id !== focused && !related.has(id) && !node.open,
      );
    });
    edges.forEach((edge) => {
      const ends = edge.dataset.topologyEdge.split(" ");
      edge.classList.toggle("is-related", Boolean(selected) && ends.includes(focused));
    });
    if (status) {
      status.textContent = selected
        ? `${selected.querySelector("strong")?.textContent || "Selected"}: ${related.size} direct relationship${related.size === 1 ? "" : "s"}.`
        : "Select a node to isolate its immediate relationships. Open it for actions and detail.";
    }
    if (remember) rememberFocus(focused);
  };

  const filter = () => {
    const terms = (search?.value || "").trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const shownKinds = new Set(kindControls.filter((control) => control.checked).map((control) => control.value));
    nodes.forEach((node) => {
      const haystack = node.dataset.topologySearchText.toLocaleLowerCase();
      node.hidden = !shownKinds.has(node.dataset.topologyNodeKind)
        || !terms.every((term) => haystack.includes(term));
    });
    lanes.forEach((lane) => {
      const visible = [...lane.querySelectorAll("[data-topology-node]")].filter((node) => !node.hidden);
      lane.hidden = visible.length === 0;
      const count = lane.querySelector("[data-topology-count]");
      if (count) count.textContent = visible.length;
    });
    const selected = nodes.find((node) => node.dataset.topologyNode === focused);
    if (selected?.hidden) focus("");
  };

  nodes.forEach((node) => {
    node.addEventListener("toggle", () => {
      if (node.open) {
        nodes.forEach((other) => {
          if (other !== node) other.removeAttribute("open");
        });
        focus(node.dataset.topologyNode);
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
    }
  });
  filter();
  if (workspace.dataset.focus) focus(workspace.dataset.focus, false);
});
