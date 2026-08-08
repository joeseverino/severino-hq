"use strict";

document.addEventListener("click", (event) => {
  const menu = document.querySelector("details.user-menu");
  if (menu?.open && !menu.contains(event.target)) menu.removeAttribute("open");
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    document.querySelector("details.user-menu")?.removeAttribute("open");
  }
});

document.addEventListener("change", (event) => {
  const control = event.target.closest("[data-submit-on-change]");
  if (control?.form) control.form.requestSubmit();
});
