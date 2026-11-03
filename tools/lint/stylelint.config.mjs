// The stylesheet's contract, held by `lint:css`. Paths are relative to this
// file. Any rule Stylelint cannot express stays a test beside StyleContractTests
// (hq/platform/application/tests/test_architecture_styles.py), with its reason.

const tokens = "../../static/css/app/tokens.css";
const cuts = "../../static/css/app/cuts.css";
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

// Text is cut in one sheet, with its ellipsis or its clamp, where the browser
// gate's cut checks read every cut a page draws (docs/Design.md); every other
// text wraps. A box that clips something other than text (a frame's corners,
// a scroll region) says why beside its declaration, in a disable comment the
// config requires a reason for.
const cutText = {
  "/^overflow(-x|-y|-inline|-block)?$/": ["/\\b(hidden|clip)\\b/"],
  "text-overflow": ["/./"],
  "/^(-webkit-)?line-clamp$/": ["/./"],
};

// Text a stylesheet draws is read aloud as text unless it says otherwise, so
// a mark drawn for the eye (a disclosure's chevron, a separator, an arrow)
// was heard as "›" or "north east arrow". A generated string says what a
// reader hears after a slash: `"›" / ""` for a mark, the words themselves
// for text that means something (`"Dismissed: " / "Dismissed: "`).
// A space before a mark is where a line may break, so the mark would open
// the next line: one drawn after a word is joined to it by a no-break space
// (`"\a0·"`). A space a hex escape ends with is the escape's own.
const strandedMark =
  "/(?<!\\\\[0-9a-f]{1,6}) (?:[·•→←↗›‹»«]|\\\\0*(?:b7|2022|2192|2190|2197|203a|2039|bb|ab)(?![0-9a-f]))/i";
const unspokenContent = { "/^content$/": [`/^\\s*(["'])(?:(?!\\1).)+\\1\\s*$/`, strandedMark] };

// What an opacity under 1 may fade: an inactive control, which WCAG 1.4.3
// does not hold to 4.5:1, and a mark that is not text. A state is inactive
// on the element or on what holds it: a disabled or aria-disabled control,
// and a region aria-busy while its answer is on the way. A mark is named by
// the compound a selector styles: a chart's shapes, lines and marks, the
// nav's caret, the calendar's dots (a mark's, a timed line's) and a job's
// sweep. An opacity of 0 draws nothing, so it fades nothing.

// The rest of a compound: classes, attributes and pseudo-classes, each
// argument whole, so a name inside `:is()` is not the compound's.
const compound = String.raw`(?:::?[\w-]+(?:\([^()]*\))?|\.[\w-]+|\[[^\]]*\])*`;
const fadedText = [
  true,
  {
    inactive: [
      "(?<!:not\\()(:disabled|\\[aria-disabled=\"true\"\\])",
      "\\[aria-busy=\"true\"\\]",
    ],
    marks: [
      `(^|[\\s>+~])(rect|path|circle|line|polyline)${compound}$`,
      `\\.(nav-caret|chart-trend|chart-mark|route-line|month-mark)(?![-\\w])${compound}$`,
      "\\.job-bar-unknown i$",
      "\\.month-item::before$",
    ],
    animations: ["conn-pulse"],
  },
];

export default {
  plugins: ["./stylelint-faded-text.mjs"],
  // The app sheets share one set of tokens, so a property one sheet reads may
  // be defined in another.
  referenceFiles: ["../../static/css/app/*.css"],
  // A rule switched off says why, and one that no longer needs switching off
  // is found.
  reportDescriptionlessDisables: true,
  reportNeedlessDisables: true,
  rules: {
    "hq/faded-text": fadedText,
    // CSS resolves an undefined custom property to an invalid value without
    // an error, so a typo'd token degrades to a browser default.
    "no-unknown-custom-properties": true,
    // A selector's rule is written once per file and at-rule block. The test
    // beside StyleContractTests holds it across sheets and `@layer` blocks.
    "no-duplicate-selectors": true,
    // Color lives in tokens, so a theme is a set of token overrides; a literal
    // anywhere else stays a light-mode value on a dark page.
    "color-no-hex": true,
    "color-named": "never",
    "function-disallowed-list": [`/^(${colorFunctions})$/i`],
    "declaration-property-value-disallowed-list": {
      ...literalFontSize,
      ...spacingLiteral,
      ...systemColor,
      ...unspokenContent,
      ...cutText,
    },
    // A face that arrives late and is swapped in moves every line it sets, so
    // each is shown only if it is ready for the first paint. Its file is
    // preloaded beside the stylesheet so it usually is (StyleContractTests).
    "at-rule-property-required-list": { "font-face": ["font-display"] },
    "declaration-property-value-allowed-list": { "font-display": ["optional"] },
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
        "declaration-property-value-disallowed-list": { ...literalFontSize, ...spacingLiteral, ...unspokenContent, ...cutText },
      },
    },
    {
      // The one sheet that cuts text.
      files: [cuts],
      rules: {
        "declaration-property-value-disallowed-list": { ...literalFontSize, ...spacingLiteral, ...systemColor, ...unspokenContent },
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
          ...unspokenContent,
          ...cutText,
        },
      },
    },
  ],
};
