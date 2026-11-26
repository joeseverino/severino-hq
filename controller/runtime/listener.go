package runtime

import (
	"context"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"time"
	"unicode/utf8"

	"github.com/coder/websocket"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/connections"
)

// The listener's pace, each defined once.
const (
	// ListenerPing is how often the socket is pinged; a ping unanswered by the
	// next one ends the connection.
	ListenerPing = 30 * time.Second
	// ListenerWindow is how long a burst of events is gathered into one batch.
	ListenerWindow = 100 * time.Millisecond
	// listenerReadLimit bounds one message: an event is its names alone.
	listenerReadLimit = 16 << 10
)

// ListenerHeartbeat is how often the listener tells HQ it is listening while
// nothing happens: HQ's declaration, which holds the channel's silence to it.
var ListenerHeartbeat = time.Duration(api.MustLimit("InboundHeartbeatSeconds", "default")) * time.Second

// Listener holds the inbound-events Worker's socket open, outbound, and tells
// HQ each event it hears through the bridge's events action.
//
// Its security properties: it dials out and is never dialed; it holds one
// credential, the socket's bearer token, sent only over TLS to the address
// the connection names; it sends nothing on the socket but the protocol's
// pings; what it hears is held to the contract's InboundArrival before it is
// posted, and an event carries only names, which HQ answers by reading with
// its own credentials. A gap in the socket is repaired by HQ reading again
// on each reconnect, so nothing is buffered across one.
type Listener struct {
	// URL is the Worker's socket (wss://), Token its bearer.
	URL, Token string
	// Client makes the handshake; its timeout bounds the handshake alone.
	Client *http.Client
	// Post is the bridge's events action.
	Post func(context.Context, InboundBatch) error
	Log  *slog.Logger
	// The pace, ListenerPing and the rest unless a test sets its own.
	Ping, Window, Heartbeat time.Duration
	Reconnect
}

// NewListener is the listener for an events connection.
func NewListener(connection connections.Connection, client *http.Client, post func(context.Context, InboundBatch) error, log *slog.Logger) (*Listener, error) {
	if connection.APIToken == nil || connection.APIToken.APIToken == "" {
		return nil, &ProviderError{Message: "the events connection", Err: ErrSettingMissing}
	}
	if address, err := url.Parse(connection.APIToken.URL); err != nil || address.Scheme != "wss" || address.Host == "" {
		return nil, &ProviderError{Message: "the events connection's URL is not a wss:// address"}
	}
	return &Listener{
		URL: connection.APIToken.URL, Token: connection.APIToken.APIToken, Client: client, Post: post, Log: log,
		Ping: ListenerPing, Window: ListenerWindow, Heartbeat: ListenerHeartbeat, Reconnect: NewReconnect(),
	}, nil
}

// Run listens until ctx ends, reconnecting whenever the socket or HQ fails.
func (l *Listener) Run(ctx context.Context) {
	l.Reconnect.Run(ctx, l.Log, "event socket lost", "listener.reconnect", l.session)
}

// session is one connection: HQ is told it reconnected, then hears each event
// in batches, and that the socket still answers, until either side fails.
func (l *Listener) session(ctx context.Context) error {
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	// The library closes the handshake's body itself (Dial's documentation).
	conn, _, err := websocket.Dial(ctx, l.URL, &websocket.DialOptions{ //nolint:bodyclose // see above
		HTTPClient: l.Client,
		HTTPHeader: http.Header{"Authorization": {"Bearer " + l.Token}},
	})
	if err != nil {
		return err
	}
	defer func() { _ = conn.CloseNow() }()
	conn.SetReadLimit(listenerReadLimit)

	posted := time.Now()
	post := func(batch InboundBatch) error {
		batch.Source = InboundSourceWorker
		if batch.Events == nil {
			batch.Events = []InboundArrival{}
		}
		if err := l.Post(ctx, batch); err != nil {
			return fmt.Errorf("HQ refused the events: %w", err)
		}
		posted = time.Now()
		return nil
	}
	if err := post(InboundBatch{Reconnected: true}); err != nil {
		return err
	}

	heard := make(chan []byte)
	go func() {
		for {
			_, data, err := conn.Read(ctx)
			if err != nil {
				cancel(err)
				return
			}
			select {
			case heard <- data:
			case <-ctx.Done():
				return
			}
		}
	}()
	answered := make(chan struct{})
	go func() {
		ticker := time.NewTicker(l.Ping)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
			pinged, stop := context.WithTimeout(ctx, l.Ping)
			err := conn.Ping(pinged)
			stop()
			if err != nil {
				cancel(err)
				return
			}
			select {
			case answered <- struct{}{}:
			case <-ctx.Done():
				return
			}
		}
	}()

	var batch []InboundArrival
	var window <-chan time.Time
	for {
		select {
		case <-ctx.Done():
			return context.Cause(ctx)
		case data := <-heard:
			event, err := ReadArrival(data)
			if err != nil {
				l.Log.Warn("event dropped", slog.String("event", "listener.unreadable"), slog.Any("error", err))
				continue
			}
			batch = append(batch, event)
			if window == nil {
				window = time.After(l.Window)
			}
			if len(batch) < MaxInboundEvents {
				continue
			}
		case <-window:
		case <-answered:
			if batch != nil || time.Since(posted) < l.Heartbeat {
				continue
			}
		}
		if err := post(InboundBatch{Events: batch}); err != nil {
			return err
		}
		batch, window = nil, nil
	}
}

// ErrUnknownProvider is an event of a provider HQ takes no events from, which
// HQ would refuse with every event posted beside it.
var ErrUnknownProvider = errors.New("no provider HQ takes events from")

// ErrOutOfBounds is an event whose text the contract's InboundArrival refuses.
var ErrOutOfBounds = errors.New("outside the contract's bounds")

// arrivalText is one text an event carries, and the contract's bound on its
// length in characters. The contract sets no floor on a request's text, since
// a secret struck from it only shortens it.
type arrivalText struct {
	member string
	max    int
	of     func(InboundArrival) string
}

func boundedText(member string, of func(InboundArrival) string) arrivalText {
	return arrivalText{member, api.MustLimit("InboundArrival", "properties", member, "maxLength"), of}
}

var arrivalTexts = []arrivalText{
	boundedText("event", func(e InboundArrival) string { return e.Event }),
	boundedText("subject", func(e InboundArrival) string { return e.Subject }),
	boundedText("delivery_id", func(e InboundArrival) string { return e.DeliveryID }),
}

// ReadArrival is one event as the contract's InboundArrival holds it: no
// member it does not name, a provider HQ declares, and each text within its
// bounds. Every reader passes each event through it, so one out of bounds is
// dropped alone and never makes HQ refuse the batch beside it.
func ReadArrival(data []byte) (InboundArrival, error) {
	var event InboundArrival
	if err := jsonv2.Unmarshal(data, &event, jsonv2.RejectUnknownMembers(true)); err != nil {
		return event, err
	}
	if !event.Provider.Valid() {
		return event, fmt.Errorf("%q: %w", event.Provider, ErrUnknownProvider)
	}
	for _, text := range arrivalTexts {
		if length := utf8.RuneCountInString(text.of(event)); length > text.max {
			return event, fmt.Errorf("%s of %d characters: %w", text.member, length, ErrOutOfBounds)
		}
	}
	return event, nil
}
