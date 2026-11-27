import { env, exports } from "cloudflare:workers";
import { afterEach, describe, expect, it, vi } from "vitest";
import arrival from "../src/generated/arrival.json";
import routes from "../src/generated/routes.json";
import { LAST_EVENT_ID, LOG_AGE_MS, LOG_EVENTS, type InboundEvent, type Logged, type Opening } from "../src/hub";
import pullRequest from "./fixtures/pull_request.opened.json";
import ping from "./fixtures/ping.json";
import push from "./fixtures/push.json";
import tailscaleEvents from "./fixtures/tailscale.json";

const github = routes.providers.find((route) => route.provider === "github_app")!;
const tailscale = routes.providers.find((route) => route.provider === "tailscale")!;
const ORIGIN = "https://example.com";

/** A listener: the Hub's opening, then each event its socket has been sent, in order. */
interface Listener {
  opening: Opening;
  /** The next event, under its id. */
  logged(): Promise<Logged>;
  /** The next event's names. */
  next(): Promise<InboundEvent>;
}

const open: WebSocket[] = [];

afterEach(() => {
  for (const socket of open.splice(0)) socket.close(1000);
});

function upgrade(headers: Record<string, string>): Promise<Response> {
  return exports.default.fetch(`${ORIGIN}${routes.socket}`, { headers: { Upgrade: "websocket", ...headers } });
}

type HubStub = ReturnType<Env["HUB"]["getByName"]>;

/**
 * A listener connected with the current token, presenting `lastEventId` (0,
 * none, unless given), through the Worker or straight to `hub`, a Hub of its own.
 */
