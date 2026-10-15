package host

import (
	"context"
	"errors"
	"os"
	"syscall"
	"time"
)

// SharedLock holds path's lock shared, as a reader of what the renderer
// writes under the same lock held exclusively, waiting up to wait for it.
func SharedLock(path string, wait time.Duration) func(context.Context) (func(), error) {
	return func(ctx context.Context) (func(), error) {
		file, err := os.OpenFile(path, os.O_CREATE|os.O_RDONLY, 0o600)
		if err != nil {
			return nil, err
		}
		deadline := time.Now().Add(wait)
		for {
			err := syscall.Flock(int(file.Fd()), syscall.LOCK_SH|syscall.LOCK_NB)
			if err == nil {
				return func() { file.Close() }, nil
			}
			if !errors.Is(err, syscall.EWOULDBLOCK) || time.Now().After(deadline) {
				file.Close()
				return nil, errors.New("timed out waiting for the identity lock")
			}
			select {
			case <-ctx.Done():
				file.Close()
				return nil, ctx.Err()
			case <-time.After(200 * time.Millisecond):
			}
		}
	}
}
