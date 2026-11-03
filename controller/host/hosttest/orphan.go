// Package hosttest holds checks every command builder of root's host
// programs is held to.
package hosttest

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// waitBound is generously past the five seconds a builder's WaitDelay gives
// a killed child's pipes, and well short of the orphan's minute.
const waitBound = 20 * time.Second

// ReturnsPastAnOrphan runs a shell that leaves a grandchild holding its
// stdout open, through build, cancels the context once the shell has
// written, and fails unless Wait returns within the bound: with no WaitDelay
// os/exec reads the pipe until the grandchild exits.
func ReturnsPastAnOrphan(t *testing.T, build func(ctx context.Context, name string, args ...string) *exec.Cmd) {
	t.Helper()
	pidFile := filepath.Join(t.TempDir(), "orphan.pid")
	t.Cleanup(func() {
		body, err := os.ReadFile(pidFile)
		if err != nil {
			return
		}
		if pid, err := strconv.Atoi(strings.TrimSpace(string(body))); err == nil {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cmd := build(ctx, "/bin/sh", "-c", `/bin/sleep 60 & echo $! > "$0"; echo x`, pidFile)
	wrote := &firstWrite{done: make(chan struct{})}
	cmd.Stdout = wrote
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-wrote.done:
	case <-time.After(waitBound):
		t.Fatal("the shell never wrote")
	}
	cancel()
	waited := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(waited)
	}()
	select {
	case <-waited:
	case <-time.After(waitBound):
		t.Errorf("Wait was still reading an orphan's pipe %s after its context was cancelled", waitBound)
	}
}

// firstWrite closes done on the first write it is given.
type firstWrite struct {
	once sync.Once
	done chan struct{}
}

func (w *firstWrite) Write(p []byte) (int, error) {
	w.once.Do(func() { close(w.done) })
	return len(p), nil
}
