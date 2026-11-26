// The one check of a provider's signature. Each route names its scheme as
// data (InboundSpec.scheme in HQ's provider_spec.py), so a provider with a
// new scheme declares it there and no verifier is written for it.

/** How a provider signs: an HMAC-SHA256 in hex, in a header of `key=value` parts. */
export interface Scheme {
  header: string;
  /** The part that holds a digest; any one of several matching passes. */
  digest: string;
  /** The part that holds the Unix time it was signed at, signed as `<time>.<body>`; "" for none. */
  timestamp: string;
  /** How far that time may be from now, either way. */
  window_seconds: number;
}

const encoder = new TextEncoder();

/** The header's parts, or null when one is not exactly `key=value`. */
function parts(header: string): [string, string][] | null {
  const split = header.split(",").map((part) => part.split("="));
  return split.every((pair): pair is [string, string] => pair.length === 2) ? split : null;
}

/** The bytes a hex digest carries, or null when it is not 32 of them. */
function digestOf(hex: string): Uint8Array<ArrayBuffer> | null {
  try {
    const digest = Uint8Array.fromHex(hex);
    return digest.length === 32 ? digest : null;
  } catch {
    return null;
  }
}

/** What the scheme signs: the body, or the time sent, a dot and the body; null when the time is missing or outside the window. */
function message(scheme: Scheme, found: [string, string][], body: Uint8Array, now: number): Uint8Array | null {
  if (!scheme.timestamp) return body;
  const times = found.filter(([key]) => key === scheme.timestamp).map(([, value]) => value);
  const sent = times.length === 1 ? times[0]! : "";
  if (!/^\d{1,12}$/.test(sent) || Math.abs(now / 1000 - Number(sent)) > scheme.window_seconds) return null;
  const prefix = encoder.encode(`${sent}.`);
  const signed = new Uint8Array(prefix.length + body.length);
  signed.set(prefix);
  signed.set(body, prefix.length);
  return signed;
}

/**
 * Whether `header`, as `scheme` reads it, signs `body` with `secret` at a
 * time within the scheme's window of `now` (milliseconds since the epoch).
 * The HMAC is computed once, and each digest compared with it in constant
 * time.
 */
export async function verify(
  scheme: Scheme,
  secret: string,
  header: string | null,
  body: Uint8Array,
  now: number = Date.now(),
): Promise<boolean> {
  const found = header ? parts(header) : null;
  if (!secret || !found) return false;
  const digests = found.filter(([key]) => key === scheme.digest).map(([, value]) => digestOf(value));
  const signed = message(scheme, found, body, now);
  if (!signed || !digests.length || digests.includes(null)) return false;
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
  ]);
  const expected = await crypto.subtle.sign("HMAC", key, signed);
  return digests.some((digest) => crypto.subtle.timingSafeEqual(digest!, expected));
}
