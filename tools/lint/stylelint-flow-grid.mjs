// `hq/flow-grid`: a run of sibling sections or cards is drawn by the flow
// (`.section-flow`, docs/Design.md "Sibling sections flow"), so an auto-fit
// or auto-fill grid is refused. A part that lays out something else inside
// itself (a form's fields, a band's cells) says so in a disable comment beside
// its declaration.

import stylelint from "stylelint";

const {
  createPlugin,
  utils: { report, ruleMessages, validateOptions },
} = stylelint;

const ruleName = "hq/flow-grid";

const messages = ruleMessages(ruleName, {
  rejected: (selector) =>
    `"${selector}" lays out its own auto-fit grid: a run of sibling sections or cards is the flow (\`.section-flow\`, docs/Design.md); a part that lays out something else inside itself says so in a disable comment`,
});

const AUTO = /\brepeat\(\s*auto-(fit|fill)\b/i;

const rule = (primary) => (root, result) => {
  if (!validateOptions(result, ruleName, { actual: primary, possible: [true] })) return;
  root.walkDecls((decl) => {
    if (!AUTO.test(decl.value)) return;
    const owner = decl.parent?.type === "rule" ? decl.parent.selector.replace(/\s+/g, " ").trim() : decl.prop;
    report({ message: messages.rejected, messageArgs: [owner], node: decl, result, ruleName });
  });
};

rule.ruleName = ruleName;
rule.messages = messages;

export default createPlugin(ruleName, rule);
