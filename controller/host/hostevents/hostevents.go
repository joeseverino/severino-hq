// Package hostevents is severino-hq-events.service's watcher: it tells HQ
// the moment something on this machine changes that HQ reads, from each
// source it hears.
package hostevents

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// tellBound is the longest HQ is given to store one batch.
const tellBound = time.Minute

// A Stream is one source's events, read until it ends.
type Stream interface {
	// Read hands each event to heard, in order, until the stream ends or ctx
	// does, and returns why.
	Read(ctx context.Context, heard chan<- runtime.InboundArrival) error
	Close() error
}

// A Source opens its stream, returning once every later event is heard.
type Source func(ctx context.Context) (Stream, error)

// Watcher tells HQ the moment the machine changes, so the readings each
// event names are made due at once instead of at the next sweep.
//
// Its security properties: it runs as root only to reach Docker's socket and
// the system bus, in the shared sandbox, with no network, capability or
// credential. Of each it reads events alone: Docker's event stream, and
// systemd's job ends, which it subscribes to and never acts on. It tells HQ
// only each event's names through the `hq-controller events` HQ's container
// runs, and an event carries no truth: HQ answers it by reading with its own
// connection. Each time it begins reading, its first start included, it
// tells HQ it reconnected, so HQ reads again whatever it missed while it was
// not reading: Docker and systemd keep no log to resume from.
type Watcher struct {
	// Sources are read together: one that ends ends the read, which begins
	// again with every source.
	Sources []Source
	// Tell hands HQ one batch, one InboundArrival per line, and returns
	// once HQ stored it; reconnected says the watcher has just begun
	// reading.
	Tell func(ctx context.Context, reconnected bool, lines []byte) error
	Log  *slog.Logger
	// The pace, runtime.NewBurst's unless a test sets its own.
	Burst runtime.Burst
	runtime.Reconnect
}

// New is the watcher of sources, telling HQ through tell.
func New(tell func(context.Context, bool, []byte) error, log *slog.Logger, sources ...Source) *Watcher {
	return &Watcher{
		Sources: sources, Tell: tell, Log: log,
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

// Run tells HQ of each event until ctx ends, reading again whenever a stream
// or HQ fails.
func (w *Watcher) Run(ctx context.Context) {
	w.Reconnect.Run(ctx, w.Log, "host events lost", "host-events.reconnect", w.session)
}

// session is one read of every stream: HQ is told the watcher reconnected,
// then each burst as one batch, and while nothing happens that it is still
// reading.
func (w *Watcher) session(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	streams := make([]Stream, 0, len(w.Sources))
	defer func() {
		for _, stream := range streams {
			_ = stream.Close()
		}
	}()
	for _, open := range w.Sources {
		stream, err := open(ctx)
		if err != nil {
			return err
		}
		streams = append(streams, stream)
	}
	// Told once every stream is open, so HQ's read covers every change before it.
	if err := w.Tell(ctx, true, nil); err != nil {
		return err
	}

	heard := make(chan runtime.InboundArrival)
	ended := make(chan error, len(streams))
	for _, stream := range streams {
		go func() { ended <- stream.Read(ctx, heard) }()
	}

	burst := w.Burst
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case event := <-heard:
			if !burst.Add(event) {
				continue
			}
		case err := <-ended:
			// Every event a stream read was handed over before its end was.
			if burst.Held() {
				if told := w.tell(ctx, burst.Take()); told != nil {
					return told
				}
			}
			return err
		case <-burst.Due():
		}
		if err := w.tell(ctx, burst.Take()); err != nil {
			return err
		}
	}
}

// tell hands batch to HQ; one with no events says the watcher is reading.
func (w *Watcher) tell(ctx context.Context, batch []runtime.InboundArrival) error {
	var lines bytes.Buffer
	encoder := json.NewEncoder(&lines)
	for _, event := range batch {
		if err := encoder.Encode(event); err != nil {
			return err
		}
	}
	return w.Tell(ctx, false, lines.Bytes())
}

// hand gives event to heard, unless ctx ends first.
func hand(ctx context.Context, heard chan<- runtime.InboundArrival, event runtime.InboundArrival) error {
	select {
	case heard <- event:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
