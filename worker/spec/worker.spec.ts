import { env, exports } from "cloudflare:workers";
import { afterEach, describe, expect, it, vi } from "vitest";
import arrival from "../src/generated/arrival.json";
import routes from "../src/generated/routes.json";
import { SOCKET_PATH } from "../src/hub";
import pullRequest from "./fixtures/pull_request.opened.json";
import push from "./fixtures/push.json";
import tailscaleEvents from "./fixtures/tailscale.json";

const github = routes.providers.find((route) => route.provider === "github_app")!;
const tailscale = routes.providers.find((route) => route.provider === "tailscale")!;
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

/** The HMAC-SHA256 of `message` under `secret`, in hex. */
async function hmac(secret: string, message: string): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
  ]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(message))).toHex();
}

const signature = async (body: string) => `sha256=${await hmac(env.GITHUB_WEBHOOK_SECRET, body)}`;

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
  if (signed !== null) headers[github.scheme.header] = signed;
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

describe("a delivery whose names exceed the contract's bounds", () => {
  const over = (member: "event" | "subject" | "delivery_id") => "x".repeat(arrival.properties[member].maxLength + 1);

  it.each<[string, Omit<Delivery, "signature" | "length">]>([
    ["its event", { event: "pull_request", payload: { ...pullRequest, action: over("event") }, id: "delivery-long" }],
    ["its subject", { event: "push", payload: { ...push, repository: { ...push.repository, full_name: over("subject") } }, id: "delivery-long" }],
    ["its delivery id", { event: "push", payload: push, id: over("delivery_id") }],
  ])("is refused alone by %s", async (_, delivery) => {
    const listener = await listen();

    const refused = await deliver(delivery);
    await deliver({ event: "push", payload: push, id: "delivery-after" });

    expect(refused.status).toBe(400);
    expect(await listener.next()).toMatchObject({ delivery_id: "delivery-after" });
  });
});

/**
 * POSTs `events` as Tailscale sends them (tailscale.com/kb/1213/webhooks): one
 * JSON array, signed "t=<time>,v1=<hex>" over "<time>.<body>", `age` seconds
 * ago, unless `signed` gives the header or null for none.
 */
async function tailscaleDelivery(events: unknown[], { age = 0, signed }: { age?: number; signed?: string | null } = {}) {
  const body = JSON.stringify(events);
  const at = Math.floor(Date.now() / 1000) - age;
  const header = signed === undefined ? `t=${at},v1=${await hmac(env.TAILSCALE_WEBHOOK_SECRET, `${at}.${body}`)}` : signed;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (header !== null) headers[tailscale.scheme.header] = header;
  return exports.default.fetch(`${ORIGIN}${tailscale.path}`, { method: "POST", headers, body });
}

describe("a Tailscale delivery", () => {
  it("reaches a listener as each event it takes, by name alone", async () => {
    const listener = await listen();

    const response = await tailscaleDelivery(tailscaleEvents);

    expect(response.status).toBe(202);
    const created = await listener.next();
    const needs = await listener.next();
    expect(created).toStrictEqual({
      provider: "tailscale",
      event: "nodeCreated",
      subject: "example.com",
      delivery_id: expect.any(String),
      received_at: expect.any(String),
    });
    expect(needs).toMatchObject({ event: "nodeNeedsApproval", subject: "example.com" });
    expect((created as { delivery_id: string }).delivery_id).not.toBe((needs as { delivery_id: string }).delivery_id);
  });

  it("sent again is each event under the delivery id it first had", async () => {
    const listener = await listen();

    await tailscaleDelivery(tailscaleEvents);
    const first = [await listener.next(), await listener.next()];
    await tailscaleDelivery(tailscaleEvents, { age: 60 });
    const again = [await listener.next(), await listener.next()];

    expect(again).toMatchObject(first.map((event) => ({ delivery_id: (event as { delivery_id: string }).delivery_id })));
  });

  // Tailscale counts a delivery answered 3xx, 4xx or 5xx as failed, so an
  // event the route does not list is answered as delivered.
  it.each<[string, unknown[], { age?: number; signed?: string | null }, number]>([
    ["a wrong signature", tailscaleEvents, { signed: `t=${Math.floor(Date.now() / 1000)},v1=${"0".repeat(64)}` }, 401],
    ["a signature older than its window", tailscaleEvents, { age: tailscale.scheme.window_seconds + 60 }, 401],
    ["no signature", tailscaleEvents, { signed: null }, 401],
    ["only events the route does not list", [tailscaleEvents[0], { ...tailscaleEvents[0], type: "webhookUpdated" }], {}, 202],
  ])("with %s never reaches a listener", async (_, events, delivery, status) => {
    const listener = await listen();

    const refused = await tailscaleDelivery(events, delivery);
    await tailscaleDelivery([{ ...tailscaleEvents[1], type: "policyUpdate" }]);

    expect(refused.status).toBe(status);
    expect(await listener.next()).toMatchObject({ event: "policyUpdate" });
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

describe("a request's log", () => {
  afterEach(() => vi.restoreAllMocks());

  it("is its method, path and status alone, never a header", async () => {
    const logged = vi.spyOn(console, "log").mockImplementation(() => {});

    await listen();
    await deliver({ event: "push", payload: push, id: "delivery-logged" });

    expect(logged.mock.calls).toStrictEqual([
      [{ method: "GET", path: SOCKET_PATH, status: 101 }],
      [{ method: "POST", path: github.path, status: 202 }],
    ]);
  });
});
