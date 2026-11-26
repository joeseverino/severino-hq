// Package dockerevents is severino-hq-events-docker.service's watcher: it
// tells HQ the moment a container on this machine changes.
package dockerevents

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

const (
	// Socket is Docker's Engine API, where its CLI finds it.
	Socket = "/var/run/docker.sock"
	// ContainerEvent is the host event a container's change is told
	// as, its action after a dot: the name HQ's host readings declare in
	// INBOUND (held to it by a test).
	ContainerEvent = "docker.container"
	// tellBound is the longest HQ is given to store one batch.
	tellBound = time.Minute
)

// Filters is what the watcher asks Docker's event stream for: a
// container that dies, starts, changes health, runs out of memory or is
// removed. Docker matches health_status against each "health_status: ..."
// action it reports.
var Filters = map[string][]string{
	"type":  {"container"},
	"event": {"die", "start", "health_status", "oom", "destroy"},
}

// subjectLimit bounds what an event is about, as the contract's
// InboundArrival does, so no container's name makes HQ refuse a batch.
var subjectLimit = api.MustLimit("InboundArrival", "properties", "subject", "maxLength")

// Watcher tells HQ the moment a container on this machine changes, so
// its container readings are made due at once instead of at the next sweep.
//
// Its security properties: it runs as root only to reach Docker's socket, in
// the shared sandbox, with no network, capability or credential. Of Docker's
// API it reads the event stream alone. It tells HQ only each event's names
// (Docker's id, action and time, and the container's name), through the
// `hq-controller events host` HQ's container runs, as a failed unit's job
// does. An event carries no truth: HQ answers it by reading the containers
// with its own connection. A stream that drops is read again from the last
// event HQ stored, which Docker replays from the last 256 it keeps, and HQ
// stores a repeat once (its delivery id); the sweep reads whatever a longer
// gap held.
type Watcher struct {
	// Client reaches Docker's Engine API; its transport dials the socket.
	Client *http.Client
	// Tell hands HQ one batch, one InboundArrival per line, and returns
	// once HQ stored it.
	Tell func(ctx context.Context, lines []byte) error
	Log  *slog.Logger
	// The pace, the listener's unless a test sets its own.
	Window time.Duration
	runtime.Reconnect
}

// New is the watcher on Docker's socket at socket, telling HQ
// through tell.
func New(socket string, tell func(context.Context, []byte) error, log *slog.Logger) *Watcher {
	return &Watcher{
		// No timeout: the stream is held open until it drops.
		Client: &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		}}},
		Tell: tell, Log: log,
		Window: runtime.ListenerWindow, Reconnect: runtime.NewReconnect(),
	}
}

// TellHQ hands a batch to `hq-controller events host` in HQ's container,
// one run per batch, so nothing is resident beside the watcher and its exit
// says HQ stored the batch.
func TellHQ(ctx context.Context, lines []byte) error {
	ctx, cancel := context.WithTimeout(ctx, tellBound)
	defer cancel()
	command := hostcore.Command(ctx, "docker", "exec", "-i", hostcore.Container, "hq-controller", "events", string(runtime.InboundSourceHost))
	command.Stdin = bytes.NewReader(lines)
	if said, err := command.CombinedOutput(); err != nil {
		return fmt.Errorf("HQ was not told: %w: %s", err, bytes.TrimSpace(said))
	}
	return nil
}

// message is what the watcher reads of one of Docker's EventMessage.
type message struct {
	Type   string `json:"Type"`
	Action string `json:"Action"`
	Actor  struct {
		ID         string            `json:"ID"`
		Attributes map[string]string `json:"Attributes"`
	} `json:"Actor"`
	Time     int64 `json:"time"`
	TimeNano int64 `json:"timeNano"`
}

// arrival is the event as HQ is told it. Its delivery id is Docker's id,
// time and action, so a replay of the same event is the event HQ stored.
func (e message) arrival() runtime.InboundArrival {
	subject := e.Actor.Attributes["name"]
	if len(subject) > subjectLimit {
		subject = subject[:subjectLimit]
	}
	return runtime.InboundArrival{
		Provider:   runtime.InboundProviderHost,
		Event:      ContainerEvent + "." + e.Action,
		Subject:    subject,
		DeliveryID: fmt.Sprintf("%s-%d-%s", e.Actor.ID, e.TimeNano, e.Action),
		ReceivedAt: time.Unix(0, e.TimeNano).UTC().Truncate(time.Microsecond),
	}
}

// errStreamEnded is Docker closing the stream, which it does as it stops.
var errStreamEnded = errors.New("docker ended its event stream")

// Run tells HQ of each event until ctx ends, reading again from the last
// event HQ stored whenever the stream or HQ fails.
func (d *Watcher) Run(ctx context.Context) {
	var since int64
	d.Reconnect.Run(ctx, d.Log, "docker events lost", "docker-events.reconnect", func(ctx context.Context) error {
		return d.session(ctx, &since)
	})
}

// session is one read of the stream, from since when HQ stored an event
// before, telling HQ of each burst as one batch; since moves to the last
// event HQ stored.
func (d *Watcher) session(ctx context.Context, since *int64) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	filters, err := json.Marshal(Filters)
	if err != nil {
		return err
	}
	query := url.Values{"filters": {string(filters)}}
	if *since > 0 {
		query.Set("since", fmt.Sprintf("%d.%09d", *since/int64(time.Second), *since%int64(time.Second)))
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker/events?"+query.Encode(), http.NoBody)
	if err != nil {
		return err
	}
	response, err := d.Client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("docker answered %s", response.Status)
	}

	heard := make(chan message)
	ended := make(chan error, 1)
	go d.read(ctx, response.Body, heard, ended)

	var batch []message
	var window <-chan time.Time
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case event := <-heard:
			batch = append(batch, event)
			if window == nil {
				window = time.After(d.Window)
			}
			continue
		case err := <-ended:
			// Every event read was handed over before the stream's end was.
			if told := d.tell(ctx, batch, since); told != nil {
				return told
			}
			if errors.Is(err, io.EOF) {
				return errStreamEnded
			}
			return err
		case <-window:
		}
		if err := d.tell(ctx, batch, since); err != nil {
			return err
		}
		batch, window = nil, nil
	}
}

// read hands each container event of the stream to heard, in order, then
// why the stream ended to ended.
func (d *Watcher) read(ctx context.Context, stream io.Reader, heard chan<- message, ended chan<- error) {
	decoder := json.NewDecoder(stream)
	for {
		var event message
		if err := decoder.Decode(&event); err != nil {
			ended <- err
			return
		}
		if event.TimeNano == 0 {
			event.TimeNano = event.Time * int64(time.Second)
		}
		if event.Type != "container" || event.Actor.ID == "" || event.Action == "" || event.TimeNano <= 0 {
			d.Log.Warn("event dropped", slog.String("event", "docker-events.unreadable"), slog.String("action", event.Action))
			continue
		}
		select {
		case heard <- event:
		case <-ctx.Done():
			return
		}
	}
}

// tell hands batch to HQ, and moves since to its last event once HQ has it.
func (d *Watcher) tell(ctx context.Context, batch []message, since *int64) error {
	if len(batch) == 0 {
		return nil
	}
	var lines bytes.Buffer
	encoder := json.NewEncoder(&lines)
	for _, event := range batch {
		if err := encoder.Encode(event.arrival()); err != nil {
			return err
		}
	}
	if err := d.Tell(ctx, lines.Bytes()); err != nil {
		return err
	}
	*since = batch[len(batch)-1].TimeNano
	return nil
}
