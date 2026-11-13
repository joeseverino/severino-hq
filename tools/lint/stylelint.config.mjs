// The stylesheet's contract, held by `lint:css`. Paths are relative to this
// file. Any rule Stylelint cannot express stays a test beside StyleContractTests
// (hq/platform/application/tests/test_architecture_styles.py), with its reason.

const tokens = "../../static/css/app/tokens.css";
const cuts = "../../static/css/app/cuts.css";
const sheet = (name) => `../../static/css/app/${name}.css`;
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
const matches = (keys, property) => Object.keys(keys).some((key) => new RegExp(key.slice(1, -1)).test(property));
// Prose fills the card or section it sits in, so no text stops at a reading
// measure: no measure token, and no cap in characters, the unit a measure is
// written in. A cap that sizes a box other than prose (a centered notice, a
// control) says why in a disable comment.
const readingMeasure = {
  "/^--([\\w-]*-)?measure(-[\\w-]*)?$/": ["/./"],
  "/^max-(width|inline-size)$/": ["/(?<![-\\w.])[\\d.]+ch\\b/"],
};
// One list holds every disallowed value, so its message tells a cut and a
// measure from the rest by its property.
const disallowedValues = (keys) => [
  keys,
  {
    message: (property, value) =>
      matches(cutText, property)
        ? `"${property}: ${value}" cuts text outside cuts.css: move the whole rule to static/css/app/cuts.css, since a selector is styled in one sheet (test_a_selector_is_styled_in_one_place); a box that clips something other than text says why in a disable comment`
        : matches(readingMeasure, property)
          ? `"${property}: ${value}" stops text at a reading measure: prose fills the card or section it sits in (docs/Design.md); a cap that sizes something other than prose says why in a disable comment`
          : `Disallowed value "${value}" for property "${property}"`,
  },
];

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

// Capitals are for a kicker over a group (`.eyebrow`), never a value's name,
// a state or a tag, which are said in sentence case (docs/Design.md). A rule
// that sets a kicker's case says what it heads in a disable comment.
const caseProperties = ["text-transform", "font-variant-caps"];
const disallowedProperties = (properties) => [
  properties,
  {
    message: (property) =>
      caseProperties.includes(property)
        ? `"${property}" changes a text's case: capitals are for a kicker over a group, so say it in sentence case, or say in a disable comment what the kicker heads`
        : `Disallowed property "${property}"`,
  },
];
// How wide a box in a table is, its own or its tracks', is the table
// primitive's: an open row holds every box in it to its cell (surfaces.css),
// and a page that sizes one inside a table reopens what that closes. A
// selector reaches inside a table through `.data-table` and a combinator, a
// cell, or the line beneath a row.
const inTable = "(\\.data-table(?![-\\w])[^,]*[\\s>+~]|(^|[\\s>+~(,])(td|th)(?![-\\w])|\\.row-(detail|columns)(?![-\\w]))";
const boxWidth = ["/^(min-|max-)?(width|inline-size)$/", "flex", "flex-basis", "/^grid(-template)?(-columns)?$/"];

// A primitive looks one way, drawn in the sheets that draw it, so a page
// built from it looks like every other (`hq/restyled-primitive`). Each is
// the classes that name it, a table its own elements too, and those sheets.
// The table's are where it is drawn (docs/Design.md, "A table fits its box"),
// and its parts are what its rules tell a cell's parts apart by.
const primitives = [
  {
    name: "table",
    classes: "data-table|row-detail|row-columns",
    names: String.raw`(?:\.(?:data-table|row-detail|row-columns)(?![-\w])|(?<![-\w.#])(?:table|caption|thead|tbody|tfoot|tr|td|th)(?![-\w]))`,
    sheets: ["base", "cuts", "figures", "tables", "surfaces"],
    parts: [
      "btn", "cell-line", "cell-part", "cell-whole", "cut", "external-link", "is-expanded", "kept-line", "line-list", "line-rest", "line-run", "muted",
      "name-line", "narrow-more", "narrow-only", "part", "pill", "readout", "row-actions", "row-below", "row-gutter",
      "row-editor", "row-later", "row-less", "row-more", "select-column", "strut", "table-clamp", "table-scroll", "token", "visually-hidden", "whole",
    ],
  },
  { name: "button", classes: "btn", sheets: ["page"] },
  { name: "facts", classes: String.raw`facts-(?:list|band)|fact(?:-label|-value|-detail)?`, sheets: ["layout", "figures", "surfaces"] },
  { name: "field", classes: "field", sheets: ["forms"] },
  { name: "section", classes: String.raw`section(?:-[\w-]+)?`, sheets: ["layout", "page", "surfaces"] },
  { name: "stat", classes: String.raw`kpi(?:-[\w-]+)?`, sheets: ["cards", "surfaces"] },
  { name: "notice", classes: String.raw`notice(?:-(?:attention|error|info|success|warning))?`, sheets: ["routes"] },
  { name: "card", classes: "card", sheets: ["layout", "surfaces"] },
];
const [table] = primitives;
const tablePrimitive = table.sheets.map(sheet);

