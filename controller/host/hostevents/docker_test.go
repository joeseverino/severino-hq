package hostevents

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// engineMessage is one of Docker's EventMessage, as the Engine API reference
// (docs.docker.com/reference/api/engine) gives its example.
func engineMessage(action string, timeNano int64) string {
	return `{"Type":"container","Action":"` + action + `","Actor":{"ID":"ede54ee1afda366ab42f824e8a5ffd195155d853ceaec74a927f249ea270c743",` +
		`"Attributes":{"com.example.some-label":"some-label-value","image":"alpine:latest","name":"my-container"}},` +
		`"scope":"local","time":` + jsonInt(timeNano/int64(time.Second)) + `,"timeNano":` + jsonInt(timeNano) + "}\n"
}

// passMessage is one of Docker's EventMessage for a controller pass's own
// container, labeled as hq-host's launch labels it.
func passMessage(action string, timeNano int64) string {
	return `{"Type":"container","Action":"` + action + `","Actor":{"ID":"0f5c1a8e9b2d4c6f8a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f",` +
		`"Attributes":{"image":"ghcr.io/example/hq@sha256:0000000000000000000000000000000000000000000000000000000000000000",` +
		`"name":"wizardly_example","severino-hq.role":"controller","severino-hq.run":"EXAMPLEEXAMPLEEXAMPLEEXAMPLE"}},` +
		`"scope":"local","time":` + jsonInt(timeNano/int64(time.Second)) + `,"timeNano":` + jsonInt(timeNano) + "}\n"
}

// docker is the Docker source on socket, as the watcher's unit reads it.
func docker(socket string) Source { return Docker(socket, slog.New(slog.DiscardHandler)) }

func jsonInt(n int64) string {
	data, _ := json.Marshal(n)
	return string(data)
}

// stream is one answer of the fake engine to GET /events: what it writes at
// once, and whether it then holds the stream open or drops it.
type stream struct {
	lines string
	hold  bool
}

// fakeEngine serves Docker's events endpoint on a unix socket: each request's
// query, and each answer from streams in turn.
func fakeEngine(t *testing.T, streams ...stream) (socket string, asked chan url.Values) {
	t.Helper()
	dir, err := os.MkdirTemp("", "engine")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	socket = filepath.Join(dir, "docker.sock")
	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	asked = make(chan url.Values, len(streams)+1)
	answers := make(chan stream, len(streams))
	for _, s := range streams {
		answers <- s
	}
	server := &http.Server{ReadHeaderTimeout: time.Second, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/events" || r.Method != http.MethodGet {
			http.NotFound(w, r)
			return
		}
		asked <- r.URL.Query()
		var answer stream
		select {
		case answer = <-answers:
		default:
			answer = stream{hold: true}
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, answer.lines)
		w.(http.Flusher).Flush()
		if answer.hold {
			<-r.Context().Done()
		}
	})}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { _ = server.Close() })
	return socket, asked
}

// watch runs the watcher on sources until the test ends; each batch of
// events it tells HQ arrives on told, and each reconnect on reconnected.
func watch(t *testing.T, sources ...Source) (told chan []byte, reconnected chan struct{}) {
	t.Helper()
	told, reconnected = make(chan []byte, 8), make(chan struct{}, 8)
	watcher := New(func(_ context.Context, again bool, lines []byte) error {
		if again {
			if len(lines) != 0 {
				t.Errorf("a reconnect carried %s", lines)
			}
			reconnected <- struct{}{}
			return nil
		}
		told <- bytes.Clone(lines)
		return nil
	}, slog.New(slog.DiscardHandler), sources...)
	watcher.Burst.Window = 50 * time.Millisecond
	watcher.FirstRetry, watcher.LastRetry = time.Millisecond, time.Millisecond
	watcher.Jitter = func(bound time.Duration) time.Duration { return bound }
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { watcher.Run(ctx); close(done) }()
	t.Cleanup(func() { cancel(); <-done })
	return told, reconnected
}

func arrived[T any](t *testing.T, from chan T) T {
	t.Helper()
	select {
	case value := <-from:
		return value
	case <-time.After(5 * time.Second):
		t.Fatal("nothing arrived")
	}
	var zero T
	return zero
}

// arrivals is each line of a batch, read as `hq-controller events` reads it.
func arrivals(t *testing.T, lines []byte) []runtime.InboundArrival {
	t.Helper()
	var read []runtime.InboundArrival
	for line := range strings.SplitSeq(strings.TrimSuffix(string(lines), "\n"), "\n") {
		arrival, err := runtime.ReadArrival([]byte(line))
		if err != nil {
			t.Fatalf("hq-controller events refuses %q: %v", line, err)
		}
		read = append(read, arrival)
	}
	return read
}

