// Mounts the vendored Scalar bundle (static/vendor/scalar) on /api/docs/.
// Runs before the bundle: zod inside it probes for eval unless told not to,
// and HQ's policy refuses eval.
globalThis.__zod_globalConfig = { jitless: true };

// Every code-sample target the bundle ships, named so all but curl can be
// hidden: the option is a list of what to hide, with no list of what to show.
// curl is the one sample every reader can run.
const HIDDEN_CLIENTS = {
  shell: ["httpie", "wget"],
  ...Object.fromEntries(
    [
      "c", "clojure", "csharp", "dart", "fsharp", "go", "http", "java", "js", "julia", "kotlin",
      "node", "objc", "ocaml", "php", "powershell", "python", "r", "ruby", "rust", "swift",
    ].map((target) => [target, true]),
  ),
};

// The bundle's sentences for a type or format with no description of its own.
const STOCK_SENTENCES = [
  "typeInteger", "formatInt32", "formatInt64", "formatDate", "formatDateTime",
  "formatPassword", "formatBase64", "formatBinary",
];

// Where a line may end inside a path: before each "/", "." or "-", the
// places Scalar's own path text takes (`ScalarWrappingText`'s `path` preset,
// static/vendor/scalar/standalone.js line 964), and before a query's "?" and
// "&"; never after a space, a ":" or another of them, so "https://" and
// "../" stay whole.
const PATH_BREAK = /(?<=[^\s/.?&:-])[/.?&-]/g;

// A path is a word with a "/" in it. Neither browser engine ends a line
// inside one but where any character may end it (`overflow-wrap`), which
// reads as a word cut in two ("refresh.reque" over "st/"), and CSS has no way
// to name the characters a line may end at. A <wbr> is the platform's place
// to end one, and is never copied. Scalar draws an operation's heading with
// one before each such character, and its list of a section's operations
// and its samples as plain text, so each text node holding a path is split at
// each such place with a <wbr> between, as it is drawn. The viewer keeps its
// own node, the first part, and may write a new path into it later, so the
// parts split off after it are kept to be taken out then.
const splitOff = new WeakMap();
const markPaths = (node) => {
  if (!node.isConnected || !node.data.includes("/")) return;
  const places = [];
  for (const word of node.data.matchAll(/\S*\/\S*/g)) {
    for (const at of word[0].matchAll(PATH_BREAK)) places.push(word.index + at.index);
  }
  const parts = [];
  for (const place of places.reverse()) {
    const rest = node.splitText(place);
    const wbr = document.createElement("wbr");
    rest.before(wbr);
    parts.push(wbr, rest);
  }
  if (parts.length) splitOff.set(node, parts);
};
const remarkPaths = (node) => {
  splitOff.get(node)?.forEach((part) => part.remove());
  splitOff.delete(node);
  markPaths(node);
};
const markPathsIn = (added) => {
  if (added.nodeType === Node.TEXT_NODE) { markPaths(added); return; }
  if (added.nodeType !== Node.ELEMENT_NODE) return;
  const walker = document.createTreeWalker(added, NodeFilter.SHOW_TEXT);
  const found = [];
  for (let text = walker.nextNode(); text; text = walker.nextNode()) found.push(text);
  found.forEach(markPaths);
};

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("api-reference-root");
  // Each path is split as the viewer draws it, in the same task, so no line
  // is drawn first and moved after. What the splitting itself changes is
  // dropped unread.
  const drawn = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "characterData") remarkPaths(record.target);
      else record.addedNodes.forEach(markPathsIn);
    }
    drawn.takeRecords();
  });
  drawn.observe(root, { childList: true, characterData: true, subtree: true });
  // Scalar scrolls an entry to just below what it is told stands over the
  // page, so HQ's header is named to it. Where its bars stick is the
  // stylesheet's (static/css/api-reference.css).
  document.querySelector(".site-header")?.setAttribute("data-scalar-scroll-header", "");
  // Scalar holds the page on an entry it was sent to that is not drawn yet,
  // scrolling back to it every frame until its next batch is drawn
  // (`freeze` in its lazy-bus `scrollToLazy`), and nothing stops that hold
  // when a reader follows another entry meanwhile: the page is pulled back to
  // the earlier entry while the address names the newer one. Scalar writes
  // the entry followed last to the address before it scrolls there, so a
  // scroll to one of its entries goes ahead only for the entry the address
  // names, or the one holding it (a deep link into an operation that falls
  // back to the operation). Its entries are ids "<document>/<entry>" in the
  // root, scrolled to the top of the view; every other scroll is left alone.
  // Scalar's own fix (scalar/scalar#10805) ends the hold, so this goes once
  // the vendored bundle carries it.
  const scrollIntoView = Element.prototype.scrollIntoView;
  const addressed = () => {
    try { return decodeURIComponent(window.location.hash.slice(1)); } catch { return ""; }
  };
  Element.prototype.scrollIntoView = function (options) {
    const entry = this.id.slice(this.id.indexOf("/") + 1);
    const named = addressed();
    const stale = options?.block === "start" && this.id.includes("/") && root.contains(this)
      && named && named !== entry && !named.startsWith(`${entry}.`);
    if (!stale) scrollIntoView.call(this, options);
  };
  // The operator's theme is the one on <html>: light, dark, or none for the
  // system's. A chosen theme is forced on Scalar; with none Scalar follows the
  // system too, once a mode it stored itself is out of the way.
  const theme = document.documentElement.dataset.theme;
  if (!theme) {
    try { localStorage.removeItem("colorMode"); } catch { /* Storage is off: nothing was stored. */ }
  }
  window.Scalar.createApiReference(root, {
    url: root.dataset.url,
    // The document names no server: it is served by the one it describes. The
    // samples are written against this origin, so a copied one runs as it is.
    servers: [{ url: window.location.origin }],
    // Color, type and spacing are HQ's tokens (static/css/api-reference.css).
    theme: "none",
    hideDarkModeToggle: true,
    ...(theme ? { forceDarkModeState: theme } : {}),
    // The schemas are shown where an operation uses them, so a field reads by
    // its type ("string", "object"), never by the name of the schema it refers
    // to, which names nothing a reader can open.
    hideModels: true,
    hideModelNames: true,
    // What a capability does and the grants it needs are the operation's own
    // fields, drawn beside it.
    showExtensions: ["x-hq-effect", "x-hq-required-capabilities"],
    // The API takes a bearer token from the identity provider, never this
    // page's session, so a request sent from here has nothing to send. This
    // also drops the panel that asks for a token.
    hideTestRequestButton: true,
    hiddenClients: HIDDEN_CLIENTS,
    defaultHttpClient: { targetKey: "shell", clientKey: "curl" },
    // The document is served as JSON; the link opens it as served.
    documentDownloadType: "direct",
    // ⌘K is HQ's "Find anything". This search covers the reference alone, and
    // says so.
    searchHotKey: "j",
    // A property the document leaves undescribed is given a stock sentence
    // for its type or format ("Integer numbers.", RFC 3339's wording for a
    // date-time), which restates what its line already says. Each is said as
    // nothing, so it is not drawn.
    localization: {
      translations: {
        search: { label: "Search the reference" },
        schema: Object.fromEntries(STOCK_SENTENCES.map((key) => [key, ""])),
      },
    },
    // Everything Scalar would fetch from its own servers, or link to there:
    // fonts, telemetry, its hosted client, agent and MCP generator, and the
    // toolbar that leads to them.
    withDefaultFonts: false,
    telemetry: false,
    hideClientButton: true,
    showDeveloperTools: "never",
    agent: { disabled: true },
    mcp: { disabled: true },
  });
});
