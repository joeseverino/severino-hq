package runtime

import (
	"context"
	"errors"
	"log/slog"
	"slices"
	"testing"
	"time"
)

// waits runs sessions that each fail at once, under pace, and returns the
// bound of the wait before each reconnect after the first n sessions.
func waits(pace Reconnect, n int) []time.Duration {
	var bounds []time.Duration
	pace.Jitter = func(bound time.Duration) time.Duration {
		bounds = append(bounds, bound)
		return 0
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sessions := 0
	pace.Run(ctx, slog.New(slog.DiscardHandler), "lost", "test.reconnect", func(context.Context) error {
		sessions++
		if sessions > n {
			cancel()
		}
		return errors.New("dropped")
	})
	return bounds
}

func TestTheWaitBeforeEachReconnectDoublesToAMinuteWithFullJitter(t *testing.T) {
	t.Run("doubles to its cap", func(t *testing.T) {
		want := []time.Duration{time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second, 16 * time.Second,
			32 * time.Second, time.Minute, time.Minute}
		if got := waits(NewReconnect(), len(want)); !slices.Equal(got, want) {
			t.Errorf("waits %v, not %v", got, want)
		}
	})
	t.Run("starts again after a connection that held", func(t *testing.T) {
		pace := NewReconnect()
		pace.Steady = 0
		for _, bound := range waits(pace, 4) {
			if bound != time.Second {
				t.Fatalf("a wait of %s after a connection that held", bound)
			}
		}
	})
	t.Run("is drawn from all of its bound", func(t *testing.T) {
		drawn := map[time.Duration]bool{}
		for range 100 {
			wait := NewReconnect().Jitter(time.Second)
			if wait < 0 || wait >= time.Second {
				t.Fatalf("a wait of %s for a bound of a second", wait)
			}
			drawn[wait] = true
		}
		if len(drawn) < 2 {
			t.Error("every wait is the same, so readers that dropped together return together")
		}
	})
}
