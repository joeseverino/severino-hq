// The browser scripts' contract, held by `lint:js`. Paths are relative to the
// repository root, where the task runs (`--config` sets no base of its own).
// Each script is a classic script the page loads, so it may name only
// what the browser defines, what it declares itself, or what fragment.js
// shares with every page.

import js from "@eslint/js";
import noUnsanitized from "eslint-plugin-no-unsanitized";
import globals from "globals";

// What reads layout, or watches it change. Layout is the stylesheet's: a
// script reads it only where a disable comment says why CSS cannot answer
// the question, so a new read is refused and an allowance that no longer
// reads anything is reported as unused.
const layoutReads = {
  selector:
    "Identifier[name=/^(getBoundingClientRect|getClientRects|getComputedStyle|offset(Width|Height|Left|Top)|scroll(Width|Height)|client(Width|Height)|ResizeObserver)$/]",
  message: "Layout is computed by CSS: a script reads it only where a disable comment says why it must.",
};

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

// A primitive's markup is the server's: a notice, a message, a button, a pill
// or a row's opened line drawn by a script is a second drawing of it that
// the template's never sees change. A class is written as a literal in a
// className, a class attribute or a classList change, and each form is
// refused for a primitive's class or one of its tones (`msg-error`).
// Today's instances are counted in eslint-suppressions.json beside it,
// which ESLint fails on once one no longer occurs.
// Python strings are refused the same names by markup-drawn-by-templates
// (tools/lint/ast-grep), which MarkupRuleTests holds to this list.
const primitiveClass = String.raw`/(^|\s)(notice(-attention)?|msg(-(error|info|success|warning))?|btn|pill(-[\w-]+)?|row-detail|row-columns)(\s|$)/`;
const drawnMarkup = [
  `AssignmentExpression[left.property.name='className'] Literal[value=${primitiveClass}]`,
  `AssignmentExpression[left.property.name='className'] TemplateElement[value.raw=${primitiveClass}]`,
  `CallExpression[callee.property.name='setAttribute'][arguments.0.value='class'] Literal[value=${primitiveClass}]`,
  `CallExpression[callee.object.property.name='classList'][callee.property.name=/^(add|toggle|replace)$/] > Literal[value=${primitiveClass}]`,
].map((selector) => ({
  selector,
  message:
    "A primitive's markup is the server's: fill what the page's HTML already holds, or swap in a part a template draws (`hqFragment.swap`), never spelling its class in a script.",
}));

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

// A native submit sends the pressed button among the form's fields in tree
// order; `set` overwrites a field of the same name and `append` puts it last.
// `new FormData(form, submitter)` sends what a plain submit sends.
const submitterRebuilt = {
  selector:
    "CallExpression[callee.property.name=/^(set|append)$/][arguments.0.property.name='name'][arguments.1.property.name='value']",
  message: "A submit's body is `new FormData(form, submitter)`, which places the button as a native submit does.",
};

// A part of the page can be replaced by an answer (base.html's `main` is a
// region), and an element found once as the page loads is gone with the part
// it was in, its successor never set up. So the page is asked for elements at
// load only through `hqFragment.each`, which sets up each one placed since
// too, or by a listener on the document asking where an event landed. Load is
// top-level code, a function called where it is written, and a
// DOMContentLoaded listener; a query there for what no fragment holds (the
// frame's header, its dialogs) says so in its disable comment.
const loadTimeQuery = {
  selector:
    "CallExpression[callee.object.name='document'][callee.property.name=/^(querySelector(All)?|getElementById|getElementsBy\\w+)$/]"
    + ":not(:function:not(.callee, CallExpression[callee.property.name='addEventListener'][arguments.0.value='DOMContentLoaded'] > .arguments) *)",
  message:
    "An element found once at load is lost when a fragment replaces its part: set it up through `hqFragment.each(selector, setup)`, or listen on the document.",
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
      "no-restricted-syntax": ["error", ...primitiveSyntax, ...drawnMarkup, layoutReads, submitterRebuilt, loadTimeQuery],
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
      "no-restricted-syntax": ["error", parserStaysInside, ...drawnMarkup, layoutReads, submitterRebuilt, loadTimeQuery],
    },
  },
];
