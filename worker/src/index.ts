// HQ's inbound events: providers' webhooks in, their names and declared
// facts out to the host listeners that hold a socket open to the Hub.
//
// Security properties:
// - The Worker holds no route and no credential into HQ. It answers webhooks
//   and holds sockets HQ dials out to, and writes only to its own Hub.
// - A webhook's body is read only when its Content-Length is within the
//   largest its provider sends. Its signature is verified over the raw body,
//   read once, before anything parses it, by the one check every route's
//   scheme goes through (verify.ts), which compares in constant time. A
//   scheme that signs its time is refused outside its window, so a captured
//   request is not accepted again later. A shared secret signs nothing: it
//   proves the sender holds it, and a body it carries, replayed or not, is
//   only a name to forward. A request that fails is answered 401 and never
//   reaches the Hub.
// - A socket is accepted only with the SOCKET_TOKEN bearer, compared in
//   constant time before the upgrade reaches the Hub, and an event reaches
//   only a socket that presented the token the Worker holds now, so a
//   rotation closes every socket opened with the old one.
// - Every event a verified request carries is forwarded, one HQ takes or
//   not: HQ records one it does not take and reads nothing for it. An
//   event's names are forwarded (provider, event, subject, delivery id,
//   time), and, for an event its declaration gives a fact, the text at each
//   of the fact's pointers into the signed body; never the body. A valid
//   sender can make HQ re-read with its own credentials, and show a told
//   state its fact says until that read, never change what HQ read. An
//   event outside the bridge contract's bounds (a name or delivery id empty
//   or too long, a fact with too many values or one too long) is answered
//   400 and never sent.
// - The Hub logs each event it sends, as the socket carries it, for a day at
//   most, so a listener that reconnects is sent what it missed. Only a
//   socket of the current token is sent the log.
// - Every secret is a Worker secret, named by the route that uses it.
// - A request is logged as its method, path and status alone, never a header
//   or the body, and counted in Analytics Engine by its path and status alone.
import routes from "./generated/routes.json";
import { bearer, Hub, withinContract, type InboundEvent } from "./hub";
import { sameSecret, verify } from "./verify";

export { Hub };

type Route = (typeof routes.providers)[number];

/** Where in an event's body its fact is (InboundSpec.facts). */
interface Fact {
  values: Record<string, string>;
  about: string;
  said_at: string[];
}

const ROUTES = new Map(routes.providers.map((route): [string, Route] => [route.path, route]));

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

/**
 * What the body says of the event named `name`, the action after its dot, by
 * the fact its route declares for that event or its name: the text at each
 * pointer, a value the body leaves empty left out. Nothing for an event with
 * no fact.
 */
function told(route: Route, name: string, item: unknown): Pick<InboundEvent, "about" | "said_at" | "fact"> {
  const facts: Record<string, Fact> = route.facts;
  const fact = facts[name] ?? facts[name.split(".")[0] ?? ""];
  if (!fact) return {};
  const at = (path: string) => text(pointer(item, path));
  return {
    about: at(fact.about),
    said_at: fact.said_at.map(at).find(Boolean) ?? "",
    fact: Object.fromEntries(
      Object.entries(fact.values).flatMap(([field, path]) => (at(path) ? [[field, at(path)]] : [])),
    ),
  };
}

/** Whether `header` is `Bearer <token>` for a configured token, compared in constant time. */
async function bearerMatches(header: string | null, token: string): Promise<boolean> {
  const presented = bearer(header);
  return presented !== null && sameSecret(presented, token);
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
  // A provider that names no delivery has it named by the body it signed, so
  // the same body sent again is the event already stored.
  const delivery = route.delivery_header
    ? (header(route.delivery_header) ?? "")
    : new Uint8Array(await crypto.subtle.digest("SHA-256", body)).toHex();
  const received_at = new Date().toISOString();
  // Every event a verified request carries is sent, one HQ takes or not, so
  // a provider's test event or a new event type shows in HQ.
  const events = sent.map((item: unknown, index): InboundEvent => {
    const name = named ?? (text(pointer(item, route.event_pointer)) || route.unnamed_event);
    const action = route.action_pointer ? text(pointer(item, route.action_pointer)) : "";
    const event = action ? `${name}.${action}` : name;
    return {
      provider: route.provider,
      event,
      subject: route.subject_pointer ? text(pointer(item, route.subject_pointer)) : "",
      delivery_id: route.batched ? `${delivery}.${index}` : delivery,
      received_at,
      ...told(route, event, item),
    };
  });
  const within = events.filter(withinContract);
  // Answered whether or not a listener is connected: the Hub logs each
  // event, which a listener that reconnects is sent.
  for (const event of within) await hub(env).broadcast(event);
  return answer(within.length === events.length ? 202 : 400);
}

function answered(request: Request, env: Env, path: string): Promise<Response> | Response {
  if (path === routes.socket) return socket(request, env);
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
    // What HQ counts the Worker's answers by, a refused signature among them.
    // The path is the index, so sampling keeps a quiet route's answers whole,
    // and it is always a route's: the Worker runs for its routes alone.
    env.ANSWERS.writeDataPoint({ indexes: [path], blobs: [path], doubles: [response.status] });
    return response;
  },
} satisfies ExportedHandler<Env>;
