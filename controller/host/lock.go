package host

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// Lock is a flock(2) lock root takes against a less trusted account, so it
// follows the rule of the renderer's own (secrets/install's Tree.Lock):
//   - Its directory is one only this process's account can change, checked on
//     every take, so no other account can plant a symlink at the path, swap
//     the file for one it holds, or hold the lock to stall root.
//   - The file is opened O_NOFOLLOW, so a symlink at the path is refused
//     rather than followed to create a file elsewhere.
//   - A take waits at most Wait and stops with its context, and one that ran
//     out names the lock (LockHeld).
type Lock struct {
	Path string
	// Wait is how long a take waits for a holder of the other kind; zero
	// tries once.
	Wait time.Duration
}

// LockHeld is a take that found the lock still held when its wait ran out.
type LockHeld struct{ Path string }

func (e LockHeld) Error() string { return "timed out waiting for the lock " + e.Path }

// Shared holds the lock beside other shared holders, while no one holds it
// exclusively, until the returned release.
func (l Lock) Shared(ctx context.Context) (func(), error) { return l.take(ctx, syscall.LOCK_SH) }

// Exclusive holds the lock alone until the returned release.
func (l Lock) Exclusive(ctx context.Context) (func(), error) { return l.take(ctx, syscall.LOCK_EX) }

func (l Lock) take(ctx context.Context, how int) (func(), error) {
	if err := unshared(filepath.Dir(l.Path)); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(l.Path, os.O_CREATE|os.O_RDONLY|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, fmt.Errorf("the lock %s could not be opened: %w", l.Path, err)
	}
	deadline := time.Now().Add(l.Wait)
	for {
		err := syscall.Flock(int(file.Fd()), how|syscall.LOCK_NB)
		switch {
		case err == nil:
			return func() { file.Close() }, nil
		case errors.Is(err, syscall.EINTR):
			continue
		case !errors.Is(err, syscall.EWOULDBLOCK):
			file.Close()
			return nil, fmt.Errorf("the lock %s could not be taken: %w", l.Path, err)
		case !time.Now().Before(deadline):
			file.Close()
			return nil, LockHeld{l.Path}
		}
		select {
		case <-ctx.Done():
			file.Close()
			return nil, ctx.Err()
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// unshared refuses a directory, or a link to one, that another account
// could write: it is this process's account's and no one else may write it.
func unshared(dir string) error {
	info, err := os.Lstat(dir)
	if err != nil {
		return fmt.Errorf("the lock's directory %s could not be read: %w", dir, err)
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !info.IsDir() || !ok || int(stat.Uid) != os.Geteuid() || info.Mode().Perm()&0o022 != 0 {
		return fmt.Errorf("refusing the lock in %s: another account can change that directory", dir)
	}
	return nil
}
