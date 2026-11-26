// HQ's inbound events: providers' webhooks in, their names out to the host
// listeners that hold a socket open to the Hub.
//
// Security properties:
// - The Worker holds no route and no credential into HQ. It answers webhooks
//   and holds sockets HQ dials out to, and writes only to its own Hub.
// - A webhook's body is read only when its Content-Length is within the
//   largest its provider sends. Its signature is verified over the raw body,
//   read once, before anything parses it; a request that fails is answered
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

  const name = request.headers.get(route.event_header) ?? "";
  const delivery = request.headers.get(route.delivery_header);
  if (!route.events.includes(name)) return answer(202);
  if (!delivery) return answer(400);
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return answer(400);
  }
  const action = text(pointer(payload, route.action_pointer));
  const event: InboundEvent = {
    provider: route.provider,
    event: action ? `${name}.${action}` : name,
    subject: text(pointer(payload, route.subject_pointer)),
    delivery_id: delivery,
    received_at: new Date().toISOString(),
  };
  if (!withinContract(event)) return answer(400);
  // Answered whether or not a listener is connected: a listener re-reads on
  // every reconnect, which covers what it missed.
  await hub(env).broadcast(event);
  return answer(202);
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
