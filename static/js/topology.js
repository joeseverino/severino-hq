"use strict";

// The topology is useful HTML before this runs: every card's select control
// is a link that focuses the node, and a focused page draws its details. This
// enhancement filters, isolates a node's immediate neighborhood, and loads
// the selected node's details into the one panel beside the lanes, creating
// no client-side topology state of its own.
document.querySelectorAll("[data-topology]").forEach((workspace) => {
  const nodes = [...workspace.querySelectorAll("[data-topology-node]")];
  const lanes = [...workspace.querySelectorAll("[data-topology-lane]")];
  const map = workspace.querySelector(".topology-map");
  const stage = workspace.querySelector("[data-topology-stage]");
  const detail = workspace.querySelector("[data-topology-detail]");
  const ledger = document.getElementById(workspace.dataset.topologyLedger);
  const edges = [...(ledger?.querySelectorAll("[data-topology-edge]") || [])];
  const search = workspace.querySelector("[data-topology-search]");
  const kindControls = [...workspace.querySelectorAll("[data-topology-kind]")];
  const status = workspace.querySelector("[data-topology-status]");
  const reset = workspace.querySelector("[data-topology-reset]");
  // The total counts every card, including those hidden until asked for.
  const total = Number(workspace.dataset.topologyTotal) || nodes.length;
  let focused = nodes.some((node) => node.dataset.topologyNode === workspace.dataset.focus)
    ? workspace.dataset.focus : "";

  const rememberFocus = (nodeId) => {
    const url = new URL(window.location.href);
    if (nodeId) url.searchParams.set("focus", nodeId);
    else {
      url.searchParams.delete("focus");
      url.searchParams.delete("direction");
      url.searchParams.delete("depth");
    }
    window.history.replaceState({}, "", url);
  };

  // Whether the toolbar alone would show this node, ignoring any focus. Asked
  // before rendering, so a focus the filter hides is dropped before painting.
  const passesToolbar = (node) => {
    const terms = (search?.value || "").trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const shownKinds = new Set(kindControls.filter((control) => control.checked).map((control) => control.value));
    return shownKinds.has(node.dataset.topologyNodeKind)
      && terms.every((term) => node.dataset.topologySearchText.toLocaleLowerCase().includes(term));
  };

  const showDetail = (shown) => {
    if (!detail) return;
    detail.hidden = !shown;
    stage?.classList.toggle("has-detail", shown);
  };

  // The selected node's body, from the same view a focused page renders.
  const loadDetail = async (node) => {
    if (!detail) return;
    if (!node) {
      detail.dataset.topologyDetail = "";
      detail.replaceChildren();
      showDetail(false);
      return;
    }
    if (detail.dataset.topologyDetail === node.dataset.topologyNode && detail.childElementCount) {
      showDetail(true);
      return;
    }
    detail.dataset.topologyDetail = node.dataset.topologyNode;
    showDetail(true);
    try {
      // The region takes its newest question only, so quick selections never
      // interleave.
      await hqFragment.swap(detail, { url: node.dataset.topologyBody, inner: true, renew: false });
    } catch (error) {
      if (error.name === "AbortError") return;
      hqFragment.fail(detail, {
        ...error,
        link: { href: node.querySelector("[data-topology-select]")?.href || "#map", label: "Open details" },
      });
    }
  };

  // `said`: whether the status line speaks for the view. The server draws it
  // for the page as it loads, so the first pass leaves it standing.
  const render = ({ said = true } = {}) => {
    const selected = nodes.find((node) => node.dataset.topologyNode === focused);
    const related = new Set(selected?.dataset.topologyNeighbors.split(" ").filter(Boolean) || []);
    workspace.classList.toggle("has-focus", Boolean(selected));
    if (reset) reset.hidden = !selected;
    nodes.forEach((node) => {
      const id = node.dataset.topologyNode;
      const matchesFilter = passesToolbar(node);
      const inNeighborhood = !selected || id === focused || related.has(id);
      node.hidden = !matchesFilter || !inNeighborhood;
      node.classList.toggle("is-selected", id === focused);
      node.classList.toggle("is-related", related.has(id));
      const select = node.querySelector("[data-topology-select]");
      if (id === focused) select?.setAttribute("aria-current", "true");
      else select?.removeAttribute("aria-current");
    });
    lanes.forEach((lane) => {
      const visible = [...lane.querySelectorAll("[data-topology-node]")]
        .filter((node) => !node.hidden);
      lane.hidden = visible.length === 0;
      const count = lane.querySelector("[data-topology-count]");
      if (count) {
        count.textContent = visible.length;
        const label = `${visible.length} shown`;
        count.title = label;
        count.setAttribute("aria-label", label);
      }
    });
    edges.forEach((edge) => {
      const ends = edge.dataset.topologyEdge.split(" ");
      const touchesFocus = Boolean(selected) && ends.includes(focused);
      edge.hidden = Boolean(selected) && !touchesFocus;
      edge.classList.toggle("is-related", touchesFocus);
    });
    if (ledger) ledger.hidden = edges.length > 0 && edges.every((edge) => edge.hidden);
    if (status && said) {
      const visible = nodes.filter((node) => !node.hidden).length;
      status.textContent = selected
        ? `${selected.querySelector("strong")?.textContent || "Selected"}: ${related.size} direct relationship${related.size === 1 ? "" : "s"}.`
        : `${visible} of ${total} shown. Press a count of links to see only what it connects to.`;
    }
  };

  // Isolating a node shrinks the lanes under whatever the page was scrolled
  // to, so the selection is brought back into view.
  const reveal = () => {
    const selected = nodes.find((node) => node.dataset.topologyNode === focused);
    if (!map || !selected) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    selected.scrollIntoView({
      behavior: reduce ? "auto" : "smooth",
      block: "nearest",
      inline: "center",
    });
  };

  const focus = (nodeId, remember = true) => {
    focused = nodes.some((node) => node.dataset.topologyNode === nodeId) ? nodeId : "";
    render();
    if (remember) rememberFocus(focused);
    loadDetail(nodes.find((node) => node.dataset.topologyNode === focused));
    if (focused && remember) reveal();
  };

  const filter = () => {
    // Decide, then draw, once. A focus the toolbar has just excluded is
    // dropped before anything is painted.
    const selected = nodes.find((node) => node.dataset.topologyNode === focused);
    if (selected && !passesToolbar(selected)) {
      focused = "";
      rememberFocus("");
      loadDetail(null);
    }
    render();
  };

  // A card selects its node: the select link, or a click anywhere on the
  // card that is not another link. Selecting the selected node clears it.
  // Middle and modified clicks on a link are left to the browser.
  workspace.addEventListener("click", (event) => {
    const node = event.target.closest?.("[data-topology-node]");
    if (!node) return;
    const link = event.target.closest("a[href]");
    if (link && !link.matches("[data-topology-select]")) return;
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const id = node.dataset.topologyNode;
    focus(focused === id ? "" : id);
  });

  search?.addEventListener("input", filter);
  kindControls.forEach((control) => control.addEventListener("change", filter));
  reset?.addEventListener("click", () => focus(""));
  search?.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      search.value = "";
      filter();
    } else if (event.key === "Enter") {
      const first = nodes.find((node) => !node.hidden);
      if (first) {
        focus(first.dataset.topologyNode);
        first.querySelector("[data-topology-select]")?.focus();
      }
    }
  });
  render({ said: false });
  if (focused) {
    showDetail(Boolean(detail?.childElementCount));
    map?.scrollTo({ top: 0, behavior: "auto" });
  }
});
