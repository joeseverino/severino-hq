// `hq/browser-floor`: every property, keyword value, function, at-rule and
// pseudo-class or pseudo-element the stylesheet writes is one each engine's
// floor release has (browser-floor.mjs), by browser-compat-data. What sits
// inside an `@supports` block is left to that block: it is the platform's way
// to write ahead of an engine, so a feature newer than the floor goes there.
// A property written in a rule beside its prefixed form is judged as the pair,
// each engine reading the one it has. A feature the config's `degrades` names
// is one an engine below it goes without, leaving the fallback written beside
// it; it is declared once there, never at a site. A name browser-compat-data
// does not hold is not judged.

import stylelint from "stylelint";

import { bcd, belowFloor, compatAt, ENGINES, FLOOR, PREFIX } from "./browser-floor.mjs";

const {
  createPlugin,
  utils: { report, ruleMessages, validateOptions },
} = stylelint;

const ruleName = "hq/browser-floor";

const floorWords = ENGINES.map((engine) => `${engine} ${FLOOR[engine]}`).join(", ");

const messages = ruleMessages(ruleName, {
  rejected: (written, path, engines) =>
    `"${written}" (${path}) is newer than the browser floor in ${engines.join(", ")} (floor: ${floorWords}): write it inside an @supports block, or name it in degrades with the fallback an engine without it draws (mise run audit:bcd ${path})`,
});

// Each function browser-compat-data holds under css.types, by its name: the
// deepest entry of that name, so `rgb(` reads css.types.color.rgb.
const functions = new Map();
const index = (node, path) => {
  for (const [key, child] of Object.entries(node)) {
    if (key.startsWith("__") || typeof child !== "object") continue;
    const at = `${path}.${key}`;
    if (child.__compat && (!functions.has(key) || at.split(".").length > functions.get(key).split(".").length)) {
      functions.set(key, at);
    }
    index(child, at);
  }
};
index(bcd.css.types, "css.types");

const insideSupports = (node) => {
  for (let parent = node.parent; parent; parent = parent.parent) {
    if (parent.type === "atrule" && parent.name.toLowerCase() === "supports") return true;
  }
  return false;
};

// A written name's browser-compat-data path: the name itself, or the name
// without its vendor prefix.
const located = (group, written) => {
  if (compatAt(`${group}.${written}`)) return `${group}.${written}`;
  const bare = written.replace(PREFIX, "");
  return bare !== written && compatAt(`${group}.${bare}`) ? `${group}.${bare}` : undefined;
};

// The value's words and function names outside strings and url().
const valueWords = (value) =>
  value
    .replace(/(["'])(?:\\.|(?!\1).)*\1/g, " ")
    .replace(/url\([^)]*\)/gi, " ")
    .match(/-?[a-z][\w-]*\(?/gi) ?? [];

const rule = (primary) => (root, result) => {
  const declared = (options) =>
    typeof options === "object" && Object.values(options.degrades ?? {}).every((fallback) => typeof fallback === "string");
  if (!validateOptions(result, ruleName, { actual: primary, possible: declared })) return;
  const degrades = primary.degrades ?? {};
  const judge = (node, written, path, engines = path ? belowFloor(path, written) : []) => {
    if (engines.length && !(path in degrades)) {
      report({ message: messages.rejected, messageArgs: [written, path, engines], node, result, ruleName, word: written });
    }
    return engines.length === 0;
  };
  const judgeDeclarations = (owner) => {
    const declarations = (owner.nodes ?? []).filter((node) => node.type === "decl" && !node.prop.startsWith("--"));
    // Each property's forms in this block: an engine lacks it only when it
    // lacks every form written.
    const forms = new Map();
    for (const decl of declarations) {
      const path = located("css.properties", decl.prop.toLowerCase());
      if (path) forms.set(path, [...(forms.get(path) ?? []), decl]);
    }
    for (const [path, written] of forms) {
      const lacking = written.map((decl) => belowFloor(path, decl.prop.toLowerCase()));
      const engines = lacking[0].filter((engine) => lacking.every((each) => each.includes(engine)));
      if (!judge(written[0], written[0].prop, path, engines)) continue;
      for (const decl of written.filter((each) => !PREFIX.test(each.prop))) {
        for (const word of new Set(valueWords(decl.value))) {
          if (word.endsWith("(")) {
            judge(decl, word, functions.get(word.slice(0, -1).toLowerCase()));
          } else if (compatAt(`${path}.${word.toLowerCase()}`)) {
            // An engine that reads only the prefixed form never reads this value.
            const value = `${path}.${word.toLowerCase()}`;
            const unread = belowFloor(path, decl.prop.toLowerCase());
            judge(decl, `${decl.prop}: ${word}`, value, belowFloor(value).filter((engine) => !unread.includes(engine)));
          }
        }
      }
    }
  };
  judgeDeclarations(root);
  root.walk((node) => {
    if (insideSupports(node) || node.type === "decl" || node.type === "comment") return;
    if (node.type === "atrule") {
      const name = node.name.toLowerCase();
      if (name === "supports") return;
      judge(node, `@${name}`, located("css.at-rules", name));
    } else {
      for (const [, written] of node.selector.matchAll(/::?(-?[a-z][\w-]*)/gi)) {
        judge(node, written, located("css.selectors", written.toLowerCase()));
      }
    }
    judgeDeclarations(node);
  });
};

rule.ruleName = ruleName;
rule.messages = messages;

export default createPlugin(ruleName, rule);
