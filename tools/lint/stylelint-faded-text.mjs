// `hq/faded-text`: an opacity under 1 is for what WCAG 1.4.3 does not hold
// to 4.5:1, an inactive control, and for marks that are not text. Text a
// reader can still use takes an ink token that clears 4.5:1 in both palettes,
// since a fade lowers its contrast below what its token was chosen for.
// Stylelint's own rule-selector-property-disallowed-list matches a whole
// selector list by one pattern and cannot read the value, so this reads each
// selector of a list and the value it fades to.
//
// Options: `inactive`, patterns of which one anywhere in a selector says it
// is an inactive state (on the element or a container); `marks`, patterns
// a selector that styles a non-text mark matches; `animations`, the
// @keyframes names that fade a mark.

import stylelint from "stylelint";

const {
  createPlugin,
  utils: { report, ruleMessages, validateOptions },
} = stylelint;

const ruleName = "hq/faded-text";

const messages = ruleMessages(ruleName, {
  rejected: (selector) =>
    `"${selector}" fades by opacity, which is for an inactive control or a mark: give text an ink token that clears 4.5:1`,
  animation: (name) =>
    `@keyframes ${name} fades by opacity, which is for an inactive control or a mark: name it in animations if it moves a mark`,
});

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

const rule = (primary, options) => (root, result) => {
  const valid = validateOptions(
    result,
    ruleName,
    { actual: primary, possible: [true] },
    {
      actual: options,
      possible: { inactive: [(v) => typeof v === "string"], marks: [(v) => typeof v === "string"], animations: [(v) => typeof v === "string"] },
    },
  );
  if (!valid) return;
  const inactive = (options.inactive ?? []).map((pattern) => new RegExp(pattern));
  const marks = (options.marks ?? []).map((pattern) => new RegExp(pattern));
  const animations = new Set(options.animations ?? []);

  root.walkDecls(/^opacity$/i, (decl) => {
    if (!fades(decl.value)) return;
    const owner = decl.parent;
    if (owner?.type !== "rule") return;
    const frames = owner.parent;
    if (frames?.type === "atrule" && /keyframes$/i.test(frames.name)) {
      if (!animations.has(frames.params.trim())) {
        report({ message: messages.animation, messageArgs: [frames.params.trim()], node: decl, result, ruleName });
      }
      return;
    }
    for (const selector of selectors(owner.selector)) {
      if (inactive.some((pattern) => pattern.test(selector))) continue;
      if (marks.some((pattern) => pattern.test(selector))) continue;
      report({ message: messages.rejected, messageArgs: [selector], node: decl, result, ruleName });
    }
  });
};

rule.ruleName = ruleName;
rule.messages = messages;

export default createPlugin(ruleName, rule);
