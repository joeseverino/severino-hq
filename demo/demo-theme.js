// Runs first on every demo page, without defer, before the page paints:
// - applies the theme chosen in the demo (demo.js keeps the choice);
// - opens the saved copy of an address with a query (a filter, a day, Show
//   all, a search), since the static host serves every query the page without
//   it. demo/build.py saved each copy under __q/ and listed it in
//   __fragments__/variants.json. demo.js says so when there is no copy.
(() => {
  try {
    const theme = (JSON.parse(localStorage.getItem("hq-demo")) || {}).theme;
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  } catch { /* private window */ }

  // One address however its query was written: its pairs in order, empty ones
  // left out. demo/build.py keys the same way (query_key).
  const key = (url) => {
    const pairs = [...url.searchParams].filter(([, value]) => value !== "");
    pairs.sort(([a, x], [b, y]) => (a < b ? -1 : a > b ? 1 : x < y ? -1 : x > y ? 1 : 0));
    return pairs.length ? `${url.pathname}?${new URLSearchParams(pairs)}` : url.pathname;
  };
  let route;
  const routes = () => (route ??= fetch("/__fragments__/variants.json")
    .then((response) => response.json())
    .catch(() => ({ variants: {}, searches: [], nothing: "" })));
  // A search the demo has no answer for is answered as the nearest word it
  // has: the shortest that starts with it, else the longest it starts with.
  const nearest = (asked, words, nothing) => {
    const word = asked.trim().toLowerCase();
    if (words.includes(word)) return word;
    const longer = words.filter((w) => w.startsWith(word)).sort((a, b) => a.length - b.length)[0];
    const shorter = words.filter((w) => word.startsWith(w)).sort((a, b) => b.length - a.length)[0];
    return longer || shorter || nothing;
  };
  const searchKey = (asked, found) => {
    const word = nearest(asked, found.searches, found.nothing);
    return key(new URL(`/search/?${new URLSearchParams({ q: word })}`, location.href));
  };
  // Where this document really came from, before its address is put back.
  window.hqDemo = { key, routes, searchKey, served: location.pathname, answered: true, answeredAs: "" };

  const address = document.querySelector('meta[name="hq-demo-address"]')?.content;
  if (address) {
    // A saved copy, shown at the address that was asked for. A search asked
    // for a word the demo has no copy of keeps that word, and says what it
    // was answered as.
    let asked = "";
    try {
      asked = sessionStorage.getItem("hq-demo-asked") || "";
      sessionStorage.removeItem("hq-demo-asked");
    } catch { /* private window */ }
    const copy = new URL(address, location.href);
    const shown = asked && new URL(asked, location.href).pathname === copy.pathname ? asked : address;
    if (key(new URL(shown, location.href)) !== key(copy)) window.hqDemo.answeredAs = copy.searchParams.get("q") || "";
    history.replaceState(history.state, "", shown + location.hash);
    return;
  }
  if (!location.search) return;
  const here = new URL(location.href);
  document.documentElement.style.visibility = "hidden";
  const reveal = () => { document.documentElement.style.visibility = ""; };
  routes().then((found) => {
    const asked = here.pathname === "/search/" ? here.searchParams.get("q") : "";
    const target = found.variants[key(here)] || (asked ? found.variants[searchKey(asked, found)] : "");
    if (target) {
      try { sessionStorage.setItem("hq-demo-asked", here.pathname + here.search); } catch { /* private window */ }
      location.replace(target + here.hash);
      return;
    }
    window.hqDemo.answered = false;
    reveal();
    document.dispatchEvent(new Event("hq-demo:unanswered"));
  }, reveal);
})();