func TestTheWatcherAsksDockerForTheContainerChangesHQReads(t *testing.T) {
	socket, asked := fakeEngine(t)
	_, _ = watch(t, docker(socket))

	query := arrived(t, asked)
	var filters map[string][]string
	if err := json.Unmarshal([]byte(query.Get("filters")), &filters); err != nil {
		t.Fatalf("filters %q: %v", query.Get("filters"), err)
	}
	slices.Sort(filters["event"])
	if want := []string{"container"}; !slices.Equal(filters["type"], want) {
		t.Errorf("type filter %v, want %v", filters["type"], want)
	}
	if want := []string{"destroy", "die", "health_status", "oom", "start"}; !slices.Equal(filters["event"], want) {
		t.Errorf("event filter %v, want %v", filters["event"], want)
	}
	if len(filters) != 2 {
		t.Errorf("filters %v name more than the type and the events", filters)
	}
	if query.Has("since") {
		t.Errorf("a read asks Docker to replay from %q", query.Get("since"))
	}
}

func TestABurstIsOneBatchOfEventsHQReads(t *testing.T) {
	const at = int64(1461943101381709551)
	socket, _ := fakeEngine(t, stream{
		lines: engineMessage("die", at) + engineMessage("health_status: unhealthy", at+1) + engineMessage("start", at+2),
		hold:  true,
	})
	told, _ := watch(t, docker(socket))

	events := arrivals(t, arrived(t, told))
	if len(events) != 3 {
		t.Fatalf("a burst of 3 was told as a batch of %d", len(events))
	}
	died := events[0]
	if died.Provider != runtime.InboundProviderHost || died.Event != string(runtime.HostEventDockerContainer)+".die" || died.Subject != "my-container" {
		t.Errorf("told %+v", died)
	}
	if !died.ReceivedAt.Equal(time.Unix(0, at).Truncate(time.Microsecond)) {
		t.Errorf("received at %s, the event was at %s", died.ReceivedAt, time.Unix(0, at))
	}
	if events[1].Event != string(runtime.HostEventDockerContainer)+".health_status: unhealthy" {
		t.Errorf("a health change is told as %q", events[1].Event)
	}
	ids := map[string]bool{}
	for _, event := range events {
		ids[event.DeliveryID] = true
	}
	if len(ids) != 3 {
		t.Errorf("three events share delivery ids: %v", ids)
	}
}

// A controller pass is a container: its own start, end and removal are HQ's
// work, and told to HQ they would ask for the next pass, without end.
func TestHQsOwnRunsAreNeverTold(t *testing.T) {
	const at = int64(1461943101381709551)
	socket, _ := fakeEngine(t, stream{
		lines: passMessage("start", at) + passMessage("die", at+1) + passMessage("destroy", at+2) + engineMessage("die", at+3),
		hold:  true,
	})
	told, _ := watch(t, docker(socket))

	events := arrivals(t, arrived(t, told))
	if len(events) != 1 || events[0].Subject != "my-container" {
		t.Fatalf("told %+v, want only my-container's die", events)
	}
}

// The watcher's own start, and each stream it loses, are a gap HQ closes by
// reading again: each read of the stream begins by telling HQ it reconnected.
func TestEachReadOfTheStreamTellsHQItReconnected(t *testing.T) {
	const at = int64(1461943101381709551)
	socket, asked := fakeEngine(t, stream{lines: engineMessage("die", at)}, stream{hold: true})
	told, reconnected := watch(t, docker(socket))

	arrived(t, asked)
	arrived(t, reconnected)
	if events := arrivals(t, arrived(t, told)); len(events) != 1 {
		t.Fatalf("told %+v", events)
	}
	arrived(t, asked)
	arrived(t, reconnected)
}

// A stream that stays quiet is still told, at the pace HQ holds the host's
// channel to, so a quiet host never reads as one that stopped reporting.
func TestAQuietStreamIsToldAsListening(t *testing.T) {
	socket, _ := fakeEngine(t)
	told := make(chan []byte, 8)
	watcher := New(func(_ context.Context, again bool, lines []byte) error {
		if !again {
			told <- bytes.Clone(lines)
		}
		return nil
	}, slog.New(slog.DiscardHandler), docker(socket))
	watcher.Burst.Heartbeat = 20 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { watcher.Run(ctx); close(done) }()
	t.Cleanup(func() { cancel(); <-done })

	for range 2 {
		if lines := arrived(t, told); len(lines) != 0 {
			t.Fatalf("a quiet stream told %s", lines)
		}
	}
}
