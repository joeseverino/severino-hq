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
