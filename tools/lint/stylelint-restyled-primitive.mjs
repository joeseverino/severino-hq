// `hq/restyled-primitive`: a primitive is styled in the sheets that draw it,
// never restyled for one page by a selector that names it beside the page's
// class. It is `selector-disallowed-list` reading what that rule does not: a
// nested rule as the selector it resolves to (`.page { .btn {} }` is
// `.page .btn`).

import stylelint from "stylelint";

const {
  createPlugin,
  utils: { report, ruleMessages, validateOptions },
} = stylelint;

const ruleName = "hq/restyled-primitive";

const messages = ruleMessages(ruleName, {
  rejected: (selector, primitive, sheets) =>
    `"${selector}" restyles the ${primitive} primitive from a page's class: grow it in ${sheets} (a variant docs/Design.md names) and draw the page with it`,
});

// What a selector names inside `:not()` is what the element is not.
const notInside = String.raw`(?<!:not\((?:[^()]|\([^()]*\))*)`;
const classes = (names) => String.raw`\.(?:${names})(?![-\w])`;

// A nested rule's selectors as the nesting spec resolves them: `&` is the
// parent's, and a selector without one is a descendant of it.
const resolved = (rule, cache) => {
  if (cache.has(rule)) return cache.get(rule);
  let parent = rule.parent;
  while (parent && parent.type !== "rule") parent = parent.parent;
  const outer = parent && `:is(${resolved(parent, cache).join(", ")})`;
  const selectors = parent
    ? rule.selectors.map((selector) => (selector.includes("&") ? selector.replaceAll("&", outer) : `${outer} ${selector}`))
    : rule.selectors;
  cache.set(rule, selectors);
  return selectors;
};

const inKeyframes = (rule) => {
  for (let node = rule.parent; node; node = node.parent) {
    if (node.type === "atrule" && /keyframes$/i.test(node.name)) return true;
  }
  return false;
};

const rule = (primary) => (root, result) => {
  const valid = validateOptions(result, ruleName, {
    actual: primary,
    possible: {
      primitives: [(each) => typeof each?.name === "string" && typeof each?.classes === "string" && Array.isArray(each?.sheets)],
      sheet: [(value) => typeof value === "string"],
    },
  });
  if (!valid) return;
  const { primitives, sheet = "" } = primary;
  // Any class but a primitive's own and a state (`is-*`, `has-*`) is a page's.
  const vocabulary = [...primitives.map((each) => each.classes), String.raw`is-[\w-]+`, String.raw`has-[\w-]+`].join("|");
  const pageClass = String.raw`${notInside}\.(?!(?:${vocabulary})(?![-\w]))[a-zA-Z_][\w-]*`;
  const checked = primitives
    .filter((each) => !each.sheets.includes(sheet))
    .map((each) => ({
      ...each,
      pattern: new RegExp(String.raw`^(?=[\s\S]*${notInside}${each.names ?? classes(each.classes)})[\s\S]*${pageClass}`),
    }));
  const cache = new Map();
  root.walkRules((owner) => {
    if (inKeyframes(owner)) return;
    const written = owner.selectors;
    resolved(owner, cache).forEach((selector, index) => {
      const primitive = checked.find((each) => each.pattern.test(selector));
      if (!primitive) return;
      const sheets = primitive.sheets.map((each) => `${each}.css`).join(" or ");
      const word = written[index];
      report({ message: messages.rejected, messageArgs: [word, primitive.name, sheets], node: owner, word, result, ruleName });
    });
  });
};

rule.ruleName = ruleName;
rule.messages = messages;

export default createPlugin(ruleName, rule);
