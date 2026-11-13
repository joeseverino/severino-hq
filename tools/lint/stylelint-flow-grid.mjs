// `hq/flow-grid`: a run of sibling sections or cards is drawn by the flow
// (`.section-flow`, docs/Design.md "Sibling sections flow"), so an auto-fit
// or auto-fill grid is refused anywhere but the parts named in `allowed`,
// which lay out something else inside themselves. A rule nested in an allowed
// one is that part's, so the allowed selector is read off the rule and every
// rule it sits in.

import stylelint from "stylelint";

const {
  createPlugin,
  utils: { report, ruleMessages, validateOptions },
} = stylelint;

const ruleName = "hq/flow-grid";

const messages = ruleMessages(ruleName, {
  rejected: (selector) =>
    `"${selector}" lays out its own auto-fit grid: a run of sibling sections or cards is the flow (\`.section-flow\`, docs/Design.md); a part that lays out something else inside itself is named in \`flowGrids\` in stylelint.config.mjs with its reason`,
});

const AUTO = /\brepeat\(\s*auto-(fit|fill)\b/i;
const spelled = (selector) => selector.replace(/\s+/g, " ").trim();

const rule = (primary) => (root, result) => {
  if (!validateOptions(result, ruleName, { actual: primary, possible: { allowed: [(value) => typeof value === "string"] } })) return;
  const allowed = new Set(primary.allowed.map(spelled));
  root.walkDecls((decl) => {
    if (!AUTO.test(decl.value)) return;
    const owners = [];
    for (let node = decl.parent; node; node = node.parent) if (node.type === "rule") owners.push(spelled(node.selector));
    if (owners.some((selector) => allowed.has(selector))) return;
    report({ message: messages.rejected, messageArgs: [owners[0] ?? decl.prop], node: decl, result, ruleName });
  });
};

rule.ruleName = ruleName;
rule.messages = messages;

export default createPlugin(ruleName, rule);
