// Package clock is the controller's one wait that ends with its context. Every
// pause between attempts goes through it, on the time package's own timer, so
// a test run under testing/synctest moves each wait on the bubble's fake clock
// and production needs no clock handed in.
package clock

import (
	"context"
	"time"
)

// Sleep waits d, or until ctx ends, and then reports ctx's error.
func Sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
