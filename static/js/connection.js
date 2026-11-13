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
// The server draws each row it asks about (request_headers.RESPONSE_CHECKS);
// this fills in the value and shows the state the response holds.
const hqShowResponseHeaders = (root) => {
  const table = root.querySelector("[data-response-headers]");
  if (!table) return;
  const disclosure = table.closest("[data-connection-protocol]");
  if (disclosure && !disclosure.open) return;
  if (table.dataset.loaded === "true") return;
  table.dataset.loaded = "true";
  const rows = [...table.querySelectorAll("[data-response-check]")];
  const show = (row, state) => {
    for (const part of row.querySelectorAll("[data-response-state]")) {
      part.hidden = part.dataset.responseState !== state;
    }
  };

  hqFetch(window.location.href, {
    credentials: "same-origin",
    cache: "no-store",
  })
    .then((response) => {
      // A directive is read from the policy the browser was actually sent.
      const policy = response.headers.get("content-security-policy") || "";
      for (const row of rows) {
        const name = row.dataset.responseCheck;
        if ("responseDirective" in row.dataset) {
          show(row, policy.includes(name) ? "present" : "absent");
          continue;
        }
        const value = response.headers.get(name);
        if (value) {
          row.querySelector("[data-response-value]").textContent =
            `${name}: ${value.length > 76 ? `${value.slice(0, 76)}…` : value}`;
        }
        show(row, value ? "present" : "absent");
      }
    })
    .catch(() => {
      for (const row of rows) show(row, "");
      const failure = table.closest(".section-body")?.querySelector("[data-response-failure]");
      if (failure) failure.hidden = false;
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
