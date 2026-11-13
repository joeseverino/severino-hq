"use strict";

// Reads, as one component. A part of a page says where it comes from; this
// file fetches it, turns the answer into markup and puts it in place. Nothing
// else in HQ's scripts parses a response or keeps a timer that asks again:
// `FragmentPrimitiveTests` holds the other files to that.
//
//   data-fragment="<url>"     a region: the address that answers it. Empty
//                             means the page it is on.
//   data-fragment-name        the part the server is asked for by name, sent
//                             as `X-Fragment`. A view that knows the name
//                             renders that part alone; one that does not
//                             answers the whole page and the region is found
//                             in it by its id.
//   data-fragment-load        a placeholder: fetched once the page is up, or
//                             when the disclosure or dialog around it opens
//   data-fragment-poll="<s>"  asked again every so many seconds for as long
//                             as the server keeps drawing the attribute
//   data-fragment-part="<k>"  inside a region: parts are swapped one by one,
//                             and only those that changed
//   data-fragment-links       links and forms inside the region that lead to
//                             the region's own page are answered in place
//   data-fragment-history     "replace": an in-place answer replaces the
//                             address. Nothing restores a region's entry
//                             on Back, so one given another value is not
//                             answered in place
//   data-fragment-failure     what a region says when it cannot be had
//   data-fragment-fallback    the page that answers the same thing, offered
//                             beside that line
//   data-fragment-target      on a link or a form: the id of the region its
//                             answer belongs in; empty for the region it is
//                             inside
//
// A button that posts a form from outside it (`post_button`) needs none of
// these: it is answered in the region it sits in, and a page's content is one
// (base.html's `main`), so every such press keeps the page.
//
// Every link and form above works without this file: it only removes the
// page load.

// Every enhanced request crosses the session boundary the same way. When an
// OIDC session needs renewal, the server returns the provider URL instead of
// letting fetch follow a cross-origin redirect that CSP correctly blocks.
// `renewSession` decides whether a request is allowed to take the page away.
// A request the operator made can: they asked for something, and renewing is
// the way to get it. A background one must not: the provider returns to the
// address that triggered the renewal, so a renewing background fetch would
// land the operator on the JSON that fetch wanted.
// A write form's retry key (`{% write_token %}`) names one sending of it, so a
// resend acts once. Once HQ has answered, the form a post came from, if it is
// still on the page, takes a new key and asks afresh next time. An answer
// that never arrived, or a failure of the server's own, keeps the key, so
// sending again cannot act twice.
const RETRY_KEY = "__retry_key";
const spendKey = (body, response) => {
  if (!(body instanceof FormData) || response.status >= 500) return;
  const sent = body.get(RETRY_KEY);
  if (!sent) return;
  const fresh = `web:${window.crypto.getRandomValues(new Uint8Array(24)).toHex()}`;
  document.querySelectorAll(`input[name="${RETRY_KEY}"]`).forEach((field) => {
    if (field.value === sent) field.value = fresh;
  });
};

const hqFetch = async (input, options = {}) => {
  const { renewSession = true, ...rest } = options;
  const headers = new Headers(rest.headers);
  headers.set("X-Requested-With", "XMLHttpRequest");
  // eslint-disable-next-line no-restricted-properties -- HQ's one request, which every enhanced request crosses
  const response = await window.fetch(input, { ...rest, headers });
  spendKey(rest.body, response);
  const refreshUrl =
    response.status === 403 ? response.headers.get("refresh_url") : "";
  if (refreshUrl && renewSession) {
    window.location.assign(refreshUrl);
    return new Promise(() => {});
  }
  return response;
};
window.hqFetch = hqFetch;

