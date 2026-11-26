import { describe, expect, it } from "vitest";
import { VERIFIERS } from "../src/verify";

// GitHub's published test vector (docs.github.com, "Validating webhook deliveries").
const SECRET = "It's a Secret to Everybody";
const PAYLOAD = new TextEncoder().encode("Hello, World!");
const SIGNATURE = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17";

describe("github-hmac-sha256", () => {
  const verify = VERIFIERS["github-hmac-sha256"]!;

  it("verifies GitHub's published test vector", async () => {
    expect(await verify(SECRET, SIGNATURE, PAYLOAD)).toBe(true);
  });

  it.each([
    ["another secret", "It's a Secret to Nobody", SIGNATURE],
    ["no signature", SECRET, null],
    ["a SHA-1 signature", SECRET, "sha1=01dc10d0c83e72ed246219cdd91669667fe2ca59"],
    ["a truncated digest", SECRET, SIGNATURE.slice(0, -2)],
    ["no secret", "", SIGNATURE],
  ])("refuses %s", async (_, secret, signature) => {
    expect(await verify(secret, signature, PAYLOAD)).toBe(false);
  });
});
