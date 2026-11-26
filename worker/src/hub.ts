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
 * Whether each member is within the length the contract bounds it to, in
 * code points as JSON Schema counts them. HQ refuses a batch holding one
 * event outside it, so such an event is refused here, alone.
 */
export function withinContract(event: InboundEvent): boolean {
  return Object.entries(arrival.properties).every(
    ([member, shape]) => !("maxLength" in shape) || [...event[member as keyof InboundEvent]].length <= shape.maxLength,
  );
}

const encoder = new TextEncoder();

/** The token a `Bearer <token>` header presents, or null when it presents none. */
export function bearer(header: string | null): string | null {
  return header?.startsWith("Bearer ") ? header.slice("Bearer ".length) : null;
}

/** A socket token's tag: its SHA-256 in hex, so the Hub holds no token and every tag is one length. */
export async function tag(token: string): Promise<string> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(token))).toHex();
}

/**
 * The one object every listener's socket is held by. It hibernates between
 * events (acceptWebSocket, no storage, no alarm), and the runtime answers a
 * listener's protocol pings without waking it. The Worker authenticates each
 * socket and verifies each event before either reaches it.
 *
 * A hibernated socket outlives a new version of the Worker, a rotated
 * SOCKET_TOKEN included, so each socket is tagged with the token it
 * presented, and an event goes only to the sockets of the token the Hub
 * holds now; every other socket is closed.
 */
export class Hub extends DurableObject<Env> {
  override async fetch(request: Request): Promise<Response> {
    const { 0: client, 1: server } = new WebSocketPair();
    this.ctx.acceptWebSocket(server, [await tag(bearer(request.headers.get("Authorization")) ?? "")]);
    return new Response(null, { status: 101, webSocket: client });
  }

  /** Sends the event to every listener holding the current token, and closes the rest. */
  async broadcast(event: InboundEvent): Promise<void> {
    const message = JSON.stringify(event);
    const current = await tag(this.env.SOCKET_TOKEN);
    for (const socket of this.ctx.getWebSockets()) {
      try {
        if (this.ctx.getTags(socket).includes(current)) socket.send(message);
        else socket.close(1008, "The socket token has changed");
      } catch {
        // A socket closing as the event arrives misses it; the listener's
        // re-read on reconnect covers what it missed.
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