async function listen(lastEventId: number | string = 0, hub?: HubStub): Promise<Listener> {
  const headers = { Authorization: `Bearer ${env.SOCKET_TOKEN}`, [LAST_EVENT_ID]: String(lastEventId) };
  const response = hub
    ? await hub.fetch(`${ORIGIN}${routes.socket}`, { headers: { Upgrade: "websocket", ...headers } })
    : await upgrade(headers);
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
  const take = () => (arrived.length ? Promise.resolve(arrived.shift()) : new Promise((resolve) => waiting.push(resolve)));
  const opening = (await take()) as Opening;
  const logged = async () => (await take()) as Logged;
  return { opening, logged, next: async () => (await logged()).arrival };
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

  it("of an event HQ does not take reaches the listeners, so HQ records it", async () => {
    const listener = await listen();

    const response = await deliver({ event: "ping", payload: ping, id: "delivery-ping" });

    expect(response.status).toBe(202);
    expect(await listener.next()).toMatchObject({ event: "ping", subject: "", delivery_id: "delivery-ping" });
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

describe("a delivery whose names are outside the contract's bounds", () => {
  const over = (member: "event" | "subject" | "delivery_id") => "x".repeat(arrival.properties[member].maxLength + 1);

  it.each<[string, Omit<Delivery, "signature" | "length">]>([
    ["its event's length", { event: "pull_request", payload: { ...pullRequest, action: over("event") }, id: "delivery-long" }],
    ["its subject's length", { event: "push", payload: { ...push, repository: { ...push.repository, full_name: over("subject") } }, id: "delivery-long" }],
    ["its delivery id's length", { event: "push", payload: push, id: over("delivery_id") }],
    ["an empty event", { event: "", payload: push, id: "delivery-unnamed" }],
    ["an empty delivery id", { event: "push", payload: push, id: "" }],
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
  it("reaches a listener as each event it carries, the console's test event among them, by name alone", async () => {
    const listener = await listen();

    const response = await tailscaleDelivery(tailscaleEvents);

    expect(response.status).toBe(202);
    expect(await listener.next()).toMatchObject({ event: "test", subject: "example.com" });
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
    const first = [await listener.next(), await listener.next(), await listener.next()];
    await tailscaleDelivery(tailscaleEvents, { age: 60 });
    const again = [await listener.next(), await listener.next(), await listener.next()];

    expect(again).toMatchObject(first.map((event) => ({ delivery_id: (event as { delivery_id: string }).delivery_id })));
  });

  it.each<[string, unknown[], { age?: number; signed?: string | null }, number]>([
    ["a wrong signature", tailscaleEvents, { signed: `t=${Math.floor(Date.now() / 1000)},v1=${"0".repeat(64)}` }, 401],
    ["a signature older than its window", tailscaleEvents, { age: tailscale.scheme.window_seconds + 60 }, 401],
    ["no signature", tailscaleEvents, { signed: null }, 401],
    ["an event with no type", [{ ...tailscaleEvents[1], type: undefined }], {}, 400],
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

  it("opened with a token since rotated is closed at once and is sent nothing of the log", async () => {
    const current = await listen();
    await deliver({ event: "push", payload: push, id: "delivery-before-rotation" });
    const { id } = await current.logged();
    await deliver({ event: "push", payload: push, id: "delivery-missed" });
    // The Hub as a new version finds it: a socket the Worker let through
    // under the token it held before, presenting an id the log holds.
    const opened = await env.HUB.getByName("hub").fetch(`${ORIGIN}${routes.socket}`, {
      headers: { Upgrade: "websocket", Authorization: "Bearer example-rotated-token", [LAST_EVENT_ID]: String(id) },
    });
    const stale = opened.webSocket!;
    const heard: unknown[] = [];
    stale.addEventListener("message", (message) => heard.push(message.data));
    const closed = new Promise<number>((resolve) => stale.addEventListener("close", (close) => resolve(close.code)));
    stale.accept();
    open.push(stale);

    expect(await closed).toBe(1008);
    expect(heard).toStrictEqual([]);
  });
});

/** Sends `event` through `hub` as the Worker does once it has verified it. */
const broadcast = (event: InboundEvent, hub: HubStub = env.HUB.getByName("hub")) => hub.broadcast(event);

const named = (delivery_id: string, received_at = new Date().toISOString()): InboundEvent => ({
  provider: "github_app",
  event: "push",
  subject: "example/hq",
  delivery_id,
  received_at,
});

describe("a listener that reconnects", () => {
  it("naming the last event it handled is sent exactly the events after it, then the live ones", async () => {
    const before = await listen();
    await deliver({ event: "push", payload: push, id: "delivery-handled" });
    const handled = await before.logged();
    await deliver({ event: "push", payload: push, id: "delivery-missed-1" });
    await deliver({ event: "pull_request", payload: pullRequest, id: "delivery-missed-2" });
    const missed = [await before.logged(), await before.logged()];

    const after = await listen(handled.id);
    await deliver({ event: "push", payload: push, id: "delivery-live" });

    expect(after.opening).toStrictEqual({ last_event_id: missed[1]!.id, resumed: true });
    expect([await after.logged(), await after.logged()]).toStrictEqual(missed);
    expect(await after.next()).toMatchObject({ delivery_id: "delivery-live" });
  });

  it("naming the newest event is sent only the live ones", async () => {
    const before = await listen();
    await deliver({ event: "push", payload: push, id: "delivery-newest" });
    const { id } = await before.logged();

    const after = await listen(id);
    await deliver({ event: "push", payload: push, id: "delivery-next" });

    expect(after.opening).toStrictEqual({ last_event_id: id, resumed: true });
    expect(await after.next()).toMatchObject({ delivery_id: "delivery-next" });
  });

  it("naming an event the log has let go of for its age is told to read again, and sent the live ones", async () => {
    // A Hub of its own, whose log holds only these events.
    const hub = env.HUB.getByName("example-aged");
    const watching = await listen(0, hub);
    const aged = new Date(Date.now() - LOG_AGE_MS - 60_000).toISOString();
    await broadcast(named("delivery-aged-1", aged), hub);
    await broadcast(named("delivery-aged-2", aged), hub);
    await broadcast(named("delivery-fresh"), hub);
    const { id } = await watching.logged();
    const newest = [await watching.logged(), await watching.logged()][1]!;

    const after = await listen(id, hub);
    await broadcast(named("delivery-live"), hub);

    expect(after.opening).toStrictEqual({ last_event_id: newest.id, resumed: false });
    expect(await after.next()).toMatchObject({ delivery_id: "delivery-live" });
  });

  it("naming an event the log has let go of for its count is told to read again", async () => {
    const hub = env.HUB.getByName("example-counted");
    await broadcast(named("delivery-first"), hub);
    const { last_event_id: first } = (await listen(0, hub)).opening;
    for (let sent = 1; sent <= LOG_EVENTS + 1; sent++) await broadcast(named(`delivery-${sent}`), hub);

    const { opening } = await listen(first, hub);

    expect(opening).toStrictEqual({ last_event_id: first + LOG_EVENTS + 1, resumed: false });
  });

  it.each<[string, (newest: number) => string]>([
    ["no event held", () => "0"],
    ["an id the log never gave", (newest) => String(newest + 1)],
    ["an id that is not one", () => "0x10"],
  ])("with %s is told to read again", async (_, presenting) => {
    await broadcast(named("delivery-logged"));
    const { last_event_id: newest } = (await listen()).opening;

    const { opening } = await listen(presenting(newest));

    expect(opening).toStrictEqual({ last_event_id: newest, resumed: false });
  });
});

describe("a listener of the release before the log", () => {
  it("presenting no id is sent each event as it is, and no opening", async () => {
    const response = await upgrade({ Authorization: `Bearer ${env.SOCKET_TOKEN}` });
    const socket = response.webSocket!;
    const heard: unknown[] = [];
    const first = new Promise((resolve) =>
      socket.addEventListener("message", (message) => resolve(heard.push(JSON.parse(String(message.data))))),
    );
    socket.accept();
    open.push(socket);

    await deliver({ event: "push", payload: push, id: "delivery-unlogged" });
    await first;

    expect(heard).toStrictEqual([
      {
        provider: github.provider,
        event: "push",
        subject: "example/hq",
        delivery_id: "delivery-unlogged",
        received_at: expect.any(String),
      },
    ]);
  });
});

describe("a request's log", () => {
  afterEach(() => vi.restoreAllMocks());

  it("is its method, path and status alone, never a header", async () => {
    const logged = vi.spyOn(console, "log").mockImplementation(() => {});

    await listen();
    await deliver({ event: "push", payload: push, id: "delivery-logged" });

    expect(logged.mock.calls).toStrictEqual([
      [{ method: "GET", path: routes.socket, status: 101 }],
      [{ method: "POST", path: github.path, status: 202 }],
    ]);
  });
});
