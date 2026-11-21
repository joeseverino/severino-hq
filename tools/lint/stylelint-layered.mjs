// `hq/layered`: every rule is in an `@layer` block, since an unlayered rule
// beats every layer at any specificity. Core's rule-nesting-at-rule-required-list
// cannot fix what it finds; this moves each stray block (the rule, or the
// at-rule holding it) into the sheet's `@layer` block before it, else the next.

import stylelint from "stylelint";

const {
  createPlugin,
  utils: { report, ruleMessages, validateOptions },
} = stylelint;

const ruleName = "hq/layered";
const messages = ruleMessages(ruleName, {
  expected: (selector) => `"${selector}" is outside every @layer block: mise run fix moves it into the one before it`,
});

const isLayer = (node) => node.type === "atrule" && node.name.toLowerCase() === "layer" && node.nodes !== undefined;

const rule = (primary) => (root, result) => {
  if (!validateOptions(result, ruleName, { actual: primary, possible: [true] })) return;
  const fixable = root.nodes.some(isLayer);
  root.walkRules((owner) => {
    let top = owner;
    for (; top.parent.type !== "root"; top = top.parent) if (isLayer(top.parent)) return;
    const fix = () => {
      // Moved already, with an earlier rule the same block holds.
      if (top.parent?.type !== "root") return;
      const at = root.index(top);
      const before = root.nodes.slice(0, at).findLast(isLayer);
      if (before) before.append(top);
      else root.nodes.slice(at + 1).find(isLayer).prepend(top);
    };
    report({ message: messages.expected, messageArgs: [owner.selector], node: owner, result, ruleName, ...(fixable && { fix }) });
  });
};

rule.ruleName = ruleName;
rule.messages = messages;
rule.meta = { fixable: true };

export default createPlugin(ruleName, rule);
