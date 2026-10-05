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
//   data-fragment-history     "replace" or "push": what an in-place answer
//                             does to the address bar
//   data-fragment-failure     what a region says when it cannot be had
//   data-fragment-fallback    the page that answers the same thing, offered
//                             beside that line
//   data-fragment-target      on a link or a form: the id of the region its
//                             answer belongs in; empty for the region it is
//                             inside
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

  const SIGN_IN = "/accounts/login/";
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

  // One line, the same everywhere a part could not be had.
  const explain = (region, text, link) => {
    const line = document.createElement("p");
    line.className = "notice notice-attention";
    line.setAttribute("role", "status");
    line.append(text);
    if (link) {
      const anchor = document.createElement("a");
      anchor.href = link.href;
      anchor.textContent = link.label;
      line.append(" ", anchor);
    }
    region.replaceChildren(line);
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
    if (!response.ok) throw new Error(String(response.status));
    return { response, page: parse(await response.text()) };
  };

  const keyOfDisclosure = (details) => details.id || details.dataset.fragmentPart || "";

  // What the reader did to a region outlives its replacement: the disclosures
  // they opened, how far they scrolled inside it, and where the keyboard was.
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
    if (element.name) return `[name="${CSS.escape(element.name)}"]`;
    if (element.getAttribute("rel")) return `a[rel="${CSS.escape(element.getAttribute("rel"))}"]`;
    const href = element.getAttribute("href");
    if (href) return `a[href="${CSS.escape(href)}"]`;
    const action = element.closest("form")?.getAttribute("action");
    return action ? `form[action="${CSS.escape(action)}"] button` : "";
  };

  const focusOf = (regions) => {
    const active = document.activeElement;
    if (!active || !regions.some((region) => region.contains(active))) return null;
    const text = active.matches("input[type=search], input[type=text], input:not([type]), textarea");
    return {
      key: focusKey(active),
      // What was typed may be past what this answer's render reflects.
      text: text ? { value: active.value, start: active.selectionStart, end: active.selectionEnd } : null,
    };
  };

  const refocus = (memo, roots) => {
    if (!memo?.key) return;
    for (const root of roots) {
      const revived = root.isConnected ? within(root, memo.key)[0] : null;
      if (!revived) continue;
      if (memo.text) revived.value = memo.text.value;
      revived.focus({ preventScroll: true });
      if (memo.text && typeof memo.text.start === "number") {
        revived.setSelectionRange(memo.text.start, memo.text.end);
      }
      return;
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

  // Puts `nodes` where `was` stands and says what is there now.
  const place = (was, nodes) => {
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
  //   history, title     "push" or "replace" the address; take the answer's title
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
      const answers = options.inner ? [[...page.body.childNodes]] : pair(target, page);
      // Matches that differ in number are not the page this one was: nothing
      // is touched.
      if (!single && options.strict !== false) {
        groups.forEach((group, index) => {
          if (!group.length || answers[index].length !== group.length) throw new Error("no region");
        });
      }
      options.before?.(page);
      const memo = focusOf(regions);
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
      const landed = response.redirected ? response.url : url;
      if (options.history === "push") window.history.pushState({}, "", landed);
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

  document.addEventListener(
    "toggle",
    (event) => {
      if (event.target instanceof HTMLDetailsElement && event.target.open) reveal(event.target);
    },
    true,
  );

  const regionFor = (control) => {
    if (control.dataset.fragmentTarget) return document.getElementById(control.dataset.fragmentTarget);
    if (control.dataset.fragmentTarget === "") return control.closest("[data-fragment]");
    const region = control.closest("[data-fragment-links]");
    const address = control.href || control.action;
    // Within such a region only what opens its own page again opens in
    // place: a record's own page is a page.
    if (!region || new URL(address, window.location.href).pathname !== window.location.pathname) return null;
    return region;
  };

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
  // said beside the form, in its `[data-fragment-status]` or in the region.
  const submit = async (form, submitter, region) => {
    // Busy, not disabled: a disabled button drops the focus it holds. A
    // second press while the first is out is ignored.
    if (form.getAttribute("aria-busy") === "true") return;
    const body = new FormData(form);
    if (submitter?.name) body.set(submitter.name, submitter.value);
    const posting = form.method === "post";
    const url = posting ? form.action : `${form.action.split("?")[0]}?${new URLSearchParams(body)}`;
    const status = form.querySelector("[data-fragment-status]") || region.querySelector("[data-fragment-status]");
    // `elements`, so a button that names the form from outside it counts.
    const controls = [...form.elements].filter((control) => control instanceof HTMLButtonElement);
    form.setAttribute("aria-busy", "true");
    controls.forEach((control) => control.setAttribute("aria-disabled", "true"));
    if (status) status.textContent = form.dataset.fragmentBusy || "";
    // Asked before the swap: afterwards the region is another element.
    const box = form.closest("dialog, details[data-menu]");
    const done = box && !box.contains(region) ? box : null;
    try {
      await swap(region, {
        url,
        method: posting ? "POST" : undefined,
        body: posting ? body : undefined,
        history: region.dataset.fragmentHistory,
      });
      if (status) status.textContent = "";
      // A form that saved from inside a dialog or a menu has nothing more to
      // say there.
      if (done instanceof HTMLDialogElement) done.close();
      else done?.removeAttribute("open");
    } catch (error) {
      if (error.name === "AbortError") return;
      if (!posting || error.signIn) {
        window.location.assign(posting ? error.signIn : url);
        return;
      }
      const said = form.dataset.fragmentFailure || region.dataset.fragmentFailure || "That could not be sent.";
      if (status?.isConnected) status.textContent = said;
      else explain(region, said);
    } finally {
      form.removeAttribute("aria-busy");
      controls.forEach((control) => control.removeAttribute("aria-disabled"));
    }
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    if (form.dataset.fragmentTarget === undefined && !form.closest("[data-fragment-links]")) return;
    const region = regionFor(form);
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

  return { swap, reveal, fail };
})();
window.hqFragment = hqFragment;
