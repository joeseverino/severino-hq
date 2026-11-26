import { DurableObject } from "cloudflare:workers";

/** The path a host listener opens its socket to the Hub on. */
export const SOCKET_PATH = "/socket";

/** What reaches a listener for one verified delivery: its names, never its body. */
export interface InboundEvent {
  provider: string;
  event: string;
  subject: string;
  delivery_id: string;
  received_at: string;
}

/**
 * The one object every listener's socket is held by. It hibernates between
 * events (acceptWebSocket, no storage, no alarm), and the runtime answers a
 * listener's protocol pings without waking it. The Worker authenticates each
 * socket and verifies each event before either reaches it.
 */
export class Hub extends DurableObject<Env> {
  override async fetch(): Promise<Response> {
    const { 0: client, 1: server } = new WebSocketPair();
    this.ctx.acceptWebSocket(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  /** Sends the event to every connected listener. */
  broadcast(event: InboundEvent): void {
    const message = JSON.stringify(event);
    for (const socket of this.ctx.getWebSockets()) {
      try {
        socket.send(message);
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
