"use strict";

// Round trip, actually measured rather than reported. Everything else in that
// panel is read from the last sweep and says so; this one is taken now, from
// the browser reading it, which is the only place the number means anything.
// It keeps sampling while the panel is open, because a peering is a live thing
// and a single figure printed once reads like a stored one.
const hqRoundTrip = (() => {
  let stopSampling = null;

  const sample = (endpoint) => {
    const started = performance.now();
    // A response with no body and no database behind it, so what is measured
    // is the path rather than what HQ did after arriving.
    return hqFetch(`${endpoint}?t=${started}`, {
      cache: "no-store",
      credentials: "same-origin",
    }).then(() => performance.now() - started);
  };

  const render = (slot, runs) => {
    const best = Math.min(...runs);
    const worst = Math.max(...runs);
    let live = slot.querySelector(".conn-live");
    if (!live) {
      live = document.createElement("span");
      live.className = "conn-live";
      const pulse = document.createElement("span");
      pulse.className = "conn-pulse";
      const value = document.createElement("strong");
      const samples = document.createElement("span");
      samples.className = "conn-samples";
      live.append(pulse, value, samples);
      slot.replaceChildren(live);
    }
    live.querySelector("strong").textContent = `${best.toFixed(1)} ms`;
    const samples = live.querySelector(".conn-samples");
    // Each bar relative to the slowest sample, so the shape shows variation
    // rather than an absolute scale nobody can read at this size.
    samples.replaceChildren(
      ...runs.map((run) => {
        const bar = document.createElement("i");
        bar.style.setProperty("--at", `${Math.max(12, (run / worst) * 100)}`);
        return bar;
      }),
    );
  };

  // The figures describe the recent path, not the whole visit.
  const WINDOW = 12;

  const start = (slot, endpoint) => {
    const runs = [];
    // Sampled by the shared loop, which leaves a hidden tab alone: nobody is
    // reading the figure there.
    const tick = () => {
      return sample(endpoint)
        .then((run) => {
          runs.push(run);
          if (runs.length > WINDOW) runs.splice(0, runs.length - WINDOW);
          render(slot, runs);
        })
        .catch(() => {
          slot.textContent = "could not measure";
          stop();
        });
    };
    stop();
    tick();
    stopSampling = hqEvery(tick, { ms: 3000 });
  };

  const stop = () => {
    stopSampling?.();
    stopSampling = null;
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

// The panel a dialog fetched when it opened is measured the same way.
document.addEventListener("hq:fragment", (event) => {
  const panel = event.target.closest("[data-connection-panel]");
  if (!panel || panel !== event.target) return;
  hqWatchRoundTrip(panel);
  hqShowResponseHeaders(panel);
});

document.getElementById("modal-connection")?.addEventListener("close", () => {
  hqRoundTrip.stop();
});

// What HQ sent back, taken from a real response rather than from the settings
// meant to produce one. A same-origin fetch exposes every response header, so
// the browser reading the panel is the honest place to ask what it was given.
const HQ_RESPONSE_HEADERS = [
  ["x-served-by", "Which reverse-proxy hostname actually served this response"],
  ["x-request-id", "Joins this browser response to HQ's structured request log"],
  ["content-security-policy", "Which origins may load script, style and frames"],
  ["strict-transport-security", "Refuses plain HTTP for this host from now on"],
  ["x-content-type-options", "Stops the browser guessing a type it was not sent"],
  ["x-frame-options", "Refuses to be framed by another site"],
  ["referrer-policy", "How much of this URL travels to anywhere you click"],
  ["cross-origin-opener-policy", "Keeps other origins out of this browsing context"],
  ["cross-origin-resource-policy", "Refuses to be loaded as a subresource elsewhere"],
  ["permissions-policy", "Which device capabilities this page may ask for"],
  ["reporting-endpoints", "Where the browser sends a policy it refused to follow"],
];

// The directives worth calling out by name, because the policy is one long
// header and the interesting parts of it are the two that stop a class of bug
// rather than naming an origin. Read from the policy the browser was actually
// sent, so a directive dropped in configuration shows as absent here.
const HQ_POLICY_DIRECTIVES = [
  ["require-trusted-types-for", "Assigning a string to a DOM sink throws instead of parsing"],
  ["frame-ancestors 'none'", "Nothing may frame this page"],
  ["object-src 'none'", "No plugins or embedded objects"],
  ["base-uri 'self'", "Injected markup cannot re-point every relative URL"],
  ["form-action 'self'", "A form cannot be made to submit somewhere else"],
];

// One muted line in a connection-style row list.
const hqNoteRow = (text) => {
  const row = document.createElement("div");
  row.className = "conn-row";
  const note = document.createElement("span");
  note.className = "conn-row-note";
  note.textContent = text;
  row.append(note);
  return row;
};

const hqShowResponseHeaders = (root) => {
  const slot = root.querySelector("[data-response-headers]");
  if (!slot || !window.fetch) return;
  const disclosure = slot.closest("[data-connection-protocol]");
  if (disclosure && !disclosure.open) return;
  if (slot.dataset.loaded === "true") return;
  slot.dataset.loaded = "true";
  const hqEvidenceRow = (label, present, purpose, chipText) => {
    const row = document.createElement("div");
    row.className = "conn-row";
    const shown = document.createElement("code");
    shown.textContent = label;
    const chip = document.createElement("span");
    chip.className = `conn-kind ${present ? "conn-kind-read" : "conn-kind-elsewhere"}`;
    chip.textContent = chipText;
    const note = document.createElement("span");
    note.className = "conn-row-note";
    note.textContent = purpose;
    row.append(shown, chip, note);
    return row;
  };

  hqFetch(window.location.href, {
    credentials: "same-origin",
    cache: "no-store",
  })
    .then((response) => {
      const rows = HQ_RESPONSE_HEADERS.map(([name, purpose]) => {
        const value = response.headers.get(name);
        return hqEvidenceRow(
          value
            ? `${name}: ${value.length > 76 ? `${value.slice(0, 76)}…` : value}`
            : name,
          value,
          purpose,
          value ? "sent" : "absent",
        );
      });
      // The policy is one header long enough to be truncated above, and its
      // most consequential directives are the ones a reader would never spot
      // in it. Named individually, and read back from the same response, so
      // "the policy says so" is checkable rather than asserted.
      const policy = response.headers.get("content-security-policy") || "";
      for (const [directive, purpose] of HQ_POLICY_DIRECTIVES) {
        const present = policy.includes(directive);
        rows.push(
          hqEvidenceRow(directive, present, purpose, present ? "enforced" : "absent"),
        );
      }
      slot.replaceChildren(...rows);
    })
    .catch(() => {
      slot.replaceChildren(hqNoteRow("The response could not be read back."));
    });
};

document.addEventListener("DOMContentLoaded", () => {
  const panel = document.querySelector("[data-connection-panel]");
  if (panel && !panel.closest("dialog")) hqShowResponseHeaders(panel);
});

document.addEventListener("toggle", (event) => {
  const disclosure = event.target;
  if (!disclosure.matches?.("[data-connection-protocol]") || !disclosure.open) return;
  const panel = disclosure.closest("[data-connection-panel]");
  if (panel) hqShowResponseHeaders(panel);
}, true);
