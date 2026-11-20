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
// A run of sibling sections or cards is drawn by the layout part (`{% layout %}`,
// its rules in docs/Shared-UI.md), so no declaration lays out an
// auto-fit or auto-fill grid. A part that lays out something else inside
// itself (a form's fields, a band's cells) says so in a disable comment.
const autoGrid = /\brepeat\(\s*auto-(fit|fill)\b/i;
const flowGrid = { "/./": [`/${autoGrid.source}/i`] };
// Every edge, shadow and corner is drawn by a list in surfaces.css, so every
// box rounds the same way; elsewhere a corner, or a property naming one, is a
// step of the --radius scale (tokens.css).
const frameProperties = ["border", "border-width", "border-style", "box-shadow"];
const radiusStep = String.raw`(?:0|var\(--[\w-]*radius[\w-]*(?:,\s*var\(--radius(?:-sm|-pill)?\))?\))`;
const radiusSteps = { "/^(border(-[a-z]+)*-radius|--[\\w-]*radius[\\w-]*)$/": [`/^${radiusStep}(?:\\s+${radiusStep}){0,3}$|^inherit$/`] };
const allowedValues = (keys) => [{ "font-display": ["optional"], ...keys }, { message: (property, value) => (/radius/.test(property) ? `"${property}: ${value}" rounds a corner of its own: write var(--radius-sm), var(--radius) or var(--radius-pill)` : `Disallowed value "${value}" for property "${property}"`) }];
// A file a rule fetches arrives after the page is drawn and moves what it
// lands beside, past the moment the browser gate measures (`_DRAWN`). A face,
// a @font-face's `src`, is the one fetch `document.fonts` reports, so the gate
// waits for it.
const fetched = { "/^(?!src$)/": ["/\\burl\\(/i"] };
// What only a screen reader hears is clipped by `.visually-hidden`
// (cadence.css) alone, so a change to how it is clipped is made once.
const readerClip = { "clip-path": ["/inset\\(\\s*50%\\s*\\)/"] };
// One list holds every disallowed value, so its message tells an auto grid, a
// cut and a measure from the rest. Every sheet's list holds the fetch's and
// the reader's clip.
const disallowedValues = (keys) => [
  { ...keys, ...flowGrid, ...fetched, ...readerClip },
  {
    message: (property, value) =>
      matches(paletteTone, property) && new RegExp(paletteTone["/^.+$/"][0].slice(1, -1)).test(value)
        ? `"${property}: ${value}" picks a tone's palette: give the element its state's tone class (\`tone_class\`) and read the role it sets (--tone-mark, --tone-ink, --tone-bg, --tone-tint, --tone-wash, --tone-line)`
        : autoGrid.test(value)
        ? `"${property}: ${value}" lays out its own auto-fit grid: a run of sibling sections or cards is the layout part (\`{% layout %}\`, docs/Shared-UI.md); a part that lays out something else inside itself says so in a disable comment`
        : matches(cutText, property)
        ? `"${property}: ${value}" cuts text outside cuts.css: move the whole rule to static/css/app/cuts.css, since a selector is styled in one sheet (test_a_selector_is_styled_in_one_place); a box that clips something other than text says why in a disable comment`
        : matches(readingMeasure, property)
          ? `"${property}: ${value}" stops text at a reading measure: prose fills the card or section it sits in (docs/Design.md); a cap that sizes something other than prose says why in a disable comment`
          : /\burl\(/i.test(value)
            ? `"${property}: ${value}" fetches a file: a sheet fetches only a face, in @font-face's src`
            : property === "clip-path"
              ? `"${property}: ${value}" hides text for a screen reader: write the \`visually-hidden\` class`
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

// A part draws a state in the roles its tone class sets (tokens.css, "Tones"),
// so no rule picks a tone's palette itself: the button's destructive look,
// which is no state, is the one other reader.
const paletteTone = {
  "/^.+$/": ["/var\\(--(ok|warn|danger|attn)(?![-\\w])|var\\(--tone-(positive|caution|critical|severe|info|neutral|special)-/"],
};
// What every component sheet holds its values to; the button's sheet leaves out
// the tones' palette.
const buttonSheet = { ...literalFontSize, ...spacingLiteral, ...systemColor, ...unspokenContent, ...cutText, ...readingMeasure };
const everySheet = { ...buttonSheet, ...paletteTone };

// Capitals are for a kicker over a group, never a value's name, a state or a
// tag, which are said in sentence case (docs/Design.md). The kicker is one
// rule, `.eyebrow`, the only one that may set a text's case: a part that heads
// a group writes the class.
const caseProperties = ["text-transform", "font-variant-caps"];
const caseOutsideKicker = { "/^(?!\\.eyebrow$)/": caseProperties };
const caseMessage = (selector, property) =>
  `"${property}" changes a text's case in "${selector}": capitals are the kicker's, so write the \`eyebrow\` class on what heads a group, or say it in sentence case`;
const disallowedProperties = (properties) => [properties, { message: (property) => (frameProperties.includes(property) ? `"${property}" draws a frame outside surfaces.css: name the part in its list there` : `Disallowed property "${property}"`) }];
// A reader who asks for less motion gets it from one rule in base.css, which
// stops every transition and animation, so a part never writes its own. A
// part that draws something else in place of a motion says so in a disable
// comment.
const mediaFeatures = (names) => [
  names,
  {
    message: (name) =>
      name === "prefers-reduced-motion"
        ? `"${name}" is answered once, in base.css, for every element: delete what only stops a motion; a part that draws something else in its place says so in a disable comment`
        : `"${name}" skips an operator's chosen theme: a palette is a light-dark() token picked by color-scheme`,
  },
];
// How wide a box in a table is, its own or its tracks', is the table
// primitive's: an open row holds every box in it to its cell (surfaces.css),
// and a page that sizes one inside a table reopens what that closes. So is
// how far a cell's line holds out, bounded by its column's share (`--hold`,
// tables.css): a page's nowrap there widens the table past its box. A
// selector reaches inside a table through `.data-table` and a combinator, a
// cell, or the line beneath a row.
const inTable = "(\\.data-table(?![-\\w])[^,]*[\\s>+~]|(^|[\\s>+~(,])(td|th)(?![-\\w])|\\.row-(detail|columns)(?![-\\w]))";
const tableHeld = ["/^(min-|max-)?(width|inline-size)$/", "flex", "flex-basis", "/^grid(-template)?(-columns)?$/", "white-space", "text-wrap-mode"];

// A primitive looks one way, drawn in the sheets that draw it, so a page
// built from it looks like every other (`hq/restyled-primitive`). Each is
// the classes that name it, a table its own elements too, and those sheets.
// The table's are where it is drawn (`table_region`'s rules, docs/Shared-UI.md),
// and its parts are what its rules tell a cell's parts apart by.
const primitives = [
  {
    name: "table",
    classes: "data-table|row-detail|row-columns",
    names: String.raw`(?:\.(?:data-table|row-detail|row-columns)(?![-\w])|(?<![-\w.#])(?:table|caption|thead|tbody|tfoot|tr|td|th)(?![-\w]))`,
    sheets: ["base", "cuts", "figures", "tables", "surfaces"],
    parts: [
      "btn", "button-words", "cell-clamp", "cell-controls", "cell-detail", "cell-line", "cell-part", "cell-short", "cell-whole", "code-list", "cut", "external-link", "is-expanded", "line-list", "line-rest", "line-run", "muted",
      "narrow-more", "narrow-only", "part", "pill", "readout", "row-actions", "row-below", "row-gutter",
      "row-editor", "row-expand", "row-fold", "row-gist", "row-less", "row-more", "select-column", "stack-list", "strut", "table-clamp", "table-scroll", "tag", "token", "visually-hidden", "whole",
    ],
  },
  { name: "button", classes: "btn", sheets: ["page", "surfaces"] },
  { name: "facts", classes: String.raw`facts-(?:list|band)|fact(?:-label|-value|-detail)?`, sheets: ["layout", "figures", "surfaces"] },
  { name: "field", classes: "field", sheets: ["forms"] },
  { name: "section", classes: String.raw`section(?:-[\w-]+)?`, sheets: ["layout", "page", "surfaces"] },
  { name: "stat", classes: String.raw`kpi(?:-[\w-]+)?`, sheets: ["cards", "surfaces"] },
  { name: "notice", classes: String.raw`notice(?:-(?:attention|error|info|success|warning))?`, sheets: ["routes", "surfaces"] },
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

// A child of one parent that `:is()` asks to be a child of another: both
// parents are the one element's, so the selector names a parent twice and
// matches nothing a page draws (`.btn > :is(.words > .token)`).
const childInIs = String.raw`/>\s*:is\([^()]*>/`;
const childInIsMessage = "a child combinator before `:is()` and another inside it ask one element's parent to be both: write each path whole inside `:is()` (`:is(.a > .b, .a > .c > .b)`)";
const selectorMessage = (selector, message) => {
  if (new RegExp(childInIs.slice(1, -1)).test(selector)) return childInIsMessage;
  return message(selector);
};
const selectorRule = (patterns, message) => [[...patterns, childInIs], { splitList: true, message: (selector) => selectorMessage(selector, message) }];

// A sheet that draws a primitive may write that one's variants.
const restyledBySheet = [...new Set(primitives.flatMap((each) => each.sheets))].map((name) => ({
  files: [sheet(name)],
  rules: { "hq/restyled-primitive": { primitives, sheet: name } },
}));

export default {
  plugins: [
    "./stylelint-ground-cover.mjs",
    "./stylelint-restyled-primitive.mjs",
    "./stylelint-browser-floor.mjs",
  ],
  // The app sheets share one set of tokens, so a property one sheet reads may
  // be defined in another.
  referenceFiles: ["../../static/css/app/*.css"],
  // A rule switched off says why, and one that no longer needs switching off
  // is found.
  reportDescriptionlessDisables: true,
  reportNeedlessDisables: true,
  rules: {
    "hq/ground-cover": true,
    // A feature newer than the browser floor (docs/Design.md, "Browsers") is
    // written inside @supports, or named here with what an engine without it
    // draws instead.
    "hq/browser-floor": {
      degrades: {
        "css.properties.text-wrap.pretty": "the engine's own line breaks",
        "css.properties.baseline-source": "the last line's baseline, and a row-columns pair set by its top (tables.css)",
        "css.at-rules.view-transition": "a navigation with no cross-fade",
        "css.properties.break-after.avoid": "a printed heading that may end a page",
      },
    },
    // Any other sheet draws none.
    "hq/restyled-primitive": { primitives },
    // CSS resolves an undefined custom property to an invalid value without
    // an error, so a typo'd token degrades to a browser default.
    "no-unknown-custom-properties": true,
    // A browser drops a rule whose selector is invalid (a `:has()` inside
    // another) without an error, so the rule silently matches nothing.
    "selector-no-invalid": true,
    // An unlayered rule beats every layer at any specificity, so nothing in
    // `components` could override it but another unlayered rule.
    "rule-nesting-at-rule-required-list": ["layer"],
    // A sheet another one imports is fetched once its parent arrives (`fetched`).
    "at-rule-disallowed-list": ["import"],
    // A selector's rule is written once per file and at-rule block. The test
    // beside StyleContractTests holds it across sheets and `@layer` blocks.
    "no-duplicate-selectors": true,
    // A declaration says its value once: a second copy in the same block is
    // a stale edit the first silently loses to, and longhands a shorthand
    // says in one declaration are bytes and lines with nothing to add.
    // no-descending-specificity stays off: it compares rules only within the
    // nearest at-rule block, so it reads a sheet's `@layer` block as a media
    // context and never compares across the sheets one layer spans, where the
    // cascade HQ orders is.
    "declaration-block-no-duplicate-properties": true,
    "declaration-block-no-duplicate-custom-properties": true,
    "declaration-block-no-redundant-longhand-properties": true,
    "shorthand-property-no-redundant-values": true,
    // Color lives in tokens, so a theme is a set of token overrides; a literal
    // anywhere else stays a light-mode value on a dark page.
    "color-no-hex": true,
    "color-named": "never",
    // A palette is a `light-dark()` token picked by `color-scheme`, so the
    // system's preference and an operator's chosen theme paint one page. A
    // rule under the system's preference skips the chosen theme, and one keyed
    // on the choice (`data-theme`, which only the tokens read) skips the
    // system's; the browser gate paints each palette through `color-scheme`,
    // so it would read neither.
    "media-feature-name-disallowed-list": mediaFeatures(["prefers-color-scheme", "prefers-reduced-motion"]),
    "selector-attribute-name-disallowed-list": [
      ["data-theme"],
      { message: "A rule keyed on data-theme paints a chosen theme apart from the system's: write the color as a light-dark() token in tokens.css" },
    ],
    "function-disallowed-list": [`/^(${colorFunctions})$/i`],
    "declaration-property-value-disallowed-list": disallowedValues(everySheet),
    // A face that arrives late and is swapped in moves every line it sets, so
    // each is shown only if it is ready for the first paint. The head preloads
    // its file beside the stylesheet so it usually is, reading each quoted
    // url() a @font-face names (`stylesheets.FACES`).
    "at-rule-property-required-list": { "font-face": ["font-display"] },
    "function-url-quotes": "always",
    "declaration-property-value-allowed-list": allowedValues(radiusSteps),
    "property-disallowed-list": disallowedProperties(frameProperties),
    "rule-selector-property-disallowed-list": [
      // The first key a selector matches decides, so a table's selector is
      // held to the kicker's rule too.
      { [`/${inTable}/`]: [...tableHeld, ...caseProperties], ...caseOutsideKicker },
      { message: (selector, property) => (caseProperties.includes(property) ? caseMessage(selector, property) : `Disallowed property "${property}" for selector "${selector}"`) },
    ],
    "selector-disallowed-list": selectorRule([], () => childInIsMessage),
  },
  overrides: [
    {
      // The one sheet that draws a frame.
      files: [sheet("surfaces")],
      rules: { "property-disallowed-list": null },
    },
    {
      // The table primitive sizes what a table holds.
      files: tablePrimitive,
      rules: {
        "rule-selector-property-disallowed-list": [caseOutsideKicker, { message: caseMessage }],
        "selector-disallowed-list": selectorRule(
          cellRoles,
          (selector) =>
            `"${selector}" tells a cell's part by a page's class or its tags: key it on its role (\`[data-cell~=name|prose|data]\`, table_tags.mark_cells) or a part the table primitive draws`,
        ),
      },
    },
    ...restyledBySheet,
    {
      // The button's sheet draws the destructive look, which is no state.
      files: [sheet("page")],
      rules: { "declaration-property-value-disallowed-list": disallowedValues(buttonSheet) },
    },
    {
      // The one sheet that answers a reader who asks for less motion.
      files: [sheet("base")],
      rules: {
        "media-feature-name-disallowed-list": mediaFeatures(["prefers-color-scheme"]),
        "declaration-property-value-disallowed-list": disallowedValues(everySheet),
      },
    },
    {
      // The tokens layer is where colors are written.
      files: [tokens],
      rules: {
        "color-no-hex": null,
        "color-named": null,
        "function-disallowed-list": null,
        // The tokens set `color-scheme` from the operator's choice.
        "selector-attribute-name-disallowed-list": null,
        // The tokens declare the --radius scale.
        "declaration-property-value-allowed-list": allowedValues({}),
        "declaration-property-value-disallowed-list": disallowedValues({ ...literalFontSize, ...spacingLiteral, ...unspokenContent, ...cutText, ...readingMeasure }),
      },
    },
    {
      // The one sheet that cuts text.
      files: [cuts],
      rules: {
        "declaration-property-value-disallowed-list": disallowedValues({ ...literalFontSize, ...spacingLiteral, ...systemColor, ...unspokenContent, ...readingMeasure, ...paletteTone }),
      },
    },
    {
      // The API reference viewer is drawn from HQ's tokens: no color, size or
      // face of its own.
      files: [reference],
      rules: {
        "function-disallowed-list": [`/^(${colorFunctions}|color-mix)$/i`],
        "property-disallowed-list": disallowedProperties([...frameProperties, ...caseProperties, "font-family"]),
        "rule-selector-property-disallowed-list": null,
        // Unlayered, to outrank the layers the viewer declares after HQ's (its header).
        "rule-nesting-at-rule-required-list": null,
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
