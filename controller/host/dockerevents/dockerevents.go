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
	"os"
	"time"

	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

const (
	// Socket is Docker's Engine API, where its CLI finds it.
	Socket = "/var/run/docker.sock"
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

// Watcher tells HQ the moment a container on this machine changes, so
// its container readings are made due at once instead of at the next sweep.
//
// Its security properties: it runs as root only to reach Docker's socket, in
// the shared sandbox, with no network, capability or credential. Of Docker's
// API it reads the event stream alone, and HQ's own runs it leaves out. It
// tells HQ only each event's names (Docker's id, action and time, and the
// container's name), through the `hq-controller events` HQ's container runs.
// An event carries no truth: HQ answers it by reading the containers with
// its own connection. Each time it begins reading, its first start included,
// it tells HQ it reconnected, so HQ reads again whatever it missed while it
// was not reading, as the listener does: nothing is replayed.
type Watcher struct {
	// Client reaches Docker's Engine API; its transport dials the socket.
	Client *http.Client
	// Tell hands HQ one batch, one InboundArrival per line, and returns
	// once HQ stored it; reconnected says the watcher has just begun
	// reading.
	Tell func(ctx context.Context, reconnected bool, lines []byte) error
	Log  *slog.Logger
	// The pace, runtime.NewBurst's unless a test sets its own.
	Burst runtime.Burst
	runtime.Reconnect
}

// New is the watcher on Docker's socket at socket, telling HQ
// through tell.
func New(socket string, tell func(context.Context, bool, []byte) error, log *slog.Logger) *Watcher {
	return &Watcher{
		// No timeout: the stream is held open until it drops.
		Client: &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		}}},
		Tell: tell, Log: log,
		Burst: runtime.NewBurst(), Reconnect: runtime.NewReconnect(),
	}
}

// TellHQ hands a batch to `hq-controller events` in HQ's container, one run
// per batch, so nothing is resident beside the watcher and its exit says HQ
// stored the batch. What it says of an event it dropped reaches this unit's
// journal.
func TellHQ(ctx context.Context, reconnected bool, lines []byte) error {
	ctx, cancel := context.WithTimeout(ctx, tellBound)
	defer cancel()
	args := []string{"exec", "-i", hostcore.Container, "hq-controller", "events"}
	if reconnected {
		args = append(args, "--reconnected")
	}
	command := hostcore.Command(ctx, "docker", args...)
	command.Stdin, command.Stderr = bytes.NewReader(lines), os.Stderr
	if said, err := command.Output(); err != nil {
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
// time and action, which no other event shares.
func (e message) arrival() runtime.InboundArrival {
	return runtime.InboundArrival{
		Provider:   runtime.InboundProviderHost,
		Event:      string(runtime.HostEventDockerContainer) + "." + e.Action,
		Subject:    e.Actor.Attributes["name"],
		DeliveryID: fmt.Sprintf("%s-%d-%s", e.Actor.ID, e.TimeNano, e.Action),
		ReceivedAt: time.Unix(0, e.TimeNano).UTC().Truncate(time.Microsecond),
	}
}

// errStreamEnded is Docker closing the stream, which it does as it stops.
var errStreamEnded = errors.New("docker ended its event stream")

// Run tells HQ of each event until ctx ends, reading again whenever the
// stream or HQ fails.
func (d *Watcher) Run(ctx context.Context) {
	d.Reconnect.Run(ctx, d.Log, "docker events lost", "docker-events.reconnect", d.session)
}

// session is one read of the stream: HQ is told the watcher reconnected,
// then each burst as one batch, and while nothing happens that it is still
// reading.
func (d *Watcher) session(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	filters, err := json.Marshal(Filters)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker/events?"+url.Values{"filters": {string(filters)}}.Encode(), http.NoBody)
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
	// Told once the stream is open, so HQ's read covers every change before it.
	if err := d.Tell(ctx, true, nil); err != nil {
		return err
	}

	heard := make(chan runtime.InboundArrival)
	ended := make(chan error, 1)
	go d.read(ctx, response.Body, heard, ended)

	burst := d.Burst
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case event := <-heard:
			if !burst.Add(event) {
				continue
			}
		case err := <-ended:
			// Every event read was handed over before the stream's end was.
			if burst.Held() {
				if told := d.tell(ctx, burst.Take()); told != nil {
					return told
				}
			}
			if errors.Is(err, io.EOF) {
				return errStreamEnded
			}
			return err
		case <-burst.Due():
		}
		if err := d.tell(ctx, burst.Take()); err != nil {
			return err
		}
	}
}

// read hands each container event of the stream to heard, in order, then
// why the stream ended to ended.
func (d *Watcher) read(ctx context.Context, stream io.Reader, heard chan<- runtime.InboundArrival, ended chan<- error) {
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
		// HQ's own runs are its work, never a change to read: a pass is a
		// container, so its start and end would each ask for the next pass.
		// Docker copies a container's labels into its events' attributes.
		if _, own := event.Actor.Attributes[host.RunLabel]; own {
			continue
		}
		select {
		case heard <- event.arrival():
		case <-ctx.Done():
			return
		}
	}
}

// tell hands batch to HQ; one with no events says the watcher is reading.
func (d *Watcher) tell(ctx context.Context, batch []runtime.InboundArrival) error {
	var lines bytes.Buffer
	encoder := json.NewEncoder(&lines)
	for _, event := range batch {
		if err := encoder.Encode(event); err != nil {
			return err
		}
	}
	return d.Tell(ctx, false, lines.Bytes())
}
