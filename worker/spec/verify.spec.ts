import { describe, expect, it } from "vitest";
import routes from "../src/generated/routes.json";
import { verify } from "../src/verify";

const scheme = (provider: string) => routes.providers.find((route) => route.provider === provider)!.scheme;

// GitHub's published test vector (docs.github.com, "Validating webhook deliveries").
const SECRET = "It's a Secret to Everybody";
const PAYLOAD = new TextEncoder().encode("Hello, World!");
const SIGNATURE = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17";

describe("GitHub's signature", () => {
  const github = scheme("github_app");

  it("verifies GitHub's published test vector", async () => {
    expect(await verify(github, SECRET, SIGNATURE, PAYLOAD)).toBe(true);
  });

  it.each([
    ["another secret", "It's a Secret to Nobody", SIGNATURE],
    ["no signature", SECRET, null],
    ["a SHA-1 signature", SECRET, "sha1=01dc10d0c83e72ed246219cdd91669667fe2ca59"],
    ["a truncated digest", SECRET, SIGNATURE.slice(0, -2)],
    ["an odd number of hex digits", SECRET, SIGNATURE.slice(0, -1)],
    ["a digit that is not hex", SECRET, SIGNATURE.slice(0, -1) + "g"],
    ["no secret", "", SIGNATURE],
  ])("refuses %s", async (_, secret, signature) => {
    expect(await verify(github, secret, signature, PAYLOAD)).toBe(false);
  });
});

// A delivery signed as tailscale.com/kb/1213/webhooks says, "<t>.<body>"
// under HMAC-SHA256, computed outside the Worker (Python's hmac).
const TS_SECRET = "tskey-webhook-example";
const TS_BODY = new TextEncoder().encode('[{"type":"test"}]');
const TS_AT = 1663781880;
const TS_DIGEST = "402c715f5b9613732571b3802154f3049d2452c7aa425e3da959731ccf75feca";
const TS_ROTATED = "6e792edb4eb58f09f1426bef4a0cc7984fbedfc876548909507db0f9c8ac1009";
const SIGNED = `t=${TS_AT},v1=${TS_DIGEST}`;

describe("Tailscale's signature", () => {
  const tailscale = scheme("tailscale");
  const at = (seconds: number) => (TS_AT + seconds) * 1000;

  it.each([
    ["as sent", SIGNED, at(0)],
    ["four minutes on", SIGNED, at(240)],
    ["beside a digest under a rotated secret", `t=${TS_AT},v1=${TS_ROTATED},v1=${TS_DIGEST}`, at(0)],
    ["beside a part of a later scheme", `${SIGNED},v2=abc`, at(0)],
  ])("verifies a delivery %s", async (_, header, now) => {
    expect(await verify(tailscale, TS_SECRET, header, TS_BODY, now)).toBe(true);
  });

  it.each([
    ["signed more than five minutes ago", SIGNED, at(301)],
    ["signed more than five minutes ahead", SIGNED, at(-301)],
    ["with no signature", null, at(0)],
    ["with no time", `v1=${TS_DIGEST}`, at(0)],
    ["with another time", `t=${TS_AT + 1},v1=${TS_DIGEST}`, at(0)],
    ["with two times", `t=${TS_AT},t=${TS_AT},v1=${TS_DIGEST}`, at(0)],
    ["with only another secret's digest", `t=${TS_AT},v1=${TS_ROTATED}`, at(0)],
    ["with a digest that is not hex", `t=${TS_AT},v1=${TS_DIGEST.slice(0, -1)}g`, at(0)],
    ["with a part that is not key=value", `${SIGNED},v1`, at(0)],
  ])("refuses a delivery %s", async (_, header, now) => {
    expect(await verify(tailscale, TS_SECRET, header, TS_BODY, now)).toBe(false);
  });

  it("refuses another secret", async () => {
    expect(await verify(tailscale, "another-webhook-secret", SIGNED, TS_BODY, at(0))).toBe(false);
  });
});

// Cloudflare sends the destination's secret itself in cf-webhook-auth
// (developers.cloudflare.com, Notifications, Configure webhooks).
const CF_SECRET = "example-cloudflare-webhook-secret";

describe("Cloudflare's shared secret", () => {
  const cloudflare = scheme("cloudflare_api");

  it("verifies the secret, whatever the body", async () => {
    expect(await verify(cloudflare, CF_SECRET, CF_SECRET, PAYLOAD)).toBe(true);
  });

  it.each([
    ["another secret", CF_SECRET, "another-cloudflare-secret"],
    ["the secret cut short", CF_SECRET, CF_SECRET.slice(0, -1)],
    ["the secret and more", CF_SECRET, `${CF_SECRET} `],
    ["no header", CF_SECRET, null],
    ["an empty header with no secret held", "", ""],
  ])("refuses %s", async (_, held, header) => {
    expect(await verify(cloudflare, held, header, PAYLOAD)).toBe(false);
  });

  it("is never read as a signature, nor a signature as a secret", async () => {
    expect(await verify({ ...cloudflare, form: "hmac" }, CF_SECRET, CF_SECRET, PAYLOAD)).toBe(false);
    expect(await verify(scheme("github_app"), SECRET, SECRET, PAYLOAD)).toBe(false);
  });
});

describe("a scheme of a form the Worker does not know", () => {
  it("proves nothing", async () => {
    expect(await verify({ ...scheme("github_app"), form: "rsa" }, SECRET, SIGNATURE, PAYLOAD)).toBe(false);
  });
});
