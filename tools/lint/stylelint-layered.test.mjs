// hq/layered's cases, run by lint:css before its scan is trusted.

import assert from "node:assert/strict";
import { test } from "node:test";
import stylelint from "stylelint";

const config = { plugins: [new URL("./stylelint-layered.mjs", import.meta.url).pathname], rules: { "hq/layered": true } };

const fixed = async (code) => {
  const { code: out, results } = await stylelint.lint({ code, config, fix: true });
  return { out, found: results[0].warnings.map((warning) => warning.rule) };
};

// The last layer opened before ``selector``.
const layerOf = (out, selector) => out.slice(0, out.indexOf(selector)).match(/@layer \w+ \{/g)?.at(-1);

test("a stray rule moves into the layer before it", async () => {
  const { out, found } = await fixed("@layer a { .x {} }\n@layer b { .y {} }\n.stray {}\n");
  assert.deepEqual(found, []);
  assert.equal(layerOf(out, ".stray"), "@layer b {");
  assert.ok(out.trimEnd().endsWith("}"), out);
  assert.deepEqual((await fixed(out)).found, []);
});

test("a stray block with none before it moves into the next layer, whole", async () => {
  const { out, found } = await fixed("@media (width > 1px) { .stray {} .other {} }\n@layer a { .x {} }\n");
  assert.deepEqual(found, []);
  assert.ok(out.startsWith("@layer a {"), out);
  assert.match(out, /@layer a \{\s*@media \(width > 1px\) \{ \.stray \{\} \.other \{\} \}/);
});

test("a rule a disable comment names stays where it is, unreported", async () => {
  const code = "@layer a { .x {} }\n/* stylelint-disable hq/layered */\n.kept {}\n/* stylelint-enable hq/layered */\n";
  const { out, found } = await fixed(code);
  assert.deepEqual(found, []);
  assert.equal(out, code);
});

test("a sheet with no layer to move into is reported", async () => {
  const { out, found } = await fixed(".stray {}\n");
  assert.deepEqual(found, ["hq/layered"]);
  assert.equal(out, ".stray {}\n");
});
