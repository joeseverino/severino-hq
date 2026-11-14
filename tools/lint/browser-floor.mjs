// HQ's browser floor (docs/Design.md, "Browsers"): the current and previous
// release of each engine, read from browser-compat-data's release dates, so
// the floor moves as the package Renovate moves does. `hq/browser-floor` and
// audit:bcd both read it from here.

import bcd from "@mdn/browser-compat-data" with { type: "json" };

export { bcd };

// Safari, Chromium as Chrome ships it, and Firefox. Safari is read on the
// Mac and on iOS, one engine at the same version numbers: a feature one of
// them has is Safari's, since what only one lacks (a cursor on a touch
// screen, text sizing on a desktop) is what that one has no use for.
export const ENGINES = ["safari", "chrome", "firefox"];
const READS = { safari: ["safari", "safari_ios"], chrome: ["chrome"], firefox: ["firefox"] };

// The engines a vendor prefix is written for: each reads only its own.
const VENDOR = { "-webkit-": ["safari", "chrome"], "-moz-": ["firefox"] };

const released = (browser) =>
  Object.entries(bcd.browsers[browser].releases)
    .filter(([, release]) => release.release_date && ["current", "retired"].includes(release.status))
    .sort(([, a], [, b]) => a.release_date.localeCompare(b.release_date));

// Each engine's previous release, the oldest the floor holds a feature to:
// what it supports, the current release supports too.
export const FLOOR = Object.fromEntries(Object.values(READS).flat().map((browser) => [browser, released(browser).at(-2)[0]]));

const parts = (version) => String(version).replace(/^≤/, "").split(".").map(Number);
const atOrBefore = (version, floor) => {
  const [a, b] = [parts(version), parts(floor)];
  for (let at = 0; at < Math.max(a.length, b.length); at += 1) {
    if ((a[at] ?? 0) !== (b[at] ?? 0)) return (a[at] ?? 0) < (b[at] ?? 0);
  }
  return true;
};

// Whether one support statement has the feature as written, `prefix` or the
// other name the data gives it, in `version`, with no flag.
const holds = (statement, version, prefix, written) =>
  typeof statement.version_added === "string" &&
  statement.version_added !== "preview" &&
  !statement.flags &&
  (statement.alternative_name ? statement.alternative_name === written : (statement.prefix ?? "") === prefix) &&
  atOrBefore(statement.version_added, version) &&
  !(statement.version_removed && atOrBefore(statement.version_removed, version));

// The compat entry at a dotted browser-compat-data path, or undefined.
export const compatAt = (path) => path.split(".").reduce((node, key) => node?.[key], bcd)?.__compat;

export const PREFIX = /^-(?:webkit|moz|ms|o)-/;

// The engines whose floor release lacks the feature at `path`, written as
// `written` (a name with its vendor prefix, if any). A prefixed name is
// judged only in its vendor's engines, and an engine whose support the data
// leaves unknown is not named.
export const belowFloor = (path, written = path.split(".").at(-1)) => {
  const compat = compatAt(path);
  if (!compat) return [];
  const vendor = written.match(PREFIX)?.[0] ?? "";
  // The prefix a statement carries, when the data holds the name unprefixed.
  const prefix = path.split(".").at(-1) === written ? "" : vendor;
  return ENGINES.filter((engine) => {
    if (vendor && !(VENDOR[vendor] ?? []).includes(engine)) return false;
    return READS[engine].every((browser) => {
      const statements = [compat.support[browser] ?? []].flat();
      if (statements.some((statement) => statement.version_added === null)) return false;
      return !statements.some((statement) => holds(statement, FLOOR[browser], prefix, written));
    });
  });
};
