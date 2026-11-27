package runtime

import (
	"context"
	"encoding/json/jsontext"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"time"
	"unicode/utf8"

	"github.com/coder/websocket"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/internal/secret"
)

// The listener's pace, each defined once.
const (
	// ListenerPing is how often the socket is pinged; a ping unanswered by the
	// next one ends the connection.
	ListenerPing = 30 * time.Second
	// listenerReadLimit bounds one message: an event is its names alone.
	listenerReadLimit = 16 << 10
)

// Listener holds the inbound-events Worker's socket open, outbound, and tells
// HQ each event it hears through the bridge's events action.
//
// Its security properties: it dials out and is never dialed; it holds one
// credential, the socket's bearer token, sent only over TLS to the address
// the connection names; it sends nothing on the socket but the protocol's
// pings; what it hears is held to the contract's InboundArrival before it is
// posted, and an event carries only names, which HQ answers by reading with
// its own credentials. It holds the id of the last event HQ took and presents
// it on each reconnect, a number in a header beside the token, and the
// Worker's Hub sends the events after it from its log; only when the log no
// longer holds every one does HQ read everything again. Nothing outlives the
// process, so a listener that starts presents no id and HQ reads again. A
// Worker of the release before the log, which deploys apart from HQ's, sends
// each event as it is and no opening; HQ reads again, as it did then.
type Listener struct {
	// URL is the Worker's socket (wss://), Token its bearer.
	URL   string
	Token secret.Value
	// Client makes the handshake; its timeout bounds the handshake alone.
	Client *http.Client
	// Post is the bridge's events action.
	Post func(context.Context, InboundBatch) error
	Log  *slog.Logger
	// The pace, ListenerPing and NewBurst's unless a test sets its own: a
	// ping every Ping, each ending the connection unless answered within
	// PongWait (gorilla/websocket's pingPeriod and pongWait).
	Ping, PongWait time.Duration
	Burst          Burst
	Reconnect
	// held is the id of the last event HQ took, zero before the Hub names one.
	held int64
}

// lastEventID is the header a listener presents held in, 0 for none, as
// Server-Sent Events name it (worker/src/hub.ts LAST_EVENT_ID).
const lastEventID = "Last-Event-ID"

// opening is the Hub's first message on a socket (worker/src/hub.ts
// Opening): the newest id its log holds, and whether the events after the id
// presented follow.
type opening struct {
	LastEventID int64 `json:"last_event_id"`
	Resumed     bool  `json:"resumed"`
}

// logged is each message after it: one event under its id in the Hub's log.
type logged struct {
	ID      int64          `json:"id"`
	Arrival jsontext.Value `json:"arrival"`
}

// NewListener is the listener for an events connection.
func NewListener(connection connections.Connection, client *http.Client, post func(context.Context, InboundBatch) error, log *slog.Logger) (*Listener, error) {
	if connection.APIToken == nil || connection.APIToken.APIToken.IsZero() {
		return nil, &ProviderError{Message: "the events connection", Err: ErrSettingMissing}
	}
	if address, err := url.Parse(connection.APIToken.URL); err != nil || address.Scheme != "wss" || address.Host == "" {
		return nil, &ProviderError{Message: "the events connection's URL is not a wss:// address"}
	}
	return &Listener{
		URL: connection.APIToken.URL, Token: connection.APIToken.APIToken, Client: client, Post: post, Log: log,
		Ping: ListenerPing, PongWait: ListenerPing, Burst: NewBurst(), Reconnect: NewReconnect(),
	}, nil
}

// Run listens until ctx ends, reconnecting whenever the socket or HQ fails.
func (l *Listener) Run(ctx context.Context) {
	l.Reconnect.Run(ctx, l.Log, "event socket lost", "listener.reconnect", l.session)
}

// session is one connection: HQ is told to read again when the listener
// holds no id or the Hub could not send what followed it, then hears each
// event in batches, and that the socket still answers, until either side
// fails. A ping unanswered ends it, so a batch with no events says the socket
// answered within the last two pings.
func (l *Listener) session(ctx context.Context) error {
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	// presented is the id this connection asks to resume from until the Hub
	// answers it, zero once answered.
	presented := l.held
	header := http.Header{"Authorization": {"Bearer " + l.Token.Reveal()}, lastEventID: {strconv.FormatInt(presented, 10)}}
	// The library closes the handshake's body itself (Dial's documentation).
	conn, _, err := websocket.Dial(ctx, l.URL, &websocket.DialOptions{ //nolint:bodyclose // see above
		HTTPClient: l.Client,
		HTTPHeader: header,
	})
	if err != nil {
		return err
	}
	defer func() { _ = conn.CloseNow() }()
	conn.SetReadLimit(listenerReadLimit)

	post := func(batch InboundBatch) error {
		batch.Source = InboundSourceWorker
		if batch.Events == nil {
			batch.Events = []InboundArrival{}
		}
		if err := l.Post(ctx, batch); err != nil {
			return fmt.Errorf("HQ refused the events: %w", err)
		}
		return nil
	}
	// Nothing to resume from: HQ reads everything again, whatever the Hub.
	if presented == 0 {
		if err := post(InboundBatch{Reconnected: true}); err != nil {
			return err
		}
	}
	// through is the id of the last event heard, which HQ has taken once the
	// batch that holds it is posted.
	through := l.held

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
	go func() {
		ticker := time.NewTicker(l.Ping)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
			pinged, stop := context.WithTimeout(ctx, l.PongWait)
			err := conn.Ping(pinged)
			stop()
			if err != nil {
				cancel(err)
				return
			}
		}
	}()

	burst := l.Burst
	for {
		select {
		case <-ctx.Done():
			return context.Cause(ctx)
		case data := <-heard:
			var message logged
			var opened opening
			switch {
			case jsonv2.Unmarshal(data, &message, jsonv2.RejectUnknownMembers(true)) == nil:
				through, data = message.ID, message.Arrival
			case jsonv2.Unmarshal(data, &opened, jsonv2.RejectUnknownMembers(true)) == nil:
				if !opened.Resumed {
					if presented > 0 {
						if err := post(InboundBatch{Reconnected: true}); err != nil {
							return err
						}
					}
					// HQ reads everything again, so what the log held is HQ's already.
					l.held, through = opened.LastEventID, opened.LastEventID
				}
				presented = 0
				continue
			case presented > 0:
				// An event as it is, with no opening before it: a Hub with no
				// log, which resumes nothing, so HQ reads again.
				if err := post(InboundBatch{Reconnected: true}); err != nil {
					return err
				}
				presented, l.held, through = 0, 0, 0
			}
			event, err := ReadArrival(data)
			if err != nil {
				l.Log.Warn("event dropped", slog.String("event", "listener.unreadable"), slog.Any("error", err))
				continue
			}
			if !burst.Add(event) {
				continue
			}
		case <-burst.Due():
		}
		if err := post(InboundBatch{Events: burst.Take()}); err != nil {
			return err
		}
		l.held = through
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
