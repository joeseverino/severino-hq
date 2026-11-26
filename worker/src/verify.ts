// Each signature scheme a provider's route names, mapped to the one function
// that verifies it. A new provider with a new scheme adds an entry here.

/** Whether `signature` (the scheme's header, as sent) signs `body` with `secret`. */
export type Verifier = (secret: string, signature: string | null, body: BufferSource) => Promise<boolean>;

const encoder = new TextEncoder();

/** The bytes of a hex string, or null when it is not exactly that. */
function hexBytes(hex: string): Uint8Array<ArrayBuffer> | null {
  if (hex.length % 2 !== 0 || !/^[0-9a-f]*$/i.test(hex)) return null;
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return bytes;
}

// GitHub signs the exact payload with HMAC-SHA256 and sends `sha256=<hex>`.
// subtle.verify compares in constant time.
const githubHmacSha256: Verifier = async (secret, signature, body) => {
  const digest = signature?.startsWith("sha256=") ? hexBytes(signature.slice("sha256=".length)) : null;
  if (!secret || !digest || digest.length !== 32) return false;
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
    "verify",
  ]);
  return crypto.subtle.verify("HMAC", key, digest, body);
};

export const VERIFIERS: Readonly<Record<string, Verifier>> = {
  "github-hmac-sha256": githubHmacSha256,
};