// The one loop. Anything that asks again later is a task here: a region that
// polls, work being followed, a figure that ticks. One timer serves them all.
// A hidden tab runs only the tasks that give a hidden pace, and a tab brought
// back runs what came due while it was away. A task that fails is asked less
// often, up to eight times its pace, until it answers again.
//
// `work` answers false to stop; anything else keeps it going.
const hqEvery = (() => {
  const tasks = new Set();
  let timer = null;
  const pace = (task) =>
    document.visibilityState === "visible" ? task.wait : task.hiddenMs;

  const plan = () => {
    window.clearTimeout(timer);
    timer = null;
    let soonest = Infinity;
    tasks.forEach((task) => {
      if (!task.running && pace(task)) soonest = Math.min(soonest, task.last + pace(task));
    });
    if (soonest < Infinity) {
      // eslint-disable-next-line no-restricted-properties -- the one timer every task that asks again shares
      timer = window.setTimeout(turn, Math.max(0, soonest - Date.now()));
    }
  };

  const settle = (task, answer) => {
    task.running = false;
    task.last = Date.now();
    if (answer === false) tasks.delete(task);
    else task.wait = answer === "failed" ? Math.min(task.wait * 2, task.ms * 8) : task.ms;
    plan();
  };

  const turn = () => {
    const now = Date.now();
    tasks.forEach((task) => {
      if (task.running || !pace(task) || task.last + pace(task) > now) return;
      if (task.until && now >= task.until) {
        tasks.delete(task);
        task.expired?.();
        return;
      }
      task.running = true;
      Promise.resolve()
        .then(task.work)
        .then((answer) => settle(task, answer), () => settle(task, "failed"));
    });
    plan();
  };

  document.addEventListener("visibilitychange", plan);

  return (work, { ms, hiddenMs = 0, limitMs = 0, expired } = {}) => {
    const task = {
      work,
      ms,
      wait: ms,
      hiddenMs,
      expired,
      last: Date.now(),
      until: limitMs ? Date.now() + limitMs : 0,
      running: false,
    };
    tasks.add(task);
    plan();
    return () => tasks.delete(task);
  };
})();
window.hqEvery = hqEvery;

