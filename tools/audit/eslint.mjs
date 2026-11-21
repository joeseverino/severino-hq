// What audit:hand-drawn counts in the browser scripts as ESLint finds it: an
// element a script builds, where the server's markup or a <template> frame
// the script clones draws it. A count for an audit to read, never a gate, so
// it holds this one rule and no other, and a script's disable comments, each
// written for lint:js's rule of the same name, hide nothing from it.
export default [
  {
    files: ["static/js/**/*.js"],
    languageOptions: { ecmaVersion: "latest", sourceType: "script" },
    linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: "off" },
    rules: {
      "no-restricted-syntax": [
        "warn",
        {
          selector: "CallExpression[callee.property.name=/^createElement(NS)?$/]",
          message: "An element built by a script, where the server's markup or a cloned <template> draws it.",
        },
      ],
    },
  },
];
