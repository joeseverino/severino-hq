"use strict";

// Moving between pages fades (`@view-transition` in the stylesheet). A
// transition the browser gives up on, because the window changed size or the
// page was hidden, is not an error: the page is simply shown.
//
// Loaded in the head, without `defer`: `pagereveal` fires before the new
// page's first frame, which is before a deferred script runs, so a listener
// there would miss the transition it has to answer for.
["pagereveal", "pageswap"].forEach((type) =>
  window.addEventListener(type, (event) => {
    const transition = event.viewTransition;
    [transition?.ready, transition?.finished, transition?.updateCallbackDone].forEach((step) =>
      step?.catch(() => {}),
    );
  }),
);
