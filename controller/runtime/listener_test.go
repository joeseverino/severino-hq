package runtime

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/internal/secret"
)

// What the Worker's Hub sends for one verified GitHub delivery (worker/src/
// hub.ts InboundEvent): the X-GitHub-Event header and the X-GitHub-Delivery
// GUID and repository GitHub's docs show.
const pushEvent = `{"provider":"github_app","event":"push","subject":"octo-org/octo-repo",` +
	`"delivery_id":"72d3162e-cc78-11e3-81ab-4c9367dc0958","received_at":"2026-01-15T12:00:00.000Z"}`

const socketToken = "example-socket-token"

// The Hub's opening to a listener it cannot resume (worker/src/hub.ts Opening).
const unresumed = `{"last_event_id":0,"resumed":false}`

// asLogged is an event as the Hub sends it, under its id in the log.
func asLogged(id int, arrival string) []byte {
	return fmt.Appendf(nil, `{"id":%d,"arrival":%s}`, id, arrival)
}

// opened sends the Hub's opening, then each message, then holds the
// connection until the listener leaves or drop closes.
func opened(opening string, drop <-chan struct{}, messages ...[]byte) func(context.Context, *websocket.Conn) {
	return sent(drop, append([][]byte{[]byte(opening)}, messages...)...)
}

// hub is the Worker's socket as a listener meets it: the bearer checked
// before the upgrade, then each connection handed to serve.
type hub struct {
	*httptest.Server
	// dialed counts each upgrade, refused or not; presented is the
	// Last-Event-ID each names, empty for none.
	dialed    chan struct{}
	presented chan string
	pinged    chan struct{}
	// pong is whether a ping is answered.
	pong  bool
	serve func(context.Context, *websocket.Conn)
}