// What the table primitive's rules tell a cell's parts apart by: the role the
// region marks on each (`data-cell`, table_tags.mark_cells) and the parts the
// primitive draws, never a page's class or what a cell leads with. A table a
// page writes later then sets as every other does, with nothing added here.
const cell = "(?:^|[\\s>+~(,])(?:td|th)(?![-\\w])|\\.row-(?:detail|columns)(?![-\\w])";
const cellRoles = [
  // A cell's content by its tags, where the row's name is `[data-cell~=name]`.
  `/(?:^|[\\s>+~(,])(?:td|th)(?![-\\w])(?:\\[[^\\]]*\\]|\\.[\\w-]+|:[\\w-]+)*(?:\\s*>\\s*|:has\\(\\s*>\\s*)(?:a(?![-\\w])[^\\s>,()]*\\s*>\\s*)?strong(?![-\\w])/`,
  // Any class inside a cell but the primitive's own and its parts: a role is its mark.
  `/(?:${cell})[\\s\\S]*\\.(?!(?:${[table.classes, ...table.parts].join("|")})(?![-\\w]))[a-zA-Z][\\w-]*/`,
];

// A table's narrow box is a property, `--box`, which the script's mark
// (`data-narrow`, tables.js) sets and a phone's region with no script sets
// too, so a rule reads the property (`@container style(--box: narrow)`): one
// that read the mark would miss the box no script measures.
const narrowMark = String.raw`/\.data-table(?![-\w])[^\s,>+~]*\[data-narrow\]/`;
const narrowMessage = "a table's narrow box is `--box` (`@container style(--box: narrow)` or `whole`), never the script's `[data-narrow]` mark, which a page with no script never gets";
const selectorRule = (patterns, message) => [patterns, { splitList: true, message: (selector) => (new RegExp(narrowMark.slice(1, -1)).test(selector) ? narrowMessage : message(selector)) }];

// A sheet that draws a primitive may write that one's variants.
const restyledBySheet = [...new Set(primitives.flatMap((each) => each.sheets))].map((name) => ({
  files: [sheet(name)],
  rules: { "hq/restyled-primitive": { primitives, sheet: name } },
}));

export default {
  plugins: [
    "./stylelint-faded-text.mjs",
    "./stylelint-flow-grid.mjs",
    "./stylelint-ground-cover.mjs",
    "./stylelint-restyled-primitive.mjs",
  ],
  // The app sheets share one set of tokens, so a property one sheet reads may
  // be defined in another.
  referenceFiles: ["../../static/css/app/*.css"],
  // A rule switched off says why, and one that no longer needs switching off
  // is found.
  reportDescriptionlessDisables: true,
  reportNeedlessDisables: true,
  rules: {
    "hq/faded-text": true,
    "hq/flow-grid": true,
    "hq/ground-cover": true,
    // Any other sheet draws none.
    "hq/restyled-primitive": { primitives },
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
    "declaration-property-value-disallowed-list": disallowedValues({
      ...literalFontSize,
      ...spacingLiteral,
      ...systemColor,
      ...unspokenContent,
      ...cutText,
      ...readingMeasure,
    }),
    // A face that arrives late and is swapped in moves every line it sets, so
    // each is shown only if it is ready for the first paint. Its file is
    // preloaded beside the stylesheet so it usually is (StyleContractTests).
    "at-rule-property-required-list": { "font-face": ["font-display"] },
    "declaration-property-value-allowed-list": { "font-display": ["optional"] },
    "property-disallowed-list": disallowedProperties(caseProperties),
    "rule-selector-property-disallowed-list": { [`/${inTable}/`]: boxWidth },
    "selector-disallowed-list": selectorRule([narrowMark], () => narrowMessage),
  },
  overrides: [
    {
      // The table primitive sizes what a table holds.
      files: tablePrimitive,
      rules: {
        "rule-selector-property-disallowed-list": null,
        "selector-disallowed-list": selectorRule(
          [...cellRoles, narrowMark],
          (selector) =>
            `"${selector}" tells a cell's part by a page's class or its tags: key it on its role (\`[data-cell~=name|prose|data]\`, table_tags.mark_cells) or a part the table primitive draws`,
        ),
      },
    },
    ...restyledBySheet,
    {
      // The tokens layer is where colors are written.
      files: [tokens],
      rules: {
        "color-no-hex": null,
        "color-named": null,
        "function-disallowed-list": null,
        "declaration-property-value-disallowed-list": disallowedValues({ ...literalFontSize, ...spacingLiteral, ...unspokenContent, ...cutText, ...readingMeasure }),
      },
    },
    {
      // The one sheet that cuts text.
      files: [cuts],
      rules: {
        "declaration-property-value-disallowed-list": disallowedValues({ ...literalFontSize, ...spacingLiteral, ...systemColor, ...unspokenContent, ...readingMeasure }),
      },
    },
    {
      // The API reference viewer is drawn from HQ's tokens: no color, size or
      // face of its own.
      files: [reference],
      rules: {
        "function-disallowed-list": [`/^(${colorFunctions}|color-mix)$/i`],
        "property-disallowed-list": disallowedProperties([...caseProperties, "font-family"]),
        "declaration-property-value-disallowed-list": disallowedValues({
          "/(^|-)font-size$/": ["/^[\\d.]/"],
          ...spacingLiteral,
          ...systemColor,
          ...unspokenContent,
          ...cutText,
          ...readingMeasure,
        }),
        // Its classes are the vendored viewer's markup (`.section-header`),
        // which HQ's primitives do not draw.
        "hq/restyled-primitive": null,
      },
    },
  ],
};
