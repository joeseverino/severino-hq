package dockerevents

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

// watch runs the watcher on socket until the test ends; each batch it tells
// HQ arrives on the channel.
func watch(t *testing.T, socket string) chan []byte {
	t.Helper()
	told := make(chan []byte, 8)
	watcher := New(socket, func(_ context.Context, lines []byte) error {
		told <- bytes.Clone(lines)
		return nil
	}, slog.New(slog.DiscardHandler))
	watcher.Window = 50 * time.Millisecond
	watcher.FirstRetry, watcher.LastRetry = time.Millisecond, time.Millisecond
	watcher.Jitter = func(bound time.Duration) time.Duration { return bound }
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { watcher.Run(ctx); close(done) }()
	t.Cleanup(func() { cancel(); <-done })
	return told
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
	watch(t, socket)

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
		t.Errorf("a first read asks from %q", query.Get("since"))
	}
}

func TestABurstIsOneBatchOfEventsHQReads(t *testing.T) {
	const at = int64(1461943101381709551)
	socket, _ := fakeEngine(t, stream{
		lines: engineMessage("die", at) + engineMessage("health_status: unhealthy", at+1) + engineMessage("start", at+2),
		hold:  true,
	})
	told := watch(t, socket)

	events := arrivals(t, arrived(t, told))
	if len(events) != 3 {
		t.Fatalf("a burst of 3 was told as a batch of %d", len(events))
	}
	died := events[0]
	if died.Provider != runtime.InboundProviderHost || died.Event != ContainerEvent+".die" || died.Subject != "my-container" {
		t.Errorf("told %+v", died)
	}
	if !died.ReceivedAt.Equal(time.Unix(0, at).Truncate(time.Microsecond)) {
		t.Errorf("received at %s, the event was at %s", died.ReceivedAt, time.Unix(0, at))
	}
	if events[1].Event != ContainerEvent+".health_status: unhealthy" {
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

// Docker replays from since inclusively, so the stream read again repeats
// the last event HQ stored, as the same delivery.
func TestADroppedStreamIsReadAgainFromTheLastEventHQStored(t *testing.T) {
	const at = int64(1461943101381709551)
	first, second := engineMessage("die", at), engineMessage("destroy", at+5)
	socket, asked := fakeEngine(t, stream{lines: first}, stream{lines: first + second, hold: true})
	told := watch(t, socket)

	stored := arrivals(t, arrived(t, told))
	arrived(t, asked)
	if since := arrived(t, asked).Get("since"); since != "1461943101.381709551" {
		t.Errorf("read again from %q, the last event was at 1461943101.381709551", since)
	}
	replayed := arrivals(t, arrived(t, told))
	if len(replayed) != 2 || replayed[0] != stored[0] {
		t.Errorf("the replay %+v is not the event HQ stored, %+v, then the next", replayed, stored)
	}
	if replayed[1].Event != ContainerEvent+".destroy" {
		t.Errorf("then told %q", replayed[1].Event)
	}
}

// HQ's host readings declare the event the watcher tells, so it makes the
// container readings due.
func TestHQsHostReadingsDeclareTheWatchersEvent(t *testing.T) {
	source, err := os.ReadFile(filepath.Join("..", "..", "..", "hq", "domains", "control_plane", "observations", "host.py"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(source, []byte(`DOCKER_CONTAINER = "`+ContainerEvent+`"`)) {
		t.Errorf("hq/domains/control_plane/observations/host.py declares no DOCKER_CONTAINER = %q", ContainerEvent)
	}
}
