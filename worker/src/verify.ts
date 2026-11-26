// Each signature scheme a provider's route names, mapped to the one function
// that verifies it. A new provider with a new scheme adds an entry here.

/** Whether `signature` (the scheme's header, as sent) signs `body` with `secret`. */
export type Verifier = (secret: string, signature: string | null, body: BufferSource) => Promise<boolean>;

const encoder = new TextEncoder();

/** The bytes a `<prefix><hex>` signature carries, or null when it is not exactly that. */
function digestOf(signature: string | null, prefix: string): Uint8Array<ArrayBuffer> | null {
  if (!signature?.startsWith(prefix)) return null;
  try {
    return Uint8Array.fromHex(signature.slice(prefix.length));
  } catch {
    return null;
  }
}

// GitHub signs the exact payload with HMAC-SHA256 and sends `sha256=<hex>`.
// subtle.verify compares in constant time.
const githubHmacSha256: Verifier = async (secret, signature, body) => {
  const digest = digestOf(signature, "sha256=");
  if (!secret || !digest || digest.length !== 32) return false;
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
    "verify",
  ]);
  return crypto.subtle.verify("HMAC", key, digest, body);
};

export const VERIFIERS: Readonly<Record<string, Verifier>> = {
  "github-hmac-sha256": githubHmacSha256,
};
