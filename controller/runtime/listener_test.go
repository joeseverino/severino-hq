package runtime

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/joeseverino/severino-hq/controller/connections"
)

// What the Worker's Hub sends for one verified GitHub delivery (worker/src/
// hub.ts InboundEvent): the X-GitHub-Event header and the X-GitHub-Delivery
// GUID and repository GitHub's docs show.
const pushEvent = `{"provider":"github_app","event":"push","subject":"octo-org/octo-repo",` +
	`"delivery_id":"72d3162e-cc78-11e3-81ab-4c9367dc0958","received_at":"2026-01-15T12:00:00.000Z"}`

const socketToken = "example-socket-token"

// hub is the Worker's socket as a listener meets it: the bearer checked
// before the upgrade, then each connection handed to serve.
type hub struct {
	*httptest.Server
	// dialed counts each upgrade, refused or not.
	dialed chan struct{}
	pinged chan struct{}
	// pong is whether a ping is answered.
	pong  bool
	serve func(context.Context, *websocket.Conn)
}

func newHub(t *testing.T, serve func(context.Context, *websocket.Conn)) *hub {
	t.Helper()
	w := &hub{dialed: make(chan struct{}, 64), pinged: make(chan struct{}, 64), pong: true, serve: serve}
	w.Server = httptest.NewTLSServer(http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		select {
		case w.dialed <- struct{}{}:
		default:
		}
		if r.Header.Get("Authorization") != "Bearer "+socketToken {
			rw.WriteHeader(http.StatusUnauthorized)
			return
		}
		conn, err := websocket.Accept(rw, r, &websocket.AcceptOptions{OnPingReceived: func(context.Context, []byte) bool {
			select {
			case w.pinged <- struct{}{}:
			default:
			}
			return w.pong
		}})
		if err != nil {
			return
		}
		defer func() { _ = conn.CloseNow() }()
		// A listener sends nothing, so this read returns only as the socket
		// ends, and answers each ping meanwhile.
		ctx := conn.CloseRead(r.Context())
		w.serve(ctx, conn)
	}))
	t.Cleanup(w.Close)
	return w
}

// holding keeps each connection open until the listener leaves.
func holding(ctx context.Context, _ *websocket.Conn) { <-ctx.Done() }

// hq is the bridge as the listener reaches it, answering every events call
// and handing each batch to the test.
func hq(t *testing.T) (*SocketBridge, chan InboundBatch) {
	t.Helper()
	batches := make(chan InboundBatch, 64)
	bridge := served(t, func(rw http.ResponseWriter, r *http.Request) {
		var batch InboundBatch
		data, _ := io.ReadAll(r.Body)
		if r.URL.Path != "/events" || json.Unmarshal(data, &batch) != nil {
			http.Error(rw, "not an events call", http.StatusBadRequest)
			return
		}
		select {
		case batches <- batch:
		default:
		}
		rw.Header().Set("Content-Type", "application/json")
		_, _ = rw.Write([]byte(`{"ok":true}`))
	})
	return bridge, batches
}

func listenerFor(t *testing.T, w *hub, bridge *SocketBridge) *Listener {
	t.Helper()
	connection := connections.Connection{Ref: "events", Provider: string(ConnectionProviderCloudflareEvents),
		APIToken: &connections.APIToken{URL: "wss" + strings.TrimPrefix(w.URL, "https"), APIToken: socketToken}}
	listener, err := NewListener(connection, w.Client(), bridge.Events, slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	listener.Jitter = func(time.Duration) time.Duration { return 0 }
	return listener
}

// run starts the listener and stops it when the test ends.
func run(t *testing.T, listener *Listener) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); listener.Run(ctx) }()
	t.Cleanup(func() { cancel(); <-done })
}

func next[T any](t *testing.T, from <-chan T, what string) T {
	t.Helper()
	select {
	case value := <-from:
		return value
	case <-time.After(10 * time.Second):
		t.Fatalf("no %s", what)
		panic("unreachable")
	}
}

func TestAnEventIsOneEventsCallFromTheWorkerSource(t *testing.T) {
	bridge, batches := hq(t)
	w := newHub(t, func(ctx context.Context, conn *websocket.Conn) {
		_ = conn.Write(ctx, websocket.MessageText, []byte(pushEvent))
		<-ctx.Done()
	})
	run(t, listenerFor(t, w, bridge))

	if first := next(t, batches, "batch on connecting"); !first.Reconnected || len(first.Events) != 0 {
		t.Fatalf("the first batch is %+v, not a reconnect", first)
	}
	batch := next(t, batches, "batch of the event")
	if batch.Source != InboundSourceWorker || batch.Reconnected || len(batch.Events) != 1 {
		t.Fatalf("the event arrived as %+v", batch)
	}
	if event := batch.Events[0]; event.Provider != InboundProviderGitHubApp || event.Event != "push" ||
		event.Subject != "octo-org/octo-repo" || event.DeliveryID != "72d3162e-cc78-11e3-81ab-4c9367dc0958" {
		t.Errorf("the event was posted as %+v", event)
	}
}

