import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

// The tests run inside workerd with the Worker's own wrangler.jsonc, which
// reads each secret it requires from the environment. These are the tests'
// own; the webhook secret is GitHub's published test vector's, so the
// verifier's test and the Worker's share one key.
Object.assign(process.env, { GITHUB_WEBHOOK_SECRET: "It's a Secret to Everybody", SOCKET_TOKEN: "example-socket-token" });

export default defineConfig({
  plugins: [cloudflareTest({ wrangler: { configPath: "./wrangler.jsonc" } })],
  test: { include: ["spec/**/*.spec.ts"] },
});
