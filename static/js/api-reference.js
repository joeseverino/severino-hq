// Mounts the vendored Scalar bundle (static/vendor/scalar) on /api/docs/.
// Runs before the bundle: zod inside it probes for eval unless told not to,
// and HQ's policy refuses eval.
globalThis.__zod_globalConfig = { jitless: true };

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
  window.Scalar.createApiReference(root, {
    url: root.dataset.url,
    // Everything Scalar would fetch from its own servers: fonts, telemetry,
    // its hosted agent and MCP generator, the toolbar that links to them.
    withDefaultFonts: false,
    telemetry: false,
    showDeveloperTools: "never",
    agent: { disabled: true },
    mcp: { disabled: true },
  });
});
