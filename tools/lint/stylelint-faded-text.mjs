// `hq/faded-text`: an opacity under 1 is for what WCAG 1.4.3 does not hold
// to 4.5:1, an inactive control, and for marks that are not text. Text a
// reader can still use takes an ink token that clears 4.5:1 in both palettes,
// since a fade lowers its contrast below what its token was chosen for.
// Stylelint's own rule-selector-property-disallowed-list matches a whole
// selector list by one pattern and cannot read the value, so this reads each
// selector of a list and the value it fades to. Any other mark a rule fades
// (a chart's trend line, a calendar's dot, an animation's pulse) says so in a
// disable comment beside its declaration.

import stylelint from "stylelint";

const {
  createPlugin,
  utils: { report, ruleMessages, validateOptions },
} = stylelint;

const ruleName = "hq/faded-text";

const messages = ruleMessages(ruleName, {
  rejected: (selector) =>
    `"${selector}" fades by opacity, which is for an inactive control or a mark: give text an ink token that clears 4.5:1, or say in a disable comment what mark it fades`,
  animation: (name) =>
    `@keyframes ${name} fades by opacity, which is for an inactive control or a mark: give text an ink token that clears 4.5:1, or say in a disable comment what mark it moves`,
});

// A state is inactive on the element or on what holds it: a disabled or
// aria-disabled control, and a region aria-busy while its answer is on the
// way.
const INACTIVE = [/(?<!:not\()(:disabled|\[aria-disabled="true"\])/, /\[aria-busy="true"\]/];
// The rest of a compound: classes, attributes and pseudo-classes, each
// argument whole, so a name inside `:is()` is not the compound's.
const compound = String.raw`(?:::?[\w-]+(?:\([^()]*\))?|\.[\w-]+|\[[^\]]*\])*`;
// A shape a chart draws is a mark, whatever its classes.
const SHAPE = new RegExp(String.raw`(^|[\s>+~])(rect|path|circle|line|polyline)${compound}$`);

// The selectors of a list, split at its top-level commas, so `:is(a, b)`
// stays one.
function selectors(list) {
  const parts = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < list.length; i++) {
    const c = list[i];
    if (c === "(" || c === "[") depth++;
    else if (c === ")" || c === "]") depth--;
    else if (c === "," && depth === 0) {
      parts.push(list.slice(start, i).trim());
      start = i + 1;
    }
  }
  parts.push(list.slice(start).trim());
  return parts;
}

// Whether a value fades: anything but a number of 0 (not drawn) or 1 and up
// (not faded), so a value read from a token is checked too.
function fades(value) {
  const number = /^\s*([\d.]+)(%?)\s*$/.exec(value);
  if (!number) return !/^\s*(inherit|initial|unset|revert|revert-layer)\s*$/i.test(value);
  const amount = parseFloat(number[1]) / (number[2] ? 100 : 1);
  return amount > 0 && amount < 1;
}

const rule = (primary) => (root, result) => {
  if (!validateOptions(result, ruleName, { actual: primary, possible: [true] })) return;

  root.walkDecls(/^opacity$/i, (decl) => {
    if (!fades(decl.value)) return;
    const owner = decl.parent;
    if (owner?.type !== "rule") return;
    const frames = owner.parent;
    if (frames?.type === "atrule" && /keyframes$/i.test(frames.name)) {
      report({ message: messages.animation, messageArgs: [frames.params.trim()], node: decl, result, ruleName });
      return;
    }
    for (const selector of selectors(owner.selector)) {
      if (INACTIVE.some((pattern) => pattern.test(selector)) || SHAPE.test(selector)) continue;
      report({ message: messages.rejected, messageArgs: [selector], node: decl, result, ruleName });
    }
  });
};

rule.ruleName = ruleName;
rule.messages = messages;

export default createPlugin(ruleName, rule);
