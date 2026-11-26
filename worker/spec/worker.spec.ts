import { env, exports } from "cloudflare:workers";
import { afterEach, describe, expect, it } from "vitest";
import routes from "../src/generated/routes.json";
import { SOCKET_PATH } from "../src/hub";
import pullRequest from "./fixtures/pull_request.opened.json";
import push from "./fixtures/push.json";

const github = routes.providers.find((route) => route.provider === "github_app")!;
const ORIGIN = "https://example.com";

/** A listener: each message its socket has been sent, in order. */
interface Listener {
  next(): Promise<unknown>;
}

const open: WebSocket[] = [];

afterEach(() => {
  for (const socket of open.splice(0)) socket.close(1000);
});

function upgrade(headers: Record<string, string>): Promise<Response> {
  return exports.default.fetch(`${ORIGIN}${SOCKET_PATH}`, { headers: { Upgrade: "websocket", ...headers } });
}

async function listen(): Promise<Listener> {
  const response = await upgrade({ Authorization: `Bearer ${env.SOCKET_TOKEN}` });
  expect(response.status).toBe(101);
  const socket = response.webSocket!;
  socket.accept();
  open.push(socket);
  const arrived: unknown[] = [];
  const waiting: ((message: unknown) => void)[] = [];
  socket.addEventListener("message", (message) => {
    const data: unknown = JSON.parse(String(message.data));
    const waiter = waiting.shift();
    if (waiter) waiter(data);
    else arrived.push(data);
  });
  return {
    next: () => (arrived.length ? Promise.resolve(arrived.shift()) : new Promise((resolve) => waiting.push(resolve))),
  };
}

async function signature(body: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(env.GITHUB_WEBHOOK_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body)));
  return `sha256=${Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

interface Delivery {
  event: string;
  payload: unknown;
  id: string;
  signature?: string | null;
  /** The Content-Length it states: the body's own unless given, none when null. */
  length?: string | null;
}

/**
 * POSTs a delivery as GitHub sends one, its length stated and signed with the
 * Worker's secret, unless `length` or `signature` says otherwise.
 */
async function deliver({ event, payload, id, signature: sent, length }: Delivery): Promise<Response> {
  const body = JSON.stringify(payload);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "User-Agent": "GitHub-Hookshot/example",
    [github.event_header]: event,
    [github.delivery_header]: id,
  };
  const signed = sent === undefined ? await signature(body) : sent;
  if (signed !== null) headers[github.signature_header] = signed;
  if (length) headers["Content-Length"] = length;
  // A string states its own length; a stream, as a chunked request sends it, none.
  const streamed = length === null ? new Response(body).body!.pipeThrough(new TransformStream()) : body;
  return exports.default.fetch(`${ORIGIN}${github.path}`, { method: "POST", headers, body: streamed });
}

describe("a GitHub delivery", () => {
  it("reaches every listener as its metadata alone", async () => {
    const listeners = [await listen(), await listen()];

    const response = await deliver({ event: "push", payload: push, id: "delivery-push" });

    expect(response.status).toBe(202);
    for (const listener of listeners) {
      const message = await listener.next();
      expect(message).toStrictEqual({
        provider: github.provider,
        event: "push",
        subject: "example/hq",
        delivery_id: "delivery-push",
        received_at: expect.any(String),
      });
      const { received_at } = message as { received_at: string };
      expect(new Date(received_at).toISOString()).toBe(received_at);
    }
  });

  it("names its event with the payload's action", async () => {
    const listener = await listen();

    await deliver({ event: "pull_request", payload: pullRequest, id: "delivery-pr" });

    expect(await listener.next()).toMatchObject({ event: "pull_request.opened", delivery_id: "delivery-pr" });
  });

  it("is answered 202 with no listener connected", async () => {
    const response = await deliver({ event: "push", payload: push, id: "delivery-alone" });

    expect(response.status).toBe(202);
  });
});

describe("a delivery that never reaches the listeners", () => {
  // Each is followed by a valid delivery, so the first message a listener
  // gets shows the refused one was never sent.
  it.each<[string, Omit<Delivery, "payload" | "id">, number]>([
    ["a wrong signature", { event: "push", signature: "sha256=" + "0".repeat(64) }, 401],
    ["a signature of another body", { event: "push", signature: "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17" }, 401],
    ["no signature", { event: "push", signature: null }, 401],
    ["an event the route does not list", { event: "issues" }, 202],
    ["a length over the provider's largest body", { event: "push", length: String(github.max_body_bytes + 1) }, 413],
    ["no length", { event: "push", length: null }, 411],
  ])("%s", async (_, delivery, status) => {
    const listener = await listen();

    const refused = await deliver({ ...delivery, payload: push, id: "delivery-refused" });
    await deliver({ event: "push", payload: push, id: "delivery-after" });

    expect(refused.status).toBe(status);
    expect(await listener.next()).toMatchObject({ delivery_id: "delivery-after" });
  });
});

describe("a listener's socket", () => {
  it.each([
    ["no bearer token", {}],
    ["a wrong bearer token", { Authorization: "Bearer example-wrong-token" }],
    ["the token without its scheme", { Authorization: env.SOCKET_TOKEN }],
  ])("is refused with %s", async (_, headers) => {
    const response = await upgrade(headers);

    expect(response.status).toBe(401);
    expect(response.webSocket).toBeNull();
  });

  it("opened with a token since rotated is closed at the next event and hears nothing", async () => {
    // The Hub as a new version finds it: a socket the Worker let through
    // under the token it held before.
    const opened = await env.HUB.getByName("hub").fetch(`${ORIGIN}${SOCKET_PATH}`, {
      headers: { Upgrade: "websocket", Authorization: "Bearer example-rotated-token" },
    });
    const stale = opened.webSocket!;
    stale.accept();
    open.push(stale);
    const heard: unknown[] = [];
    stale.addEventListener("message", (message) => heard.push(message.data));
    const closed = new Promise<number>((resolve) => stale.addEventListener("close", (close) => resolve(close.code)));
    const current = await listen();

    await deliver({ event: "push", payload: push, id: "delivery-rotated" });

    expect(await current.next()).toMatchObject({ delivery_id: "delivery-rotated" });
    expect(await closed).toBe(1008);
    expect(heard).toStrictEqual([]);
  });
});
