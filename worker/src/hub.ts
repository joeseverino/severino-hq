import { DurableObject } from "cloudflare:workers";
import arrival from "./generated/arrival.json";

/** The path a host listener opens its socket to the Hub on. */
export const SOCKET_PATH = "/socket";

/**
 * What reaches a listener for one verified delivery: its names, never its
 * body, as the bridge contract's InboundArrival has them.
 */
export type InboundEvent = Record<keyof typeof arrival.properties, string>;

/**
 * Whether the event is named, as HQ stores only a named event under a named
 * delivery, and each member is within the length the contract bounds it to,
 * in code points as JSON Schema counts them. HQ refuses a batch holding one
 * event outside it, so such an event is refused here, alone.
 */
export function withinContract(event: InboundEvent): boolean {
  return (
    Boolean(event.event && event.delivery_id) &&
    Object.entries(arrival.properties).every(
      ([member, shape]) => !("maxLength" in shape) || [...event[member as keyof InboundEvent]].length <= shape.maxLength,
    )
  );
}

const encoder = new TextEncoder();

const ROTATED = "The socket token has changed";

/** A logged event's message, from the names the log holds as JSON. */
const logged = (id: number, names: string): string =>
  JSON.stringify({ id, arrival: JSON.parse(names) as InboundEvent } satisfies Logged);

/** The token a `Bearer <token>` header presents, or null when it presents none. */
export function bearer(header: string | null): string | null {
  return header?.startsWith("Bearer ") ? header.slice("Bearer ".length) : null;
}

/** A socket token's tag: its SHA-256 in hex, so the Hub holds no token and every tag is one length. */
export async function tag(token: string): Promise<string> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(token))).toHex();
}

/** The request header a reconnecting listener names the last event it handled by, as Server-Sent Events do. */
export const LAST_EVENT_ID = "Last-Event-ID";

/**
 * The log's window. HQ hears about fifty events an hour from its providers,
 * a few hundred in a burst of pushes, and a listener is away for a restart
 * or a dropped connection, seconds to minutes. A day of events covers an
 * overnight outage with room to spare, and one away longer reads everything
 * again, which a day's worth of names would ask of HQ anyway.
 */
export const LOG_EVENTS = 2000;
export const LOG_AGE_MS = 24 * 60 * 60 * 1000;

/**
 * The first message on every socket: the newest id the log holds, and
 * whether the events after the id the listener presented follow it. When
 * they do not, the listener reads everything again, and the events after
 * `last_event_id` follow.
 */
export interface Opening {
  last_event_id: number;
  resumed: boolean;
}

/** Every later message: one event, under the id the log holds it by. */
export interface Logged {
  id: number;
  arrival: InboundEvent;
}

const MEMBERS = Object.keys(arrival.properties) as (keyof InboundEvent)[];

/** An id a listener can present: a positive integer JavaScript holds exactly. */
const ID = /^[1-9]\d{0,14}$/;

/**
 * The one object every listener's socket is held by. It hibernates between
 * events (acceptWebSocket, no alarm; its storage is written only as an event
 * arrives), and the runtime answers a listener's protocol pings without
 * waking it. The Worker authenticates each socket and verifies each event
 * before either reaches it.
 *
 * Each event it sends is appended to a log in its SQLite storage under an id
 * that only grows, so a listener that reconnects naming the last id it
 * handled is sent exactly the events after it, then the live ones. The log
 * holds the contract's members of each event alone, the names the socket
 * carries, never a body. It keeps the last LOG_EVENTS events of the last
 * LOG_AGE_MS, and always the newest, so its ids keep growing; it is cut from
 * its oldest end alone, so an id within it has every event after it.
 *
 * A hibernated socket outlives a new version of the Worker, a rotated
 * SOCKET_TOKEN included, so each socket is tagged with the token it
 * presented, and the log and each event go only to the sockets of the token
 * the Hub holds now; every other socket is closed.
 */
export class Hub extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY, arrival TEXT NOT NULL) STRICT");
  }

  override async fetch(request: Request): Promise<Response> {
    const [presented, current] = await Promise.all([
      tag(bearer(request.headers.get("Authorization")) ?? ""),
      tag(this.env.SOCKET_TOKEN),
    ]);
    const { 0: client, 1: server } = new WebSocketPair();
    this.ctx.acceptWebSocket(server, [presented]);
    // Sent before anything else can run here, so no event falls between the
    // log and the live ones.
    if (presented !== current) server.close(1008, ROTATED);
    else for (const message of this.since(request.headers.get(LAST_EVENT_ID))) server.send(message);
    return new Response(null, { status: 101, webSocket: client });
  }

  /** The opening, then the events after `presented` when the log holds every one. */
  private since(presented: string | null): string[] {
    this.trim();
    const { oldest, newest } = this.ctx.storage.sql
      .exec<{ oldest: number | null; newest: number | null }>(
        "SELECT (SELECT MIN(id) FROM log) AS oldest, (SELECT MAX(id) FROM log) AS newest",
      )
      .one();
    const after = presented && ID.test(presented) ? Number(presented) : 0;
    const resumed = oldest !== null && newest !== null && after > 0 && after >= oldest - 1 && after <= newest;
    const opening: Opening = { last_event_id: newest ?? 0, resumed };
    if (!resumed) return [JSON.stringify(opening)];
    const missed = this.ctx.storage.sql
      .exec<{ id: number; arrival: string }>("SELECT id, arrival FROM log WHERE id > ? ORDER BY id", after)
      .toArray();
    return [JSON.stringify(opening), ...missed.map(({ id, arrival }) => logged(id, arrival))];
  }

  /** Cuts the log to its window from its oldest end, keeping the newest. */
  private trim(): void {
    const sql = this.ctx.storage.sql;
    sql.exec("DELETE FROM log WHERE id <= (SELECT MAX(id) FROM log) - ?", LOG_EVENTS);
    sql.exec(
      `DELETE FROM log WHERE id < COALESCE(
         (SELECT id FROM log WHERE json_extract(arrival, '$.received_at') >= ? ORDER BY id LIMIT 1),
         (SELECT MAX(id) FROM log))`,
      new Date(Date.now() - LOG_AGE_MS).toISOString(),
    );
  }

  /** Logs the event, then sends it to every listener holding the current token, and closes the rest. */
  async broadcast(event: InboundEvent): Promise<void> {
    const current = await tag(this.env.SOCKET_TOKEN);
    const names = JSON.stringify(Object.fromEntries(MEMBERS.map((member) => [member, event[member]])));
    const { id } = this.ctx.storage.sql
      .exec<{ id: number }>("INSERT INTO log (arrival) VALUES (?) RETURNING id", names)
      .one();
    this.trim();
    const message = logged(id, names);
    for (const socket of this.ctx.getWebSockets()) {
      try {
        if (this.ctx.getTags(socket).includes(current)) socket.send(message);
        else socket.close(1008, ROTATED);
      } catch {
        // A socket closing as the event arrives misses it; the log sends it
        // when its listener reconnects.
      }
    }
  }

  // The channel runs one way, and each message a listener sends wakes the
  // object and is billed, so a sender is closed.
  override webSocketMessage(socket: WebSocket): void {
    socket.close(1003, "This channel only sends");
  }

  // Completes the closing handshake the listener began.
  override webSocketClose(socket: WebSocket, code: number, reason: string): void {
    if (code >= 1000 && code < 5000 && ![1004, 1005, 1006, 1015].includes(code)) socket.close(code, reason);
    else socket.close();
  }
}
