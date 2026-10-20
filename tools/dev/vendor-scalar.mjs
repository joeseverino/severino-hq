#!/usr/bin/env node
// Vendors the API reference bundle at the version static/vendor/scalar/UPSTREAM
// names, and rewrites UPSTREAM to record it. To move it: change `version:` in
// UPSTREAM, then `node tools/dev/vendor-scalar.mjs`. Renovate runs the same
// command after it changes the version.
//
// The tarball is checked against the registry's integrity before anything is
// written, and the sourceMappingURL line is dropped because the map is not
// vendored and collectstatic fails on a reference it cannot resolve.

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const vendor = new URL("../../static/vendor/scalar/", import.meta.url);
const upstream = new URL("UPSTREAM", vendor);
const file = "dist/browser/standalone.js";

const recorded = Object.fromEntries(
  readFileSync(upstream, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => line.split(/: (.*)/s, 2)),
);
const { package: name, version } = recorded;

const response = await fetch(`https://registry.npmjs.org/${name}/${version}`);
if (!response.ok) throw new Error(`${name}@${version}: registry answered ${response.status}`);
const { dist } = await response.json();

const tarball = Buffer.from(await (await fetch(dist.tarball)).arrayBuffer());
const [algorithm, expected] = dist.integrity.split("-", 2);
const actual = createHash(algorithm).update(tarball).digest("base64");
if (actual !== expected) throw new Error(`${dist.tarball}: integrity ${actual} is not ${expected}`);

const scratch = mkdtempSync(join(tmpdir(), "scalar-"));
let original;
try {
  writeFileSync(join(scratch, "package.tgz"), tarball);
  original = execFileSync("tar", ["-xzOf", join(scratch, "package.tgz"), `package/${file}`], {
    maxBuffer: 64 * 1024 * 1024,
  });
} finally {
  rmSync(scratch, { recursive: true, force: true });
}

const bundle = Buffer.from(original.toString("utf8").replace(/\n\/\/# sourceMappingURL=[^\n]*\n?$/, "\n"));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

writeFileSync(new URL("standalone.js", vendor), bundle);
writeFileSync(
  upstream,
  [
    `package: ${name}`,
    `version: ${version}`,
    `license: ${recorded.license}`,
    `file: ${file}`,
    `tarball: ${dist.tarball}`,
    `integrity: ${dist.integrity}`,
    `upstream_sha256: ${sha256(original)}`,
    `edit: ${recorded.edit}`,
    `sha256: ${sha256(bundle)}`,
    "",
  ].join("\n"),
);
