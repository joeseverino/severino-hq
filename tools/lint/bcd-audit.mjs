// audit:bcd: each browser's support for the features given, from the
// browser-compat-data the lint tools pin, held to the browser floor
// (browser-floor.mjs); given none, every place the stylesheet writes a
// feature newer than the floor, each with the fallback stylelint.config.mjs
// declares for it. Each browser's statements come in the data's order, its
// current support first, and what qualifies one (a flag, a prefix, another
// name, partial support, a removal) is said beside its version.

import stylelint from "stylelint";

import { bcd, belowFloor, ENGINES, FLOOR } from "./browser-floor.mjs";
import config from "./stylelint.config.mjs";

const BROWSERS = ["safari", "safari_ios", "chrome", "chrome_android", "edge", "firefox", "firefox_android"];
const floorWords = ENGINES.map((engine) => `${engine} ${FLOOR[engine]}`).join(", ");

const released = (browser, version) => {
  const date = bcd.browsers[browser].releases[String(version).replace(/^≤/, "")]?.release_date;
  return date ? ` (${date})` : "";
};

const described = (browser, statement) => {
  if (!statement) return "no data";
  if (statement.version_added === false) return "not supported";
  if (statement.version_added === null) return "unknown";
  const version = String(statement.version_added);
  return [
    version + released(browser, version),
    statement.flags && " behind a flag",
    statement.prefix && ` as ${statement.prefix}`,
    statement.alternative_name && ` as ${statement.alternative_name}`,
    statement.partial_implementation && " partly",
    statement.version_removed && ` until ${statement.version_removed}`,
  ]
    .filter(Boolean)
    .join("");
};

const children = (node) => Object.keys(node).filter((key) => !key.startsWith("__"));

const feature = (path) => {
  const node = path.split(".").reduce((at, key) => at?.[key], bcd);
  if (!node) return [`${path}: not a path browser-compat-data holds`];
  if (!node.__compat) {
    const held = children(node);
    return [`${path}: a group, not a feature; it holds ${held.slice(0, 25).join(", ")}${held.length > 25 ? ", ..." : ""}`];
  }
  const status = Object.entries(node.__compat.status ?? {})
    .filter(([, on]) => on)
    .map(([name]) => name)
    .join(", ");
  const below = belowFloor(path);
  return [
    path + (status ? `  [${status}]` : ""),
    `  floor (${floorWords}): ${below.length ? `newer in ${below.join(", ")}` : "met"}`,
    ...BROWSERS.map((browser) => {
      const statements = [node.__compat.support[browser] ?? null].flat();
      return `  ${browser}: ${statements.map((statement) => described(browser, statement)).join("; ")}`;
    }),
    ...(children(node).length ? [`  subfeatures, each a path of its own: ${children(node).join(", ")}`] : []),
  ];
};

const stylesheet = async () => {
  const degrades = config.rules["hq/browser-floor"].degrades;
  const { results } = await stylelint.lint({
    files: "static/css/**/*.css",
    config: { plugins: ["./tools/lint/stylelint-browser-floor.mjs"], rules: { "hq/browser-floor": { degrades: {} } } },
  });
  const found = results.flatMap((file) =>
    file.warnings.map((warning) => {
      const path = warning.text.match(/\(([\w.-]+)\)/)[1];
      const where = `${file.source.slice(process.cwd().length + 1)}:${warning.line}`;
      return `${where}: ${warning.text.split(" (floor:")[0]}${path in degrades ? `; degrades to ${degrades[path]}` : ""}`;
    }),
  );
  return [`floor: ${floorWords}`, ...(found.length ? found : ["every feature the stylesheet writes outside @supports is at the floor"])];
};

const features = process.argv.slice(2);
const lines = features.length ? features.flatMap(feature) : await stylesheet();
console.log(lines.join("\n"));
