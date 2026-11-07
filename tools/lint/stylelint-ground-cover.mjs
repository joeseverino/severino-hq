// `hq/ground-cover`: a box that covers what scrolls beneath it (a sticky
// head or bar, or a fade from transparent) paints `var(--ground)`, the color
// of the surface it sits on, which a frame sets for what it holds and the
// page for the rest. Painted the page's `--bg` or the frame's `--surface` by
// name, it is a band of the wrong color wherever it lands on the other.

import stylelint from "stylelint";

const {
  createPlugin,
  utils: { report, ruleMessages, validateOptions },
} = stylelint;

const ruleName = "hq/ground-cover";

const messages = ruleMessages(ruleName, {
  rejected: (selector) =>
    `"${selector}" covers what scrolls beneath it in a fixed color: paint var(--ground), the surface it sits on`,
});

const GROUND = /var\(\s*--(bg|surface)\s*[,)]/;
const FADE = /gradient\([^;]*\btransparent\b/;

const rule = (primary) => (root, result) => {
  if (!validateOptions(result, ruleName, { actual: primary, possible: [true] })) return;
  root.walkRules((owner) => {
    const own = owner.nodes.filter((node) => node.type === "decl");
    const sticky = own.some((decl) => decl.prop.toLowerCase() === "position" && /\bsticky\b/i.test(decl.value));
    for (const decl of own) {
      if (!/^background(-color)?$/i.test(decl.prop) || !GROUND.test(decl.value)) continue;
      if (sticky || FADE.test(decl.value)) {
        report({ message: messages.rejected, messageArgs: [owner.selector], node: decl, result, ruleName });
      }
    }
  });
};

rule.ruleName = ruleName;
rule.messages = messages;

export default createPlugin(ruleName, rule);