func newHub(t *testing.T, serve func(context.Context, *websocket.Conn)) *hub {
	t.Helper()
	w := &hub{dialed: make(chan struct{}, 64), presented: make(chan string, 64), pinged: make(chan struct{}, 64), pong: true, serve: serve}
	w.Server = httptest.NewTLSServer(http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		select {
		case w.dialed <- struct{}{}:
		default:
		}
		select {
		case w.presented <- r.Header.Get("Last-Event-ID"):
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

// holding opens each connection and keeps it until the listener leaves.
var holding = opened(unresumed, nil)

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
		APIToken: &connections.APIToken{URL: "wss" + strings.TrimPrefix(w.URL, "https"), APIToken: secret.New(socketToken)}}
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
	w := newHub(t, opened(unresumed, nil, asLogged(1, pushEvent)))
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
	burst := make([][]byte, 0, 8)
	for id := range 5 {
		burst = append(burst, asLogged(id+1, pushEvent))
	}
	// Neither HQ's provider nor the contract's shape: dropped, never posted.
	burst = append(burst, asLogged(6, strings.Replace(pushEvent, "github_app", "example", 1)),
		asLogged(7, `{"body":{"commits":[]}}`))
	w := newHub(t, opened(unresumed, nil, burst...))
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
	// Each connection is dropped as soon as the Hub has opened it.
	w := newHub(t, opened(unresumed, dropped()))
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
	listener.Ping, listener.Burst.Heartbeat = 10*time.Millisecond, 50*time.Millisecond
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
	listener.Ping, listener.PongWait = 10*time.Millisecond, 10*time.Millisecond
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
			APIToken: &connections.APIToken{URL: address, APIToken: secret.New(socketToken)}}
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

// A listener presents the id of the last event HQ took, and a Hub that sends
// what followed it is not asked to make HQ read again; one whose log no
// longer holds it is, and the listener then holds the newest id it named.
func TestAReconnectResumesAfterTheLastEventHQTook(t *testing.T) {
	bridge, batches := hq(t)
	// Each of the first three connections is dropped once HQ has its posts,
	// which a heartbeat after them says, as posts follow one another.
	drops := []chan struct{}{make(chan struct{}), make(chan struct{}), make(chan struct{})}
	var dials atomic.Int32
	w := newHub(t, nil)
	w.serve = func(ctx context.Context, conn *websocket.Conn) {
		switch dials.Add(1) {
		case 1:
			opened(unresumed, drops[0], asLogged(7, pushEvent))(ctx, conn)
		case 2:
			opened(`{"last_event_id":9,"resumed":true}`, drops[1], asLogged(8, pushEvent), asLogged(9, pushEvent))(ctx, conn)
		case 3:
			opened(`{"last_event_id":12,"resumed":false}`, drops[2])(ctx, conn)
		default:
			holding(ctx, conn)
		}
	}
	listener := listenerFor(t, w, bridge)
	listener.Burst.Heartbeat = 10 * time.Millisecond
	run(t, listener)

	if presented := next(t, w.presented, "first dial"); presented != "0" {
		t.Fatalf("a listener that heard nothing presented %q", presented)
	}
	if first := next(t, batches, "batch on connecting"); !first.Reconnected {
		t.Fatalf("a first connection began with %+v, not a read", first)
	}
	if batch := events(t, batches); len(batch.Events) != 1 {
		t.Fatalf("the event arrived as %+v", batch)
	}
	heartbeat(t, batches)
	close(drops[0])

	if presented := next(t, w.presented, "second dial"); presented != "7" {
		t.Fatalf("the reconnect presented %q, not the event HQ took", presented)
	}
	if batch := events(t, batches); len(batch.Events) != 2 {
		t.Fatalf("the missed events arrived as %+v", batch)
	}
	heartbeat(t, batches)
	close(drops[1])

	if presented := next(t, w.presented, "third dial"); presented != "9" {
		t.Fatalf("the reconnect presented %q, not the last event HQ took", presented)
	}
	if batch := events(t, batches); !batch.Reconnected {
		t.Fatalf("a connection the Hub could not resume began with %+v, not a read", batch)
	}
	heartbeat(t, batches)
	close(drops[2])
	if presented := next(t, w.presented, "fourth dial"); presented != "12" {
		t.Fatalf("after reading again the reconnect presented %q, not the newest id the Hub named", presented)
	}
}

// events is the next batch that holds events or asks HQ to read again, past
// any heartbeats.
func events(t *testing.T, batches <-chan InboundBatch) InboundBatch {
	t.Helper()
	for {
		if batch := next(t, batches, "batch"); batch.Reconnected || len(batch.Events) > 0 {
			return batch
		}
	}
}

// heartbeat waits for a heartbeat, which follows every post before it.
func heartbeat(t *testing.T, batches <-chan InboundBatch) {
	t.Helper()
	if batch := next(t, batches, "heartbeat"); batch.Reconnected || len(batch.Events) != 0 {
		t.Fatalf("a heartbeat was due, and %+v was posted", batch)
	}
}

// A Worker of the release before the log, which deploys apart from HQ's,
// sends each event as it is and no opening, before the listener holds an id
// and after it does; each connection to it is heard, and HQ reads again.
func TestAHubWithNoLogIsHeardAndHQReadsAgain(t *testing.T) {
	bridge, batches := hq(t)
	drops := []chan struct{}{make(chan struct{}), make(chan struct{})}
	var dials atomic.Int32
	w := newHub(t, nil)
	w.serve = func(ctx context.Context, conn *websocket.Conn) {
		switch dials.Add(1) {
		case 1:
			sent(drops[0], []byte(pushEvent))(ctx, conn)
		case 2:
			opened(unresumed, drops[1], asLogged(7, pushEvent))(ctx, conn)
		default:
			sent(nil, []byte(pushEvent))(ctx, conn)
		}
	}
	listener := listenerFor(t, w, bridge)
	listener.Burst.Heartbeat = 10 * time.Millisecond
	run(t, listener)

	for i, drop := range drops {
		next(t, w.presented, "dial")
		if batch := events(t, batches); !batch.Reconnected {
			t.Fatalf("connection %d began with %+v, not a read", i+1, batch)
		}
		if batch := events(t, batches); len(batch.Events) != 1 {
			t.Fatalf("connection %d posted the event as %+v", i+1, batch)
		}
		heartbeat(t, batches)
		close(drop)
	}
	if presented := next(t, w.presented, "dial after the log"); presented != "7" {
		t.Fatalf("the reconnect presented %q, not the event HQ took", presented)
	}
	if batch := events(t, batches); !batch.Reconnected {
		t.Fatalf("a Hub with no log was not read again for: %+v", batch)
	}
	if batch := events(t, batches); len(batch.Events) != 1 {
		t.Fatalf("the event was posted as %+v", batch)
	}
}

// sent sends each message as a Hub with no log does, with no opening, then
// holds the connection until the listener leaves or drop closes.
func sent(drop <-chan struct{}, messages ...[]byte) func(context.Context, *websocket.Conn) {
	return func(ctx context.Context, conn *websocket.Conn) {
		for _, message := range messages {
			_ = conn.Write(ctx, websocket.MessageText, message)
		}
		select {
		case <-ctx.Done():
		case <-drop:
		}
	}
}

// dropped drops a connection as soon as the Hub has opened it.
func dropped() <-chan struct{} {
	closed := make(chan struct{})
	close(closed)
	return closed
}
