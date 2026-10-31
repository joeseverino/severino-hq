// The browser scripts' contract, held by `lint:js`. Paths are relative to the
// repository root, where the task runs (`--config` sets no base of its own).
// Each script is a classic script the page loads, so it may name only
// what the browser defines, what it declares itself, or what fragment.js
// shares with every page.

import js from "@eslint/js";
import noUnsanitized from "eslint-plugin-no-unsanitized";
import globals from "globals";

// What reads layout. tables.js reads it only where a disable comment says
// why, so a new read is refused and an allowance that no longer reads
// anything is reported as unused.
const layoutReads =
  "^(getBoundingClientRect|getClientRects|getComputedStyle|offset(Width|Height|Left|Top)|scroll(Width|Height)|client(Width|Height))$";

// fragment.js is the one fetch-and-swap: it crosses the session boundary,
// turns a response into markup through the page's one Trusted Types policy,
// and keeps the one timer that asks again. A second copy of any of these in
// another script is a second request path with its own focus, session and
// failure behavior, and a second markup sink the policy header cannot see.
// A read is a `data-fragment` region or `hqFragment.swap`, a poll is
// `hqEvery`, and a request is `hqFetch`.
const onlyThePrimitive = " is fragment.js's alone: a read is a `data-fragment` region or `hqFragment.swap`, a poll is `hqEvery`, a request is `hqFetch`.";

const primitiveGlobals = [
  { name: "fetch", message: `fetch${onlyThePrimitive}` },
  { name: "DOMParser", message: `Parsing markup${onlyThePrimitive}` },
  { name: "EventSource", message: `A stream of its own${onlyThePrimitive}` },
  { name: "XMLHttpRequest", message: `A request of its own${onlyThePrimitive}` },
  { name: "setInterval", message: `An interval of its own${onlyThePrimitive}` },
];

const primitiveProperties = [
  { object: "window", property: "fetch", message: `fetch${onlyThePrimitive}` },
  { object: "window", property: "DOMParser", message: `Parsing markup${onlyThePrimitive}` },
  { object: "window", property: "EventSource", message: `A stream of its own${onlyThePrimitive}` },
  { object: "window", property: "XMLHttpRequest", message: `A request of its own${onlyThePrimitive}` },
  { object: "window", property: "setInterval", message: `An interval of its own${onlyThePrimitive}` },
  { property: "parseFromString", message: `A Trusted Types sink${onlyThePrimitive}` },
  { property: "createPolicy", message: `A Trusted Types policy${onlyThePrimitive}` },
  { property: "insertAdjacentHTML", message: `Parsing markup${onlyThePrimitive}` },
  { property: "innerHTML", message: `Parsing markup${onlyThePrimitive}` },
];

const primitiveSyntax = [
  {
    selector: "CallExpression[arguments.length=0] > MemberExpression.callee[property.name='text']",
    message: `Reading a response body as text${onlyThePrimitive}`,
  },
  {
    selector: "CallExpression[arguments.length=0] > MemberExpression.callee[property.name='json']",
    message: `Reading a response body as data${onlyThePrimitive}`,
  },
  {
    selector: "AssignmentExpression > MemberExpression.left[property.name='outerHTML']",
    message: `Parsing markup${onlyThePrimitive}`,
  },
  {
    selector:
      "CallExpression:matches([callee.name='setTimeout'], [callee.property.name='setTimeout'])[arguments.0.name='resolve']",
    message: `Sleeping in a loop${onlyThePrimitive}`,
  },
];

// In fragment.js each of these happens once, where a disable comment says
// so; a second one is refused, and a comment left over is reported unused.
// The parser is declared and called, never handed out, so no script outside
// the closure can reach the policy's sink.
const oncePerPage = [
  { object: "window", property: "fetch", message: "fragment.js crosses the session boundary in hqFetch alone." },
  { object: "window", property: "setTimeout", message: "fragment.js keeps one timer, hqEvery's." },
  { property: "parseFromString", message: "fragment.js parses markup in parse alone." },
  { property: "createPolicy", message: "fragment.js mints the page's one Trusted Types policy, in parse." },
];

const parserStaysInside = {
  selector:
    "Identifier[name='parse']:not(CallExpression > Identifier.callee, VariableDeclarator > Identifier.id, MemberExpression > Identifier.property, Property > Identifier.key)",
  message: "The parser is declared and called inside hqFragment's closure, never handed out: anything holding it holds the Trusted Types sink.",
};

export default [
  {
    linterOptions: { reportUnusedDisableDirectives: "error" },
  },
  {
    files: ["static/js/**/*.js"],
    ...js.configs.recommended,
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: {
        ...globals.browser,
        // fragment.js declares these, and loads before any handler runs.
        hqEvery: "readonly",
        hqFetch: "readonly",
        hqFragment: "readonly",
      },
    },
  },
  {
    files: ["static/js/**/*.js"],
    ...noUnsanitized.configs.recommended,
  },
  {
    // The modern forms: block-scoped bindings, const unless reassigned, and
    // comparison without coercion.
    files: ["static/js/**/*.js"],
    rules: {
      eqeqeq: "error",
      "no-var": "error",
      "prefer-const": "error",
    },
  },
  {
    files: ["static/js/**/*.js"],
    ignores: ["static/js/fragment.js"],
    rules: {
      "no-restricted-globals": ["error", ...primitiveGlobals],
      "no-restricted-properties": ["error", ...primitiveProperties],
      "no-restricted-syntax": ["error", ...primitiveSyntax],
    },
  },
  {
    files: ["static/js/fragment.js"],
    languageOptions: { globals: { hqEvery: "off", hqFetch: "off", hqFragment: "off" } },
    rules: {
      "no-restricted-globals": [
        "error",
        { name: "fetch", message: "fragment.js crosses the session boundary in hqFetch alone, as window.fetch." },
        { name: "setTimeout", message: "fragment.js keeps one timer, hqEvery's, as window.setTimeout." },
      ],
      "no-restricted-properties": ["error", ...oncePerPage],
      "no-restricted-syntax": ["error", parserStaysInside],
    },
  },
  {
    files: ["static/js/tables.js"],
    rules: {
      "no-restricted-syntax": [
        "error",
        ...primitiveSyntax,
        {
          selector: `Identifier[name=/${layoutReads}/]`,
          message: "tables.js reads layout only where a disable comment says why it must.",
        },
      ],
    },
  },
];