func TestABurstOfEventsIsOneBatch(t *testing.T) {
	bridge, batches := hq(t)
	w := newHub(t, func(ctx context.Context, conn *websocket.Conn) {
		for range 5 {
			_ = conn.Write(ctx, websocket.MessageText, []byte(pushEvent))
		}
		// Neither HQ's provider nor the contract's shape: dropped, never posted.
		_ = conn.Write(ctx, websocket.MessageText, []byte(strings.Replace(pushEvent, "github_app", "example", 1)))
		_ = conn.Write(ctx, websocket.MessageText, []byte(`{"body":{"commits":[]}}`))
		<-ctx.Done()
	})
	run(t, listenerFor(t, w, bridge))

	next(t, batches, "batch on connecting")
	if batch := next(t, batches, "batch of the burst"); len(batch.Events) != 5 {
		t.Fatalf("a burst of 5 was posted as %d events", len(batch.Events))
	}
	select {
	case batch := <-batches:
		t.Errorf("a second batch was posted: %+v", batch)
	case <-time.After(10 * BurstWindow):
	}
}

func TestADroppedSocketReconnectsAndHQReadsAgainEachTime(t *testing.T) {
	bridge, batches := hq(t)
	// Each connection is dropped as soon as it is made.
	w := newHub(t, func(context.Context, *websocket.Conn) {})
	run(t, listenerFor(t, w, bridge))

	for range 3 {
		next(t, w.dialed, "dial")
		if batch := next(t, batches, "batch on connecting"); !batch.Reconnected {
			t.Fatalf("a connection began with %+v, not a reconnect", batch)
		}
	}
}

func TestAnAnsweredSocketIsPingedAndHeardAsAHeartbeat(t *testing.T) {
	bridge, batches := hq(t)
	w := newHub(t, holding)
	listener := listenerFor(t, w, bridge)
	listener.Ping, listener.Heartbeat = 10*time.Millisecond, 50*time.Millisecond
	run(t, listener)

	next(t, batches, "batch on connecting")
	for range 3 {
		next(t, w.pinged, "ping")
	}
	if batch := next(t, batches, "heartbeat"); batch.Reconnected || len(batch.Events) != 0 || batch.Source != InboundSourceWorker {
		t.Errorf("the heartbeat was %+v", batch)
	}
}

func TestAnUnansweredPingEndsTheConnectionWithNoHeartbeat(t *testing.T) {
	bridge, batches := hq(t)
	w := newHub(t, holding)
	w.pong = false
	listener := listenerFor(t, w, bridge)
	listener.Ping, listener.Heartbeat = 10*time.Millisecond, 0
	run(t, listener)

	for range 2 {
		next(t, w.dialed, "dial")
		if batch := next(t, batches, "batch on connecting"); !batch.Reconnected {
			t.Fatalf("a heartbeat was posted while pings went unanswered: %+v", batch)
		}
	}
}

func TestTheTokenGoesOnlyToAWSSAddress(t *testing.T) {
	for _, address := range []string{"ws://events.example.com/socket", "https://events.example.com/socket", "wss:///socket"} {
		connection := connections.Connection{Provider: string(ConnectionProviderCloudflareEvents),
			APIToken: &connections.APIToken{URL: address, APIToken: socketToken}}
		if _, err := NewListener(connection, http.DefaultClient, nil, nil); err == nil {
			t.Errorf("a listener would send its token to %s", address)
		}
	}
}

// Each text is held to the contract's bounds by its length in characters, so
// a name HQ would store whole is never refused, and one it would refuse
// never reaches it.
func TestAnEventIsHeldToTheContractsBounds(t *testing.T) {
	with := func(member, value string) []byte {
		var event map[string]any
		if err := json.Unmarshal([]byte(pushEvent), &event); err != nil {
			t.Fatal(err)
		}
		event[member] = value
		data, _ := json.Marshal(event)
		return data
	}
	for _, kept := range [][]byte{with("subject", strings.Repeat("é", 255)), with("subject", "")} {
		if _, err := ReadArrival(kept); err != nil {
			t.Errorf("%s refused: %v", kept, err)
		}
	}
	for _, refused := range [][]byte{
		with("subject", strings.Repeat("a", 256)), with("event", strings.Repeat("a", 121)),
		with("delivery_id", strings.Repeat("a", 129)),
	} {
		if _, err := ReadArrival(refused); err == nil {
			t.Errorf("%.80s... is read", refused)
		}
	}
}