const hqFragment = (() => {
  // The one place in HQ where a string becomes markup.
  //
  // `DOMParser.parseFromString` is a Trusted Types sink, which is exactly the
  // point. The content policy names one policy and does not allow duplicates,
  // so this is the only one that can ever exist on the page. Every other sink
  // still throws, and script that gets itself onto the page cannot mint the
  // permission to reach one, because the name is already taken and the
  // function below is reachable only from inside this closure.
  //
  // What it accepts is narrow by construction rather than by inspection: its
  // one caller passes the body of a same-origin response HQ itself rendered.
  const parse = (() => {
    let policy = { createHTML: (html) => html };
    try {
      // eslint-disable-next-line no-restricted-properties -- the one policy the content policy names
      policy = window.trustedTypes.createPolicy("hq-fragment", {
        createHTML: (html) => html,
      });
    } catch {
      // A browser without Trusted Types, or a policy already created. Parsing
      // still has to work either way; where the browser does enforce, the
      // pass-through is what the sink refuses.
    }
    return (html) =>
      // eslint-disable-next-line no-restricted-properties -- the one sink, fed a same-origin response HQ rendered
      new DOMParser().parseFromString(policy.createHTML(html), "text/html");
  })();

  // Where an expired session is sent: the page says, from the route itself.
  const SIGN_IN = document.body.dataset.signIn;
  const POLL_LIMIT_MS = 180_000;
  // What a request in flight is keyed by -> its abort. A newer request for
  // the same thing ends the older one, so answers never land out of order.
  const pending = new WeakMap();
  // Region -> the validator of the answer it shows and the address it came
  // from, presented with the next question so an unchanged part costs a 304.
  const shown = new WeakMap();
  const polled = new WeakSet();
  // A polled region that was replaced -> what replaced it, so its one poll
  // follows it instead of a second one starting.
  const successor = new WeakMap();

  // A notice in a tone, cloned from the empty one the server drew for it
  // (base.html), so a line a script adds is the part the server draws.
  const notice = (tone) =>
    document.querySelector(`template[data-notice="${tone}"]`).content.firstElementChild.cloneNode(true);
  // One line, the same everywhere a part could not be had.
  const explain = (region, text, link) => {
    const line = notice("warning");
    line.append(text);
    if (link) {
      const anchor = document.createElement("a");
      anchor.href = link.href;
      anchor.textContent = link.label;
      line.append(" ", anchor);
    }
    region.replaceChildren(line);
  };
  // A line among the page's messages, where the server says how a post went.
  const say = (region, text) => {
    let list = region.querySelector(":scope > .messages");
    if (!list) {
      list = document.createElement("ul");
      list.className = "messages";
      region.prepend(list);
    }
    const line = notice("error");
    line.textContent = text;
    const item = document.createElement("li");
    item.append(line);
    list.append(item);
  };

  // Why a post was refused, as the page it was answered with says it among
  // its messages: the server's own sentence, which nothing else carries once
  // that page is not the one shown. An error is the one notice that is an
  // alert.
  const refusals = (page) =>
    [...page.querySelectorAll(".messages [role=alert]")].map((line) => line.textContent.trim()).filter(Boolean);
  // Why an answer that is not a success was given, where it says: a refusal
  // is a sentence, or the page drawn again with it. A server's failure says
  // nothing a person can act on.
  const failed = async (response) => {
    const failure = new Error(String(response.status));
    const type = response.headers.get("Content-Type") || "";
    if (response.status < 500 && type.startsWith("text/plain")) {
      failure.said = [(await response.text()).trim()].filter(Boolean);
    } else if (response.status < 500 && type.startsWith("text/html")) {
      failure.said = refusals(parse(await response.text()));
    }
    return failure;
  };

  // A region says why it is empty: the session ended, or its own failure
  // line. One that declares neither keeps what it held.
  const fail = (region, error) => {
    region.removeAttribute("aria-busy");
    if (error?.signIn) {
      explain(region, "Your session has ended.", { href: error.signIn, label: "Sign in again" });
    } else if (region.dataset.fragmentFailure) {
      const page = region.dataset.fragmentFallback;
      explain(region, region.dataset.fragmentFailure, error?.link || (page && { href: page, label: "Open the page" }));
    }
  };

  const read = async (url, { name, method, body, renew, signal, validator }) => {
    const headers = {};
    if (name) headers["X-Fragment"] = name;
    if (validator) headers["If-None-Match"] = validator;
    const response = await hqFetch(url, {
      method,
      body,
      headers,
      signal,
      credentials: "same-origin",
      renewSession: renew,
    });
    if (response.status === 304) return { response, page: null };
    // A session that ended under an open tab answers with the sign-in page.
    if (response.redirected && new URL(response.url).pathname.startsWith(SIGN_IN)) {
      throw Object.assign(new Error("signed out"), { signIn: response.url });
    }
    if (!response.ok) throw await failed(response);
    return { response, page: parse(await response.text()) };
  };

  const keyOfDisclosure = (details) => details.id || details.dataset.fragmentPart || "";

  // What the reader did to a region outlives its replacement: the disclosures
  // and popovers they opened, how far they scrolled inside it, and where the
  // keyboard was.
  const within = (root, selector) => [
    ...(root.matches?.(selector) ? [root] : []),
    ...root.querySelectorAll(selector),
  ];

  const carry = (was, next) => {
    const open = new Map(
      within(was, "details").filter(keyOfDisclosure).map((d) => [keyOfDisclosure(d), d.open]),
    );
    within(next, "details").forEach((details) => {
      const key = keyOfDisclosure(details);
      if (open.has(key)) details.open = open.get(key);
    });
  };

  const scrolled = (was) =>
    within(was, "[id]")
      .filter((box) => box.scrollTop || box.scrollLeft)
      .map((box) => [box.id, box.scrollTop, box.scrollLeft]);

  const focusKey = (element) => {
    if (element.id) return `#${CSS.escape(element.id)}`;
    // A button that posts on its own (`post_button`) is told apart by where
    // it posts and what it says there, which no other button on the page shares.
    const posts = element.getAttribute("formaction");
    if (posts) {
      const value = element.getAttribute("value");
      return `button[formaction="${CSS.escape(posts)}"]${value ? `[value="${CSS.escape(value)}"]` : ""}`;
    }
    if (element.name) return `[name="${CSS.escape(element.name)}"]`;
    if (element.getAttribute("rel")) return `a[rel="${CSS.escape(element.getAttribute("rel"))}"]`;
    const href = element.getAttribute("href");
    if (href) return `a[href="${CSS.escape(href)}"]`;
    const action = element.closest("form")?.getAttribute("action");
    return action ? `form[action="${CSS.escape(action)}"] button` : "";
  };

  // Where the keyboard goes when its control is gone or can no longer be
  // pressed (a row moved to the top, a row deleted): the rest of its row,
  // then the row after it, then the one before.
  const CONTROLS = "a[href], button, input, select, textarea";
  const neighbors = (element) => {
    const row = element.closest("tr, li");
    if (!row) return [];
    const own = [...row.querySelectorAll(CONTROLS)];
    const at = own.indexOf(element);
    const of = (other) => (other ? [...other.querySelectorAll(CONTROLS)] : []);
    return [
      ...own.slice(at + 1),
      ...own.slice(0, Math.max(at, 0)).reverse(),
      ...of(row.nextElementSibling),
      ...of(row.previousElementSibling),
    ];
  };

  const focusOf = (regions, pressed) => {
    const active = pressed || document.activeElement;
    if (!active || !regions.some((region) => region.contains(active))) return null;
    const text = active.matches("input[type=search], input[type=text], input:not([type]), textarea");
    return {
      keys: [active, ...neighbors(active)].map(focusKey).filter(Boolean),
      // What was typed may be past what this answer's render reflects.
      text: text ? { value: active.value, start: active.selectionStart, end: active.selectionEnd } : null,
    };
  };

  const refocus = (memo, roots) => {
    for (const [index, key] of (memo?.keys || []).entries()) {
      for (const root of roots) {
        const revived = root.isConnected ? within(root, key).find((found) => !found.matches(":disabled")) : null;
        if (!revived) continue;
        const text = index === 0 ? memo.text : null;
        if (text) revived.value = text.value;
        revived.focus({ preventScroll: true });
        if (text && typeof text.start === "number") revived.setSelectionRange(text.start, text.end);
        return;
      }
    }
  };

  // A region that names its parts keeps itself and takes only the parts that
  // changed, so an answer that brings nothing new leaves the page exactly as
  // it was. Its own attributes follow the server either way: that is how a
  // poll ends.
  const byParts = (was, next) => {
    const parts = (root) => [...root.querySelectorAll("[data-fragment-part]")];
    const order = (list) => list.map((part) => part.dataset.fragmentPart).join();
    const [old, fresh] = [parts(was), parts(next)];
    if (!old.length || order(old) !== order(fresh)) return false;
    [...was.attributes].forEach(({ name }) => {
      if (name.startsWith("data-fragment") && !next.hasAttribute(name)) was.removeAttribute(name);
    });
    [...next.attributes].forEach(({ name, value }) => {
      if (name.startsWith("data-fragment")) was.setAttribute(name, value);
    });
    old.forEach((part, index) => {
      if (part.outerHTML !== fresh[index].outerHTML) part.replaceWith(fresh[index]);
    });
    return true;
  };

  // A popover is shown only while it is on the page, so one the reader had
  // open is shown again once the element that replaces it, found by its id,
  // is placed: a filter's panel stays open across the answer to its own box.
  const place = (was, nodes) => {
    const raised = within(was, "[popover][id]:popover-open").map((popover) => popover.id);
    const placed = put(was, nodes);
    raised.forEach((id) => {
      const popover = document.getElementById(id);
      if (popover?.popover && !popover.matches(":popover-open")) popover.showPopover();
    });
    return placed;
  };

  // Puts `nodes` where `was` stands and says what is there now.
  const put = (was, nodes) => {
    const elements = nodes.filter((node) => node.nodeType === Node.ELEMENT_NODE);
    elements.forEach((element) => carry(was, element));
    if (elements.length === 1 && byParts(was, elements[0])) return [was];
    const offsets = scrolled(was);
    if (!elements.length) {
      was.remove();
      return [];
    }
    if (elements.length === 1 && polled.has(was)) {
      polled.add(elements[0]);
      successor.set(was, elements[0]);
    }
    was.replaceWith(...nodes);
    offsets.forEach(([id, top, left]) => {
      const box = document.getElementById(id);
      if (box) box.scrollTo(left, top);
    });
    return elements;
  };

  const announce = (roots) => {
    roots.forEach((root) => {
      if (!root.isConnected) return;
      root.dispatchEvent(new CustomEvent("hq:fragment", { bubbles: true }));
      watch(root);
    });
  };

  const regionsOf = (target) =>
    typeof target === "string" || Array.isArray(target)
      ? [target].flat().map((selector) => [...document.querySelectorAll(selector)])
      : [[target]];

  // What stands in for each region in the answer. A region with an id is
  // found by it. A list of selectors pairs each match with the answer's match
  // in the same position. A region with neither is answered by a bare part:
  // a response that is the part and nothing else.
  const pair = (target, page) => {
    if (typeof target === "string" || Array.isArray(target)) {
      return [target].flat().map((selector) => [...page.querySelectorAll(selector)]);
    }
    const found = target.id ? page.getElementById(target.id) : null;
    if (found) return [[found]];
    // A whole page where a part was expected is never poured into a region.
    if (page.title || page.querySelector("main")) throw new Error("not a part");
    return [[...page.body.childNodes]];
  };

  // Fetches and places. `target` is a region, or selectors whose matches on
  // this page are replaced by the answer's.
  //
  //   url, method, body  the question; the region's own address by default
  //   name               the part asked for; the region's own by default
  //   renew              false for a question nobody pressed anything to ask
  //   inner              the answer becomes the region's content, not the region
  //   strict             false lets a selector's matches differ in number: what
  //                      the answer no longer has is removed
  //   busy               the element marked busy; the regions by default
  //   before(page)       sees the answer before anything is placed
  //   history, title     "replace" the address; take the answer's title
  //   focus              the element whose place keeps the keyboard; the
  //                      focused one by default
  //   page               the answer is the page, the region found in it by
  //                      its id; anything else is shown as the page
  //
  // Resolves true when the answer was placed, false when it was unchanged.
  // Rejects when it could not be had, with nothing on the page touched.
  const swap = async (target, options = {}) => {
    const groups = regionsOf(target);
    const regions = groups.flat();
    if (!regions.length) throw new Error("no region");
    const single = target instanceof Element ? target : null;
    const names = new Set(regions.map((region) => region.dataset.fragmentName || ""));
    const url = options.url || single?.dataset.fragment || window.location.href;
    const key = options.busy || regions[0];
    const busy = options.busy ? [options.busy] : regions;

    pending.get(key)?.abort();
    const controller = new AbortController();
    pending.set(key, controller);
    busy.forEach((element) => element.setAttribute("aria-busy", "true"));
    try {
      const held = single && !options.method ? shown.get(single) : null;
      const { response, page } = await read(url, {
        name: options.name ?? (names.size === 1 ? [...names][0] : ""),
        method: options.method,
        body: options.body,
        renew: options.renew ?? true,
        signal: controller.signal,
        validator: held && held.url === String(url) ? held.validator : "",
      });
      if (!page) return false;
      const landed = response.redirected ? response.url : url;
      let answers;
      try {
        // A post sent on to another page is shown that page.
        const path = (address) => new URL(address, window.location.href).pathname;
        const homes = [window.location.href, single?.dataset.fragment].filter(Boolean).map(path);
        if (options.method && response.redirected && !homes.includes(path(landed))) {
          throw new Error("another page");
        }
        // A press is answered with the page it was made on, never a bare part.
        if (options.page && !(single?.id && page.getElementById(single.id))) throw new Error("not this page");
        answers = options.inner ? [[...page.body.childNodes]] : pair(target, page);
        // Matches that differ in number are not the page this one was: nothing
        // is touched.
        if (!single && options.strict !== false) {
          groups.forEach((group, index) => {
            if (!group.length || answers[index].length !== group.length) throw new Error("no region");
          });
        }
      } catch (error) {
        // A post that was answered has done its work, so an answer that
        // cannot be placed is read as the page: the one it was sent on to,
        // or for a press this one.
        if (options.method && (response.redirected || options.page)) {
          error.landed = response.redirected ? response.url : window.location.href;
          error.said = refusals(page);
        }
        throw error;
      }
      options.before?.(page);
      const memo = focusOf(regions, options.focus);
      const at = [window.scrollX, window.scrollY];
      let roots = [];
      if (options.inner) {
        single.replaceChildren(...answers[0]);
        roots = [single];
      } else if (single) {
        roots = place(single, answers[0]);
      } else {
        groups.forEach((group, index) => {
          group.forEach((region, position) => {
            const fresh = answers[index][position];
            if (fresh) roots.push(...place(region, [fresh]));
            else region.remove();
          });
        });
      }
      const validator = response.headers.get("ETag");
      if (validator && roots.length === 1) shown.set(roots[0], { url: String(url), validator });
      if (options.title && page.title) document.title = page.title;
      if (options.history === "replace") window.history.replaceState({}, "", landed);
      refocus(memo, roots);
      if (window.scrollX !== at[0] || window.scrollY !== at[1]) window.scrollTo(at[0], at[1]);
      announce(roots);
      return true;
    } finally {
      if (pending.get(key) === controller) {
        pending.delete(key);
        busy.forEach((element) => element.removeAttribute("aria-busy"));
      }
    }
  };

  // A region the server marks as still changing is asked again until the
  // server stops marking it, the region leaves the page, or a few minutes
  // pass. A tick is never a request to leave the page.
  function watch(region) {
    within(region, "[data-fragment-poll]").forEach((polling) => {
      if (polled.has(polling)) return;
      polled.add(polling);
      let current = polling;
      const stop = () => {
        polled.delete(current);
        return false;
      };
      const latest = () => {
        while (successor.has(current)) current = successor.get(current);
      };
      hqEvery(
        async () => {
          latest();
          if (!current.isConnected || !current.hasAttribute("data-fragment-poll")) return stop();
          try {
            await swap(current, { renew: false });
          } catch (error) {
            if (error.signIn) {
              fail(current, error);
              return stop();
            }
            throw error;
          }
          latest();
          return current.isConnected && current.hasAttribute("data-fragment-poll") ? true : stop();
        },
        {
          ms: Math.max(1, Number(polling.dataset.fragmentPoll) || 3) * 1000,
          limitMs: POLL_LIMIT_MS,
          expired: stop,
        },
      );
    });
  }

  // Placeholders: fetched once, when somebody could be reading them. One
  // inside a closed disclosure or dialog waits for it to open. An empty
  // answer leaves nothing behind.
  const closed = (slot) => slot.closest("details:not([open]), dialog:not([open])");
  const load = async (slot) => {
    if (slot.dataset.fragmentLoading) return;
    slot.dataset.fragmentLoading = "true";
    try {
      await swap(slot, { renew: false });
    } catch (error) {
      fail(slot, error);
      if (!slot.childElementCount) slot.remove();
    }
  };
  const reveal = (root) => {
    within(root, "[data-fragment-load]").forEach((slot) => {
      if (!closed(slot)) load(slot);
    });
  };

  // A disclosure or a dialog that opens reads what it held back: both fire
  // `toggle`, which does not bubble.
  document.addEventListener(
    "toggle",
    (event) => {
      if (event.target.open) reveal(event.target);
    },
    true,
  );

  const declaredRegion = (control) => {
    if (control.dataset.fragmentTarget) return document.getElementById(control.dataset.fragmentTarget);
    if (control.dataset.fragmentTarget === "") return control.closest("[data-fragment]");
    const region = control.closest("[data-fragment-links]");
    const address = control.href || control.action;
    // Within such a region only what opens its own page again opens in
    // place: a record's own page is a page.
    if (!region || new URL(address, window.location.href).pathname !== window.location.pathname) return null;
    return region;
  };
  // A region whose history is anything but "replace" is asked the ordinary
  // way, since Back would show its entry's address over another answer.
  const answerable = (region) => {
    const history = region?.dataset.fragmentHistory;
    return history === undefined || history === "replace" ? region : null;
  };
  const regionFor = (control) => answerable(declaredRegion(control));

  // A region's answer replaces its history entry, which a navigate listener
  // can do for a link only from a precommit handler, not in Safari yet; until
  // then links and forms are taken here, where tables.js takes a list's in
  // its navigate listener.
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[href]");
    if (!link || link.target || link.hasAttribute("download")) return;
    const declared = link.dataset.fragmentTarget !== undefined || link.closest("[data-fragment-links]");
    const region = declared ? regionFor(link) : null;
    if (!region) return;
    event.preventDefault();
    // Any failure falls through to the navigation the link already is.
    swap(region, { url: link.href, history: region.dataset.fragmentHistory }).catch((error) => {
      if (error.name !== "AbortError") window.location.assign(link.href);
    });
  });

  // A form answered in place. A question (GET) that cannot be answered here
  // is asked the ordinary way. A post is never sent twice: what went wrong is
  // said beside the form, in its `[data-fragment-status]` or in the region,
  // in the server's words where its answer gives them: a refusal is answered
  // here even when the server sent it on to another page, since showing that
  // page would read it again without them. Any other post answered with
  // another page, or one that is not this page's, shows that page, as the
  // post would have without this file.
  //
  // A button that names its form from outside it (`post_button`, which posts
  // the one form base.html draws) is answered in the region it sits in, and
  // the keyboard stays where it pressed.
  const submit = async (form, submitter, region) => {
    // Busy, not disabled: a disabled button drops the focus it holds. A
    // second press while the first is out is ignored.
    if (form.getAttribute("aria-busy") === "true") return;
    const body = new FormData(form, submitter);
    // What the button says of where and how it sends overrides its form's.
    const posting = (submitter?.hasAttribute("formmethod") ? submitter.formMethod : form.method) === "post";
    const action = submitter?.hasAttribute("formaction") ? submitter.formAction : form.action;
    const url = posting ? action : `${action.split("?")[0]}?${new URLSearchParams(body)}`;
    const pressed = submitter && !form.contains(submitter) ? submitter : null;
    // The one form a press posts was drawn with the page's first address; a
    // region answered in place since may have moved it (a month paged), and
    // the press comes back to where the reader is now.
    if (pressed && body.has("next")) body.set("next", window.location.pathname + window.location.search);
    // A press's region is the page's, whose status lines are other forms'.
    const status =
      form.querySelector("[data-fragment-status]") || (pressed ? null : region.querySelector("[data-fragment-status]"));
    // `elements`, so a button that names the form from outside it counts.
    const controls = [...form.elements].filter((control) => control instanceof HTMLButtonElement);
    form.setAttribute("aria-busy", "true");
    controls.forEach((control) => control.setAttribute("aria-disabled", "true"));
    if (status) status.textContent = form.dataset.fragmentBusy || "";
    // Asked before the swap: afterwards the region is another element.
    const box = form.closest("dialog");
    const done = box && !box.contains(region) ? box : null;
    try {
      await swap(region, {
        url,
        method: posting ? "POST" : undefined,
        body: posting ? body : undefined,
        history: region.dataset.fragmentHistory,
        // A press is marked busy on its form, which nobody sees: the region
        // stays as it was drawn until the answer replaces it.
        busy: pressed ? form : undefined,
        focus: pressed,
        page: Boolean(pressed),
      });
      if (status) status.textContent = "";
      // A form that saved from inside a dialog has nothing more to say there.
      done?.close();
    } catch (error) {
      if (error.name === "AbortError") return;
      const refused = posting && !error.signIn && error.said?.length ? error.said : null;
      if (!refused && (!posting || error.signIn || error.landed)) {
        window.location.assign(posting ? error.signIn || error.landed : url);
        return;
      }
      const said = refused || [
        form.dataset.fragmentFailure || region.dataset.fragmentFailure || "That could not be sent.",
      ];
      if (status?.isConnected) status.textContent = said.join(" ");
      // A press's region is the page's content, which keeps what it shows:
      // each line is said where the server says how a post went, among the
      // page's messages.
      else if (pressed) said.forEach((line) => say(region, line));
      else explain(region, said.join(" "));
    } finally {
      form.removeAttribute("aria-busy");
      controls.forEach((control) => control.removeAttribute("aria-disabled"));
    }
  };

  // The region a press is answered in: the form's own, when the form names
  // one; else, for a button outside its form, the region the button sits in.
  const submittedTo = (form, submitter) => {
    if (form.dataset.fragmentTarget !== undefined || form.closest("[data-fragment-links]")) return regionFor(form);
    if (!submitter || form.contains(submitter)) return null;
    return answerable(submitter.closest("[data-fragment]"));
  };

  // On the window, so it hears a submit after every listener on the
  // document: a script that answers a press of its own (ask.js) has it first.
  window.addEventListener("submit", (event) => {
    const form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    const region = submittedTo(form, event.submitter);
    if (!region) return;
    event.preventDefault();
    submit(form, event.submitter, region);
  });

  // A form marked `data-fragment-auto` is sent once by the page that drew
  // it: the page asking for what it was drawn without. Opening a page is not
  // a request to stay signed in, so it never renews a session.
  const auto = (form) => {
    const region = regionFor(form);
    if (!region) return;
    swap(region, { url: form.action, method: "POST", body: new FormData(form), renew: false })
      .catch(() => {});
  };

  reveal(document);
  watch(document.documentElement);
  document.querySelectorAll("form[data-fragment-auto]").forEach(auto);

  return { swap, fail };
})();
window.hqFragment = hqFragment;
