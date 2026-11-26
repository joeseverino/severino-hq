// HQ's inbound events: providers' webhooks in, their names out to the host
// listeners that hold a socket open to the Hub.
//
// Security properties:
// - The Worker holds no route and no credential into HQ. It answers webhooks
//   and holds sockets HQ dials out to, and writes only to its own Hub.
// - A webhook's body is read only when its Content-Length is within the
//   largest its provider sends. Its signature is verified over the raw body,
//   read once, before anything parses it, by the one check every route's
//   scheme goes through (verify.ts), which compares in constant time. A
//   scheme that signs its time is refused outside its window, so a captured
//   request is not accepted again later. A request that fails is answered
//   401 and never reaches the Hub.
// - A socket is accepted only with the SOCKET_TOKEN bearer, compared in
//   constant time before the upgrade reaches the Hub, and an event reaches
//   only a socket that presented the token the Worker holds now, so a
//   rotation closes every socket opened with the old one.
// - Only an event's names are forwarded (provider, event, subject, delivery
//   id, time), never its body, so a valid sender can only make HQ re-read
//   with its own credentials. An event outside the bridge contract's bounds
//   is answered 400 and never sent.
// - Every secret is a Worker secret, named by the route that uses it.
// - A request is logged as its method, path and status alone, never a header
//   or the body.
import routes from "./generated/routes.json";
import { bearer, Hub, SOCKET_PATH, tag, withinContract, type InboundEvent } from "./hub";
import { verify } from "./verify";

export { Hub };

type Route = (typeof routes.providers)[number];

const ROUTES = new Map(routes.providers.map((route): [string, Route] => [route.path, route]));

const encoder = new TextEncoder();

/** The value at an RFC 6901 pointer, or undefined where the path ends. */
function pointer(document: unknown, path: string): unknown {
  return path
    .split("/")
    .slice(1)
    .map((token) => token.replaceAll("~1", "/").replaceAll("~0", "~"))
    .reduce<unknown>(
      (node, token) => (node !== null && typeof node === "object" ? (node as Record<string, unknown>)[token] : undefined),
      document,
    );
}

const text = (value: unknown): string => (typeof value === "string" ? value : "");

/** Whether `header` is `Bearer <token>` for a configured token, compared in constant time. */
async function bearerMatches(header: string | null, token: string): Promise<boolean> {
  const presented = bearer(header);
  if (!token || presented === null) return false;
  // Tags of equal length, so the comparison reveals neither value's length.
  return crypto.subtle.timingSafeEqual(encoder.encode(await tag(presented)), encoder.encode(await tag(token)));
}

const answer = (status: number): Response => new Response(null, { status });

/** The one Hub every listener and every event meets at. */
const hub = (env: Env) => env.HUB.getByName("hub");

async function socket(request: Request, env: Env): Promise<Response> {
  if (request.method !== "GET") return answer(405);
  if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") return answer(426);
  if (!(await bearerMatches(request.headers.get("Authorization"), env.SOCKET_TOKEN))) return answer(401);
  return hub(env).fetch(request);
}

async function webhook(request: Request, env: Env, route: Route): Promise<Response> {
  if (request.method !== "POST") return answer(405);
  // Bounded before the body is read: a provider states its length, and
  // sends nothing larger than its declared cap.
  const length = request.headers.get("Content-Length") ?? "";
  if (!/^\d+$/.test(length)) return answer(411);
  if (Number(length) > route.max_body_bytes) return answer(413);
  const body = new Uint8Array(await request.arrayBuffer());
  const secret = (env as unknown as Record<string, unknown>)[route.secret_binding];
  if (!(await verify(route.scheme, text(secret), request.headers.get(route.scheme.header), body))) return answer(401);

  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return answer(400);
  }
  const sent: unknown = route.batched ? payload : [payload];
  if (!Array.isArray(sent)) return answer(400);
  const header = (name: string) => (name ? request.headers.get(name) : null);
  const named = header(route.event_header);
  const taken = sent
    .map((item: unknown, index) => ({ item, index, name: named ?? text(pointer(item, route.event_pointer)) }))
    .filter(({ name }) => route.events.includes(name));
  if (!taken.length) return answer(202);
  // A provider that names no delivery has it named by the body it signed, so
  // the same body sent again is the event already stored.
  const delivery = route.delivery_header
    ? header(route.delivery_header)
    : new Uint8Array(await crypto.subtle.digest("SHA-256", body)).toHex();
  if (!delivery) return answer(400);
  const received_at = new Date().toISOString();
  const events = taken.map(({ item, index, name }): InboundEvent => {
    const action = route.action_pointer ? text(pointer(item, route.action_pointer)) : "";
    return {
      provider: route.provider,
      event: action ? `${name}.${action}` : name,
      subject: route.subject_pointer ? text(pointer(item, route.subject_pointer)) : "",
      delivery_id: route.batched ? `${delivery}.${index}` : delivery,
      received_at,
    };
  });
  const within = events.filter(withinContract);
  // Answered whether or not a listener is connected: a listener re-reads on
  // every reconnect, which covers what it missed.
  for (const event of within) await hub(env).broadcast(event);
  return answer(within.length === events.length ? 202 : 400);
}

function answered(request: Request, env: Env, path: string): Promise<Response> | Response {
  if (path === SOCKET_PATH) return socket(request, env);
  const route = ROUTES.get(path);
  return route ? webhook(request, env, route) : answer(404);
}

export default {
  async fetch(request, env): Promise<Response> {
    const path = new URL(request.url).pathname;
    const response = await answered(request, env, path);
    // The one record a request leaves in Workers Logs, whose invocation logs
    // are off since they keep the request's headers, the bearer among them.
    console.log({ method: request.method, path, status: response.status });
    return response;
  },
} satisfies ExportedHandler<Env>;
