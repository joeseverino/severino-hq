// The stylesheet's contract, held by `checks:css`. Paths are relative to this
// file. Any rule Stylelint cannot express stays a test beside StyleContractTests
// (hq/platform/application/tests/test_architecture.py), with its reason.

const tokens = "../../static/css/app/tokens.css";
const reference = "../../static/css/api-reference.css";

// CSS Color 4's system colors. color-named knows only the named colors, and
// `canvas` or `field` written in a component is as much a color the theme
// cannot reach as `tomato`.
const systemColors = [
  "accentcolor", "accentcolortext", "activetext", "buttonborder", "buttonface",
  "buttontext", "canvas", "canvastext", "field", "fieldtext", "graytext",
  "highlight", "highlighttext", "linktext", "mark", "marktext", "selecteditem",
  "selecteditemtext", "visitedtext",
];

// A font size is a step of the --text-* scale. `em` stays free: it says
// "relative to what this sits in", which no absolute step can.
const literalFontSize = { "/(^|-)font-size$/": ["/^[\\d.]+(px|rem)/"] };

// The even spacing rungs are --space-* tokens, so one written as a literal is
// drift. Odd values are judged one by one, `1px` is a hairline rather than a
// space, and calc(), min(), max() and clamp() carry their own arithmetic.
const spacingLiteral = {
  "/(^|-)(padding|margin|gap)(-[a-z-]+)?$/": [
    "/^(?!.*\\b(calc|max|min|clamp)\\().*(?<![-\\w.])(2|4|6|8|10|12|14|16|18|20|24|28|32|40)px/",
  ],
};

const systemColor = {
  "/^/": [`/(?<![-\\w.#$@])(${systemColors.join("|")})(?![-\\w(])/i`],
};

const colorFunctions = "rgba?|hsla?|hwb|lab|lch|oklab|oklch|color";

export default {
  // The app sheets share one set of tokens, so a property one sheet reads may
  // be defined in another.
  referenceFiles: ["../../static/css/app/*.css"],
  rules: {
    // CSS resolves an undefined custom property to an invalid value without
    // an error, so a typo'd token degrades to a browser default.
    "no-unknown-custom-properties": true,
    // Color lives in tokens, so a theme is a set of token overrides; a literal
    // anywhere else stays a light-mode value on a dark page.
    "color-no-hex": true,
    "color-named": "never",
    "function-disallowed-list": [`/^(${colorFunctions})$/i`],
    "declaration-property-value-disallowed-list": {
      ...literalFontSize,
      ...spacingLiteral,
      ...systemColor,
    },
    // What names a value (a term, a band's or a tile's label) is said in
    // sentence case; capitals are for a kicker over a group (`.eyebrow`).
    "rule-selector-property-disallowed-list": {
      "/(\\bdt|\\.band-label|\\.kpi \\.label)\\s*(,|$)/": ["text-transform"],
    },
  },
  overrides: [
    {
      // The tokens layer is where colors are written.
      files: [tokens],
      rules: {
        "color-no-hex": null,
        "color-named": null,
        "function-disallowed-list": null,
        "declaration-property-value-disallowed-list": { ...literalFontSize, ...spacingLiteral },
      },
    },
    {
      // The API reference viewer is drawn from HQ's tokens: no color, size or
      // face of its own.
      files: [reference],
      rules: {
        "function-disallowed-list": [`/^(${colorFunctions}|color-mix)$/i`],
        "property-disallowed-list": ["font-family"],
        "declaration-property-value-disallowed-list": {
          "/(^|-)font-size$/": ["/^[\\d.]/"],
          ...spacingLiteral,
          ...systemColor,
        },
      },
    },
  ],
};
