// Mounts the vendored Scalar bundle (static/vendor/scalar) on /api/docs/.
// Runs before the bundle: zod inside it probes for eval unless told not to,
// and HQ's policy refuses eval.
globalThis.__zod_globalConfig = { jitless: true };

// Every code-sample target the bundle ships, named so all but curl can be
// hidden: the option is a list of what to hide, with no list of what to show.
// HQ's own clients (the generated CLI and TypeScript client) are not among
// them, and curl is the one sample every reader can run.
const HIDDEN_CLIENTS = {
  shell: ["httpie", "wget"],
  ...Object.fromEntries(
    [
      "c", "clojure", "csharp", "dart", "fsharp", "go", "http", "java", "js", "julia", "kotlin",
      "node", "objc", "ocaml", "php", "powershell", "python", "r", "ruby", "rust", "swift",
    ].map((target) => [target, true]),
  ),
};

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("api-reference-root");
  // Scalar pins its sidebar and section bars to the top of the viewport unless
  // it is told what already stands there. HQ's header is named to it, and its
  // height is handed over as it is and whenever it changes.
  const header = document.querySelector(".site-header");
  if (header) {
    header.setAttribute("data-scalar-scroll-header", "");
    const offset = () => root.style.setProperty("--scalar-custom-header-height", `${header.offsetHeight}px`);
    offset();
    new ResizeObserver(offset).observe(header);
  }
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
    // Colour, type and spacing are HQ's tokens (static/css/api-reference.css).
    theme: "none",
    hideDarkModeToggle: true,
    ...(theme ? { forceDarkModeState: theme } : {}),
    // The schemas are shown where an operation uses them.
    hideModels: true,
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
    localization: { translations: { search: { label: "Search the reference" } } },
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
