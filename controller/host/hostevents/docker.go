package hostevents

import (
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

	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// DockerSocket is Docker's Engine API, where its CLI finds it.
const DockerSocket = "/var/run/docker.sock"

// DockerFilters is what the watcher asks Docker's event stream for: a
// container that dies, starts, changes health, runs out of memory or is
// removed. Docker matches health_status against each "health_status: ..."
// action it reports.
var DockerFilters = map[string][]string{
	"type":  {"container"},
	"event": {"die", "start", "health_status", "oom", "destroy"},
}

// Docker is the source of each change to a container on the machine whose
// Engine API answers on socket. HQ's own runs are left out.
func Docker(socket string, log *slog.Logger) Source {
	// No timeout: the stream is held open until it drops.
	client := &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}}}
	return func(ctx context.Context) (Stream, error) {
		filters, err := json.Marshal(DockerFilters)
		if err != nil {
			return nil, err
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker/events?"+url.Values{"filters": {string(filters)}}.Encode(), http.NoBody)
		if err != nil {
			return nil, err
		}
		response, err := client.Do(request)
		if err != nil {
			return nil, err
		}
		if response.StatusCode != http.StatusOK {
			response.Body.Close()
			return nil, fmt.Errorf("docker answered %s", response.Status)
		}
		return dockerStream{response.Body, log}, nil
	}
}

// errDockerEnded is Docker closing the stream, which it does as it stops.
var errDockerEnded = errors.New("docker ended its event stream")

type dockerStream struct {
	io.ReadCloser
	log *slog.Logger
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

// Read hands each container event of the stream to heard, in order.
func (s dockerStream) Read(ctx context.Context, heard chan<- runtime.InboundArrival) error {
	decoder := json.NewDecoder(s.ReadCloser)
	for {
		var event message
		if err := decoder.Decode(&event); err != nil {
			if errors.Is(err, io.EOF) {
				return errDockerEnded
			}
			return err
		}
		if event.TimeNano == 0 {
			event.TimeNano = event.Time * int64(time.Second)
		}
		if event.Type != "container" || event.Actor.ID == "" || event.Action == "" || event.TimeNano <= 0 {
			s.log.Warn("event dropped", slog.String("event", "docker-events.unreadable"), slog.String("action", event.Action))
			continue
		}
		// HQ's own runs are its work, never a change to read: a pass is a
		// container, so its start and end would each ask for the next pass.
		// Docker copies a container's labels into its events' attributes.
		if _, own := event.Actor.Attributes[host.RunLabel]; own {
			continue
		}
		if err := hand(ctx, heard, event.arrival()); err != nil {
			return err
		}
	}
}
