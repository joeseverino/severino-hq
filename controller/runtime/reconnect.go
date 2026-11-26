package runtime

import (
	"context"
	"log/slog"
	"math/rand/v2"
	"time"
)

// Reconnect is the pace of a reader that holds a stream open for as long as
// it runs: the wait before each reconnect doubles from FirstRetry to
// LastRetry, and starts again from FirstRetry after a connection that held
// for Steady. Each wait is drawn by Jitter below its bound (full jitter), so
// readers that lost their stream together do not return together.
type Reconnect struct {
	FirstRetry, LastRetry, Steady time.Duration
	// Jitter is the wait before a reconnect, given its bound.
	Jitter func(bound time.Duration) time.Duration
}

// NewReconnect is the pace HQ's stream readers keep: a second doubling to a
// minute, from the first again after a connection held a minute.
func NewReconnect() Reconnect {
	return Reconnect{
		FirstRetry: time.Second, LastRetry: time.Minute, Steady: time.Minute,
		// A wait needs spreading, not secrecy.
		Jitter: rand.N[time.Duration], //nolint:gosec // G404: see above
	}
}

// Run runs session again each time it returns, until ctx ends, logging each
// loss as lost under the event name event.
func (r Reconnect) Run(ctx context.Context, log *slog.Logger, lost, event string, session func(context.Context) error) {
	retry := r.FirstRetry
	for {
		began := time.Now()
		err := session(ctx)
		if ctx.Err() != nil {
			return
		}
		if time.Since(began) >= r.Steady {
			retry = r.FirstRetry
		}
		wait := r.Jitter(retry)
		log.Warn(lost, slog.String("event", event), slog.Any("error", err), slog.Duration("wait", wait))
		retry = min(2*retry, r.LastRetry)
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}
