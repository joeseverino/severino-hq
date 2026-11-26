import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

// The tests run inside workerd with the Worker's own wrangler.jsonc, which
// reads each secret it requires from the environment. These are the tests'
// own; each webhook secret is the one its verifier's test signs with, so the
// verifier's test and the Worker's share one key.
Object.assign(process.env, {
  GITHUB_WEBHOOK_SECRET: "It's a Secret to Everybody",
  TAILSCALE_WEBHOOK_SECRET: "tskey-webhook-example",
  SOCKET_TOKEN: "example-socket-token",
});

export default defineConfig({
  plugins: [cloudflareTest({ wrangler: { configPath: "./wrangler.jsonc" } })],
  test: { include: ["spec/**/*.spec.ts"] },
});
